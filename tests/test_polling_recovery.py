"""接收自愈回归：使用真实 PTB 请求链及本机故障，不访问 Telegram/CD2。"""

import asyncio
import json
import subprocess
import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import httpx
from telegram.error import RetryAfter, TimedOut
from telegram.ext import ApplicationBuilder, MessageHandler, filters
from telegram.ext._utils.networkloop import network_retry_loop
from telegram.request import HTTPXRequest

import main


def _health_context(health, *, running=True, task=None):
    application = SimpleNamespace(
        running=running,
        updater=SimpleNamespace(),
        bot_data={"polling_health": health},
        update_queue=asyncio.Queue(),
        stop_running=MagicMock(),
    )
    if task is not None:
        application.updater._Updater__polling_task = task
    return SimpleNamespace(application=application)


class PollingApiHealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.health = main.PollingHealth()
        self.calls = []
        self.responses = []

        async def transport(request):
            self.calls.append(request)
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            status, body = result if isinstance(result, tuple) else (200, result)
            return httpx.Response(status, content=body)

        self.request = main.PollingHTTPXRequest(
            polling_health=self.health,
            httpx_kwargs={"transport": httpx.MockTransport(transport), "trust_env": False,
                          "verify": False},
        )
        self.send_request = HTTPXRequest(httpx_kwargs={"trust_env": False, "verify": False})
        # 假 Token 仅供 PTB 验证格式；所有请求交给内存 transport，不连接公网。
        self.bot = main.MonitoredExtBot(
            "123456:FAKE_TOKEN_FOR_LOCAL_TEST_ONLY",
            polling_health=self.health,
            request=self.send_request,
            get_updates_request=self.request,
        )
        await self.request.initialize()

    async def asyncTearDown(self):
        await self.request.shutdown()
        await self.send_request.shutdown()

    async def test_empty_valid_api_response_counts_as_healthy_idle(self):
        self.responses = [b'{"ok": true, "result": []}']
        self.health.consecutive_failures = 5

        result = await self.bot.get_updates(offset=314, timeout=0)

        self.assertEqual(result, ())
        self.assertIsNotNone(self.health.last_success_at)
        self.assertEqual(self.health.consecutive_failures, 0)
        self.assertEqual(len(self.calls), 1)

    async def test_http_200_without_valid_api_result_does_not_refresh_health(self):
        invalid_responses = [
            b"not JSON",
            b'{"ok": false, "error_code": 400, "description": "Bad Request"}',
            b'{"ok": true, "result": [42]}',
        ]
        for payload in invalid_responses:
            with self.subTest(payload=payload):
                self.responses = [payload]
                self.health.last_success_at = 10.0
                with self.assertRaises(Exception):
                    await self.bot.get_updates(offset=314, timeout=0)
                self.assertEqual(self.health.last_success_at, 10.0)

    async def test_real_ptb_silent_timeout_loop_is_observed_without_replaying_request(self):
        self.responses = [
            httpx.ReadTimeout("local timeout 1"),
            httpx.PoolTimeout("local pool exhausted"),
            httpx.ConnectTimeout("local connect timeout"),
            b'{"ok": true, "result": []}',
        ]
        error_callback = MagicMock()
        send_client = self.send_request._client
        updates_client = self.request._client

        with self.assertLogs(main.logger, level="WARNING") as captured:
            await network_retry_loop(
                action_cb=lambda: self.bot.get_updates(
                    offset=314, limit=7, timeout=0, allowed_updates=["message"]
                ),
                on_err_cb=error_callback,
                description="isolated polling regression",
                interval=0,
                max_retries=-1,
            )

        # PTB 的 TimedOut 分支仍不走错误回调；可观测性必须由我们的接收边界补齐。
        error_callback.assert_not_called()
        self.assertTrue(captured.output)
        self.assertIsNotNone(self.health.last_success_at)
        self.assertEqual(self.health.consecutive_failures, 0)
        self.assertEqual(len(self.calls), 4, "底层恢复不得暗中增加 getUpdates 请求")
        for request in self.calls:
            self.assertTrue(request.url.path.endswith("/getUpdates"))
            data = parse_qs(request.content.decode())
            self.assertEqual(data["offset"], ["314"])
            self.assertEqual(data["limit"], ["7"])
            self.assertEqual(json.loads(data["allowed_updates"][0]), ["message"])
        self.assertIsNot(self.request._client, updates_client)
        self.assertIs(self.send_request._client, send_client)
        self.assertFalse(send_client.is_closed)

    async def test_retry_after_is_recorded_without_claiming_success(self):
        self.responses = [
            (429, b'{"ok": false, "error_code": 429, "description": "Too Many Requests", '
             b'"parameters": {"retry_after": 600}}')
        ]
        before = time.monotonic()
        with self.assertRaises(RetryAfter):
            await self.bot.get_updates(timeout=0)
        self.assertIsNone(self.health.last_success_at)
        self.assertGreaterEqual(self.health.retry_until, before + 600)


class PollingWatchdogProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_grace_prevents_false_restart(self):
        health = main.PollingHealth()
        context = _health_context(health)
        with patch.object(main, "_request_fatal_restart") as restart:
            await main.watchdog_check(context)
        restart.assert_not_called()

    async def test_live_task_without_api_progress_triggers_restart(self):
        health = main.PollingHealth()
        health.started_at = time.monotonic() - main.POLLING_STALL_SECONDS - 1
        context = _health_context(health, task=SimpleNamespace(done=lambda: False))
        with patch.object(main, "_request_fatal_restart") as restart:
            await main.watchdog_check(context)
        restart.assert_called_once()

    async def test_recent_empty_poll_success_prevents_restart(self):
        health = main.PollingHealth()
        health.started_at = time.monotonic() - main.POLLING_STALL_SECONDS * 2
        health.mark_success(0)
        context = _health_context(health, task=SimpleNamespace(done=lambda: False))
        with patch.object(main, "_request_fatal_restart") as restart:
            await main.watchdog_check(context)
        restart.assert_not_called()

    async def test_retry_after_wait_does_not_trigger_stall_restart(self):
        health = main.PollingHealth()
        health.started_at = time.monotonic() - main.POLLING_STALL_SECONDS * 2
        health.record_failure(RetryAfter(timedelta(seconds=600)))
        context = _health_context(health, task=SimpleNamespace(done=lambda: False))
        with patch.object(main, "_request_fatal_restart") as restart:
            await main.watchdog_check(context)
        restart.assert_not_called()

    async def test_stall_after_retry_after_grace_expires_restarts(self):
        health = main.PollingHealth()
        health.started_at = time.monotonic() - main.POLLING_STALL_SECONDS * 2
        health.retry_until = time.monotonic() - main.POLLING_STALL_SECONDS - 1
        context = _health_context(health, task=SimpleNamespace(done=lambda: False))
        with patch.object(main, "_request_fatal_restart") as restart:
            await main.watchdog_check(context)
        restart.assert_called_once()

    async def test_private_task_attribute_missing_does_not_disable_progress_monitor(self):
        health = main.PollingHealth()
        health.started_at = time.monotonic() - main.POLLING_STALL_SECONDS - 1
        context = _health_context(health)
        with patch.object(main, "_request_fatal_restart") as restart:
            await main.watchdog_check(context)
        restart.assert_called_once()

    async def test_shutdown_does_not_turn_stale_health_into_failure(self):
        health = main.PollingHealth()
        health.started_at = 0
        context = _health_context(health, running=False)
        with patch.object(main, "_request_fatal_restart") as restart:
            await main.watchdog_check(context)
        restart.assert_not_called()

    async def test_updater_shutdown_does_not_restart_while_application_is_still_running(self):
        health = main.PollingHealth()
        health.started_at = 0
        context = _health_context(health)
        context.application.updater.running = False
        with patch.object(main, "_request_fatal_restart") as restart:
            await main.watchdog_check(context)
        restart.assert_not_called()


class PollingPoolRecoveryBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.health = main.PollingHealth()
        self.attempts = 0
        self.now = 100.0

        async def transport(_request):
            self.attempts += 1
            raise httpx.PoolTimeout("local exhausted pool")

        self.request = main.PollingHTTPXRequest(
            polling_health=self.health,
            httpx_kwargs={"transport": httpx.MockTransport(transport),
                          "trust_env": False, "verify": False},
        )
        self.clock = patch.object(main, "_polling_now", side_effect=lambda: self.now)
        self.clock.start()
        await self.request.initialize()

    async def asyncTearDown(self):
        self.clock.stop()
        await self.request.shutdown()

    async def _fail(self, count=1):
        for _ in range(count):
            with self.assertRaises(TimedOut):
                await self.request.do_request("https://local.invalid/getUpdates", "POST")

    async def test_threshold_cooldown_and_budget_bound_rebuilds_without_hidden_calls(self):
        original_client = self.request._client
        await self._fail(main.POLLING_RECOVERY_THRESHOLD - 1)
        self.assertIs(self.request._client, original_client)
        await self._fail()
        self.assertIsNot(self.request._client, original_client)
        self.assertEqual(self.request._recovery_count, 1)

        self.now += main.POLLING_RECOVERY_COOLDOWN_SECONDS - 1
        await self._fail()
        self.assertEqual(self.request._recovery_count, 1, "冷却期间不得反复重建")
        for expected in range(2, main.POLLING_MAX_RECOVERIES + 1):
            self.now += main.POLLING_RECOVERY_COOLDOWN_SECONDS
            await self._fail()
            self.assertEqual(self.request._recovery_count, expected)
        self.now += main.POLLING_RECOVERY_COOLDOWN_SECONDS
        await self._fail()
        self.assertEqual(self.request._recovery_count, main.POLLING_MAX_RECOVERIES)
        self.assertEqual(self.attempts,
                         main.POLLING_RECOVERY_THRESHOLD + 1 + main.POLLING_MAX_RECOVERIES)

        self.request.note_success()
        self.assertEqual(self.request._recovery_count, 0)
        self.now += main.POLLING_RECOVERY_COOLDOWN_SECONDS
        await self._fail(main.POLLING_RECOVERY_THRESHOLD)
        self.assertEqual(self.request._recovery_count, 1, "API成功后允许新的恢复周期")

    async def test_rebuild_timeout_is_bounded_and_preserves_original_request_error(self):
        await self._fail(main.POLLING_RECOVERY_THRESHOLD - 1)
        never_finishes = asyncio.Event()

        async def stuck_shutdown(_request):
            await never_finishes.wait()

        with patch.object(HTTPXRequest, "shutdown", stuck_shutdown), \
                patch.object(main, "POLLING_RECOVERY_TIMEOUT_SECONDS", 0.02):
            with self.assertRaises(TimedOut):
                await asyncio.wait_for(
                    self.request.do_request("https://local.invalid/getUpdates", "POST"),
                    timeout=1,
                )
        self.assertEqual(self.request._recovery_count, 1)
        with self.assertRaises(main.RequestNotSent):
            await self.request.do_request("https://local.invalid/getUpdates", "POST")
        self.assertEqual(self.attempts, main.POLLING_RECOVERY_THRESHOLD)


class LocalPollingTimeoutRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """真实 TCP 读超时后仍能成功接收；所有套接字仅绑定本机。"""

    loop_factory = asyncio.SelectorEventLoop

    async def test_repeated_real_read_timeouts_recover_without_changing_offset(self):
        requests = []
        handlers = set()
        connections = set()

        async def api(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            connections.add(writer)
            try:
                header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
                length = 0
                for line in header.split(b"\r\n")[1:]:
                    name, separator, value = line.partition(b":")
                    if separator and name.lower() == b"content-length":
                        length = int(value.strip())
                body = await asyncio.wait_for(reader.readexactly(length), timeout=2)
                requests.append((header.split(b"\r\n")[0], parse_qs(body.decode())))
                if len(requests) <= main.POLLING_RECOVERY_THRESHOLD:
                    # 请求已经真实到达服务端，但故意不回响应，触发 httpx 的 ReadTimeout。
                    await asyncio.wait_for(reader.read(), timeout=2)
                else:
                    payload = b'{"ok": true, "result": []}'
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                        + str(len(payload)).encode() + b"\r\n\r\n" + payload
                    )
                    await writer.drain()
                    await asyncio.wait_for(reader.read(), timeout=2)
            except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
                connections.discard(writer)
                handlers.discard(task)

        server = await asyncio.start_server(api, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        health = main.PollingHealth()
        request = main.PollingHTTPXRequest(
            polling_health=health,
            connection_pool_size=1,
            read_timeout=0.05,
            connect_timeout=1,
            pool_timeout=0.05,
            httpx_kwargs={"trust_env": False, "verify": False},
        )
        send_request = HTTPXRequest(httpx_kwargs={"trust_env": False, "verify": False})
        bot = main.MonitoredExtBot(
            "123456:LOCAL_TCP_TEST_TOKEN_ONLY",
            base_url=f"http://127.0.0.1:{port}/bot",
            polling_health=health,
            request=send_request,
            get_updates_request=request,
        )
        await request.initialize()
        original_client = request._client
        try:
            for _ in range(main.POLLING_RECOVERY_THRESHOLD):
                with self.assertRaises(TimedOut):
                    await bot.get_updates(offset=2718, timeout=0)
                self.assertIsNone(health.last_success_at)
            self.assertIsNot(request._client, original_client)
            self.assertEqual(await bot.get_updates(offset=2718, timeout=0), ())
            self.assertIsNotNone(health.last_success_at)
            self.assertEqual(len(requests), main.POLLING_RECOVERY_THRESHOLD + 1)
            for request_line, data in requests:
                self.assertIn(b"/getUpdates ", request_line)
                self.assertEqual(data["offset"], ["2718"])
        finally:
            await request.shutdown()
            await send_request.shutdown()
            server.close()
            await server.wait_closed()
            for writer in tuple(connections):
                writer.close()
            if handlers:
                await asyncio.wait_for(asyncio.gather(*tuple(handlers), return_exceptions=True), 3)

    async def test_real_application_updater_recovers_and_delivers_update_once(self):
        handlers = set()
        polling_parameters = []
        methods = []
        delivered = []
        message_received = asyncio.Event()
        health = main.PollingHealth()

        async def api(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
                lines = header.split(b"\r\n")
                method = lines[0].split(b" ")[1].rsplit(b"/", 1)[1].decode()
                length = next((int(line.split(b":", 1)[1]) for line in lines[1:]
                               if line.lower().startswith(b"content-length:")), 0)
                body = await asyncio.wait_for(reader.readexactly(length), 2)
                methods.append(method)
                if method == "getMe":
                    result = {"id": 123456, "is_bot": True, "first_name": "Local test",
                              "username": "local_test_bot"}
                elif method == "deleteWebhook":
                    result = True
                elif method == "getUpdates":
                    polling_parameters.append(parse_qs(body.decode()))
                    if len(polling_parameters) <= main.POLLING_RECOVERY_THRESHOLD:
                        await asyncio.wait_for(reader.read(), 2)
                        return
                    result = ([{"update_id": 314, "message": {
                        "message_id": 1, "date": 1700000000,
                        "chat": {"id": 123, "type": "private"},
                        "from": {"id": 123, "is_bot": False, "first_name": "Test"},
                        "text": "local lifecycle test",
                    }}] if len(polling_parameters) == main.POLLING_RECOVERY_THRESHOLD + 1
                              else [])
                else:
                    raise AssertionError(f"未预期的本机 API 方法：{method}")
                payload = json.dumps({"ok": True, "result": result}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                             + str(len(payload)).encode() + b"\r\n\r\n" + payload)
                await writer.drain()
                await asyncio.wait_for(reader.read(), 2)
            except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
                handlers.discard(task)

        async def receive(update, _context):
            delivered.append(update.update_id)
            message_received.set()

        server = await asyncio.start_server(api, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        request = main.PollingHTTPXRequest(
            polling_health=health, read_timeout=0.05, connect_timeout=1,
            httpx_kwargs={"trust_env": False, "verify": False},
        )
        bot = main.MonitoredExtBot(
            "123456:LOCAL_LIFECYCLE_TEST_TOKEN_ONLY",
            base_url=f"http://127.0.0.1:{port}/bot", polling_health=health,
            request=HTTPXRequest(httpx_kwargs={"trust_env": False, "verify": False}),
            get_updates_request=request,
        )
        application = ApplicationBuilder().bot(bot).build()
        application.bot_data["polling_health"] = health
        application.add_handler(MessageHandler(filters.TEXT, receive))
        try:
            await application.initialize()
            await application.updater.start_polling(
                timeout=0, drop_pending_updates=False, bootstrap_retries=0,
            )
            await application.start()
            await asyncio.wait_for(message_received.wait(), 3)
            await application.updater.stop()
            await application.stop()
            self.assertEqual(delivered, [314])
            self.assertIsNotNone(health.last_success_at)
            self.assertIn("getMe", methods)
            self.assertEqual(methods.count("deleteWebhook"), 1,
                             "接收池恢复不得重启Updater或重新设置webhook")
            self.assertEqual([item["offset"] for item in
                              polling_parameters[:main.POLLING_RECOVERY_THRESHOLD + 1]],
                             [["0"]] * (main.POLLING_RECOVERY_THRESHOLD + 1))
            self.assertTrue(all(item["offset"] == ["315"] for item in
                                polling_parameters[main.POLLING_RECOVERY_THRESHOLD + 1:]))
        finally:
            if application.updater.running:
                await application.updater.stop()
            if application.running:
                await application.stop()
            await application.shutdown()
            server.close()
            await server.wait_closed()
            if handlers:
                await asyncio.wait_for(asyncio.gather(*tuple(handlers), return_exceptions=True), 3)


class FatalRestartTests(unittest.TestCase):
    def test_restart_is_nonzero_even_if_ptb_shutdown_returns_normally(self):
        application = MagicMock()
        health = main.PollingHealth()
        with patch.object(main.threading, "Timer") as timer:
            main._request_fatal_restart(application, health, "stale polling")
            with self.assertRaises(SystemExit) as caught:
                main.run_bot(application, health)
        self.assertEqual(caught.exception.code, 1)
        application.stop_running.assert_called_once()
        self.assertFalse(application.run_polling.call_args.kwargs["drop_pending_updates"])
        timer.return_value.start.assert_called_once()
        timer.return_value.cancel.assert_called_once()
        timer.assert_called_once_with(main.POLLING_EXIT_TIMEOUT_SECONDS, main._force_process_exit)
        self.assertTrue(timer.return_value.daemon)

    def test_repeated_watchdog_failure_does_not_schedule_multiple_exits(self):
        application = MagicMock()
        health = main.PollingHealth()
        with patch.object(main.threading, "Timer") as timer:
            main._request_fatal_restart(application, health, "first failure")
            main._request_fatal_restart(application, health, "second failure")
        timer.assert_called_once()
        application.stop_running.assert_called_once()

    def test_normal_shutdown_preserves_success_exit(self):
        application = MagicMock()
        main.run_bot(application, main.PollingHealth())
        self.assertFalse(application.run_polling.call_args.kwargs["drop_pending_updates"])

    def test_force_exit_uses_nonzero_status_when_graceful_shutdown_hangs(self):
        with patch.object(main.os, "_exit") as exit_process:
            main._force_process_exit()
        exit_process.assert_called_once_with(1)

    def test_hung_shutdown_is_actually_killed_with_exit_one_in_child_process(self):
        # 真正调用 os._exit 仅限隔离子进程，验证独立计时线程可终止卡住的退出流程。
        script = """
import time
import main
main.POLLING_EXIT_TIMEOUT_SECONDS = 0.1
health = main.PollingHealth()
class HungApplication:
    def stop_running(self):
        pass
    def run_polling(self, **kwargs):
        main._request_fatal_restart(self, health, 'isolated hung shutdown test')
        time.sleep(10)
main.run_bot(HungApplication(), health)
"""
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", script],
            cwd=Path(main.__file__).parent,
            capture_output=True, timeout=5,
        )
        self.assertEqual(result.returncode, 1, result.stderr.decode("utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main()
