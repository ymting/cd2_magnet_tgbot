"""回执恢复回归：真实本机代理握手故障、通知补发和 CD2 提交隔离。

网络探针仅绑定 127.0.0.1；保留真实 httpx/httpcore 连接池行为，不访问 Telegram。
"""

import asyncio
from datetime import timedelta
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.request import HTTPXRequest

import main


def _connect_failure():
    """PTB 保留原始 httpx 原因，恢复策略据此区分未发送与送达未知。"""
    error = NetworkError("httpx.ConnectError: test connection failure")
    error.__cause__ = httpx.ConnectError("test connection failure")
    return error


class _FakeChannel:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class LocalTransportRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """超出连接池容量的真实 TLS 失败必须仍能建立下一条连接。"""

    async def asyncSetUp(self):
        self.connect_count = 0
        self.connections = set()
        self.handlers = set()
        self.release_http = asyncio.Event()
        self.http_started = asyncio.Event()
        self.block_first_http = False
        self.server = await asyncio.start_server(self._proxy, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _proxy(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        self.connections.add(writer)
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
            if header.startswith(b"CONNECT "):
                self.connect_count += 1
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                # 代理成功建隧道后，目标侧 TLS 握手被断开；这是生产疑似泄漏的同一路径。
                await asyncio.wait_for(reader.read(4096), timeout=3)
            else:
                # 先消费完整请求，避免 Windows 在关闭仍有未读数据的 socket 时发 RST。
                for line in header.split(b"\r\n")[1:]:
                    name, separator, value = line.partition(b":")
                    if separator and name.strip().lower() == b"content-length":
                        length = int(value.strip())
                        if length:
                            await asyncio.wait_for(reader.readexactly(length), timeout=3)
                        break
                self.http_started.set()
                if self.block_first_http:
                    self.block_first_http = False
                    await self.release_http.wait()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
                await writer.drain()
                # drain 只保证写进操作系统缓冲；等客户端读完响应并关闭后再关服务端。
                # TLS 故障分支仍主动断开，以保留真实握手失败，而 HTTP 200 不制造关闭竞态。
                await asyncio.wait_for(reader.read(), timeout=3)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.connections.discard(writer)
            self.handlers.discard(task)

    async def asyncTearDown(self):
        self.release_http.set()
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self.connections):
            writer.close()
        if self.handlers:
            await asyncio.gather(*list(self.handlers), return_exceptions=True)

    def _request(self, cls=main.ResilientHTTPXRequest):
        return cls(
            proxy=f"http://127.0.0.1:{self.port}",
            connection_pool_size=2,
            connect_timeout=0.6,
            pool_timeout=0.1,
            read_timeout=2,
            write_timeout=2,
            httpx_kwargs={"verify": False, "trust_env": False},
        )

    async def test_tls_failures_beyond_capacity_never_leave_pool_occupied(self):
        request = self._request()
        await request.initialize()
        try:
            for _ in range(5):
                with self.assertRaises(NetworkError) as caught:
                    await request.do_request("https://unused.invalid/sendMessage", "GET")
                self.assertIsInstance(caught.exception.__cause__, httpx.ConnectError)
            self.assertEqual(self.connect_count, 5)
            # 同一发送器经历多轮失败后，可恢复发送；.invalid 地址从未被代理连接。
            status, body = await request.do_request("http://unused.invalid/sendMessage", "GET")
            self.assertEqual((status, body), (200, b"OK"))
        finally:
            await request.shutdown()

    async def test_send_pool_rebuild_does_not_replace_getupdates_client(self):
        send_request = self._request()
        updates_request = self._request(HTTPXRequest)
        await send_request.initialize()
        await updates_request.initialize()
        original_update_client = updates_request._client
        original_send_client = send_request._client
        try:
            with self.assertRaises(NetworkError):
                await send_request.do_request("https://unused.invalid/sendMessage", "GET")
            self.assertIsNot(send_request._client, original_send_client)
            self.assertIs(updates_request._client, original_update_client)
            self.assertFalse(original_update_client.is_closed)
            self.assertEqual(
                await updates_request.do_request("http://unused.invalid/getUpdates", "GET"),
                (200, b"OK"),
            )
        finally:
            await send_request.shutdown()
            await updates_request.shutdown()

    async def test_rebuild_never_closes_another_inflight_request(self):
        self.block_first_http = True
        request = self._request()
        await request.initialize()
        first = asyncio.create_task(request.do_request("http://unused.invalid/sendMessage", "GET"))
        await asyncio.wait_for(self.http_started.wait(), timeout=2)
        second = asyncio.create_task(request.do_request("https://unused.invalid/sendMessage", "GET"))
        try:
            await asyncio.sleep(0.05)
            self.assertFalse(first.done(), "在途回执不应因另一条失败连接被关闭")
            self.release_http.set()
            self.assertEqual(await asyncio.wait_for(first, timeout=3), (200, b"OK"))
            with self.assertRaises(NetworkError):
                await asyncio.wait_for(second, timeout=3)
            self.assertEqual(
                await request.do_request("http://unused.invalid/sendMessage", "GET"),
                (200, b"OK"),
            )
        finally:
            self.release_http.set()
            for task in (first, second):
                if not task.done():
                    task.cancel()
            await asyncio.gather(first, second, return_exceptions=True)
            await request.shutdown()

    async def test_cancelled_request_releases_lock_and_allows_next_send(self):
        self.block_first_http = True
        request = self._request()
        await request.initialize()
        sending = asyncio.create_task(request.do_request("http://unused.invalid/sendMessage", "GET"))
        try:
            await asyncio.wait_for(self.http_started.wait(), timeout=2)
            sending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await sending
            self.release_http.set()
            self.assertEqual(
                await asyncio.wait_for(request.do_request("http://unused.invalid/sendMessage", "GET"), timeout=3),
                (200, b"OK"),
            )
        finally:
            self.release_http.set()
            await asyncio.gather(sending, return_exceptions=True)
            await request.shutdown()

    async def test_shutdown_waits_for_inflight_request_then_closes_client(self):
        self.block_first_http = True
        request = self._request()
        await request.initialize()
        sending = asyncio.create_task(request.do_request("http://unused.invalid/sendMessage", "GET"))
        await asyncio.wait_for(self.http_started.wait(), timeout=2)
        shutting_down = asyncio.create_task(request.shutdown())
        try:
            await asyncio.sleep(0.05)
            self.assertFalse(shutting_down.done())
            self.release_http.set()
            self.assertEqual(await asyncio.wait_for(sending, timeout=3), (200, b"OK"))
            await asyncio.wait_for(shutting_down, timeout=3)
            self.assertTrue(request._client.is_closed)
        finally:
            self.release_http.set()
            await asyncio.gather(sending, shutting_down, return_exceptions=True)
            await request.shutdown()


class ReplyQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main._pending_replies.clear()
        self.time = 1000.0
        self.patchers = [
            patch.object(main, "REPLY_RETRY_DELAY_SECONDS", 0),
            patch.object(main, "ADMIN_IDS", [123]),
            # 只替换主模块的时钟，不能改共享 time 模块，否则 asyncio 本身也会冻结。
            patch.object(main, "time", SimpleNamespace(monotonic=lambda: self.time)),
        ]
        for patcher in self.patchers:
            patcher.start()

    async def asyncTearDown(self):
        main._pending_replies.clear()
        for patcher in reversed(self.patchers):
            patcher.stop()

    def _update(self, text, send):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            message=SimpleNamespace(text=text, reply_text=send),
        )

    async def _handle(self, text, send, stub):
        with patch("main.grpc.aio.insecure_channel", return_value=_FakeChannel()), patch(
            "main.clouddrive_pb2_grpc.CloudDriveFileSrvStub", return_value=stub
        ):
            await main.handle_link(self._update(text, send), None)

    async def _flush(self):
        self.time += main.PENDING_REPLY_INTERVAL_SECONDS + 1
        await main.flush_pending_replies(self._context())

    def _context(self, running=True):
        return SimpleNamespace(application=SimpleNamespace(running=running))

    async def test_failed_success_receipt_is_queued_and_resends_without_cd2_submission(self):
        stub = SimpleNamespace(AddOfflineFiles=AsyncMock(return_value=SimpleNamespace(success=True)))
        send = AsyncMock(side_effect=_connect_failure())
        await self._handle("magnet:?xt=urn:btih:" + "A" * 40, send, stub)
        self.assertEqual(stub.AddOfflineFiles.await_count, 1)
        self.assertEqual(len(main._pending_replies), 1)
        send.side_effect = None
        send.return_value = object()
        await self._flush()
        self.assertEqual(len(main._pending_replies), 0)
        self.assertEqual(stub.AddOfflineFiles.await_count, 1)
        for call in send.await_args_list:
            self.assertIn("提交成功", call.args[0])
            self.assertNotIn("CD2 连接异常", call.args[0])

    async def test_failed_batch_progress_only_final_report_is_queued(self):
        stub = SimpleNamespace(AddOfflineFiles=AsyncMock(return_value=SimpleNamespace(success=True)))
        send = AsyncMock(side_effect=_connect_failure())
        await self._handle(
            "magnet:?xt=urn:btih:" + "A" * 40 + "\nmagnet:?xt=urn:btih:" + "B" * 40,
            send,
            stub,
        )
        self.assertEqual(stub.AddOfflineFiles.await_count, 2)
        self.assertEqual(len(main._pending_replies), 1)
        self.assertIn("批量提交完成", main._pending_replies[0].text)
        self.assertNotIn("正在逐个提交", main._pending_replies[0].text)
        send.side_effect = None
        await self._flush()
        self.assertEqual(len(main._pending_replies), 0)
        self.assertEqual(stub.AddOfflineFiles.await_count, 2)

    async def test_failed_edit_and_fallback_never_queue_two_reports(self):
        edit = AsyncMock(side_effect=_connect_failure())
        send = AsyncMock(side_effect=[SimpleNamespace(edit_text=edit)] + [_connect_failure()] * 3)
        stub = SimpleNamespace(AddOfflineFiles=AsyncMock(return_value=SimpleNamespace(success=True)))
        await self._handle(
            "magnet:?xt=urn:btih:" + "A" * 40 + "\nmagnet:?xt=urn:btih:" + "B" * 40,
            send,
            stub,
        )
        self.assertEqual(len(main._pending_replies), 1)
        send.side_effect = None
        edit.side_effect = None
        await self._flush()
        self.assertEqual(len(main._pending_replies), 0)
        self.assertEqual(stub.AddOfflineFiles.await_count, 2)

    async def test_batch_edit_with_lost_response_only_retries_same_message(self):
        error = TimedOut()
        error.__cause__ = httpx.ReadTimeout("reply response lost")
        edit = AsyncMock(side_effect=error)
        send = AsyncMock(return_value=SimpleNamespace(edit_text=edit))
        stub = SimpleNamespace(AddOfflineFiles=AsyncMock(return_value=SimpleNamespace(success=True)))
        await self._handle(
            "magnet:?xt=urn:btih:" + "A" * 40 + "\nmagnet:?xt=urn:btih:" + "B" * 40,
            send, stub,
        )
        self.assertEqual(send.await_count, 1, "编辑结果未知不能新发第二份报告")
        self.assertEqual(len(main._pending_replies), 1)
        self.assertTrue(main._pending_replies[0].is_edit)
        edit.side_effect = BadRequest("Message is not modified")
        await self._flush()
        self.assertFalse(main._pending_replies)
        self.assertEqual(send.await_count, 1)
        self.assertEqual(stub.AddOfflineFiles.await_count, 2)

    async def test_permanent_edit_failure_falls_back_to_one_new_report(self):
        edit = AsyncMock(side_effect=BadRequest("Message to edit not found"))
        send = AsyncMock(side_effect=[SimpleNamespace(edit_text=edit), object()])
        stub = SimpleNamespace(AddOfflineFiles=AsyncMock(return_value=SimpleNamespace(success=True)))
        await self._handle(
            "magnet:?xt=urn:btih:" + "A" * 40 + "\nmagnet:?xt=urn:btih:" + "B" * 40,
            send, stub,
        )
        self.assertEqual(edit.await_count, 1)
        self.assertEqual(send.await_count, 2)
        self.assertIn("批量提交完成", send.await_args.args[0])
        self.assertFalse(main._pending_replies)

    async def test_badrequest_and_forbidden_never_retry_or_queue(self):
        for error in (BadRequest("can't parse entities"), Forbidden("bot blocked")):
            with self.subTest(error=type(error).__name__):
                send = AsyncMock(side_effect=error)
                self.assertIsNone(await main._safe_send(send, "text", "test", queue_on_failure=True))
                self.assertEqual(send.await_count, 1)
                self.assertEqual(len(main._pending_replies), 0)

    async def test_delivery_unknown_is_not_retried_or_queued_as_new_message(self):
        for cause in (httpx.ReadTimeout("response lost"), httpx.WriteError("partial request"), None):
            with self.subTest(cause=type(cause).__name__):
                error = TimedOut() if cause is None else NetworkError("response unknown")
                error.__cause__ = cause
                send = AsyncMock(side_effect=error)
                await main._safe_send(send, "text", "test", queue_on_failure=True)
                self.assertEqual(send.await_count, 1)
                self.assertEqual(len(main._pending_replies), 0)

        send = AsyncMock(side_effect=NetworkError("unidentified transport interruption"))
        await main._safe_send(send, "text", "test", queue_on_failure=True)
        self.assertEqual(send.await_count, 1)
        self.assertFalse(main._pending_replies)

    async def test_message_not_modified_is_success_for_edit(self):
        edit = AsyncMock(side_effect=BadRequest("Message is not modified"))
        result = await main._safe_send(edit, "text", "test", is_edit=True, queue_on_failure=True)
        self.assertTrue(result)
        self.assertEqual(edit.await_count, 1)
        self.assertFalse(main._pending_replies)

    async def test_capacity_and_ttl_bound_unsent_receipts(self):
        with patch.object(main, "PENDING_REPLY_MAX_ITEMS", 2):
            for number in range(3):
                await main._safe_send(
                    AsyncMock(side_effect=_connect_failure()), str(number), "test", queue_on_failure=True
                )
            self.assertLessEqual(len(main._pending_replies), 2)
        self.time += main.PENDING_REPLY_TTL_SECONDS + 1
        before = [item.send_func.await_count for item in main._pending_replies]
        senders = [item.send_func for item in main._pending_replies]
        await main.flush_pending_replies(self._context())
        self.assertFalse(main._pending_replies)
        self.assertEqual([sender.await_count for sender in senders], before)

    async def test_long_retryafter_is_queued_until_telegram_allows_retry(self):
        send = AsyncMock(side_effect=RetryAfter(60))
        await main._safe_send(send, "text", "test", queue_on_failure=True)
        self.assertEqual(send.await_count, 1)
        self.assertEqual(len(main._pending_replies), 1)
        self.assertGreaterEqual(main._pending_replies[0].next_attempt_at, self.time + 60)
        self.time += 59
        await main.flush_pending_replies(self._context())
        self.assertEqual(send.await_count, 1)
        send.side_effect = None
        self.time += 2
        await main.flush_pending_replies(self._context())
        self.assertEqual(send.await_count, 2)
        self.assertFalse(main._pending_replies)

    async def test_timedelta_retryafter_respects_next_allowed_time(self):
        send = AsyncMock(side_effect=RetryAfter(timedelta(seconds=60)))
        await main._safe_send(send, "text", "test", queue_on_failure=True)
        self.assertEqual(send.await_count, 1)
        self.assertEqual(len(main._pending_replies), 1)
        self.assertGreaterEqual(main._pending_replies[0].next_attempt_at, self.time + 60)

    async def test_short_retryafter_waits_once_then_succeeds(self):
        send = AsyncMock(side_effect=[RetryAfter(2), object()])
        with patch("main.asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.assertTrue(await main._safe_send(send, "text", "test", queue_on_failure=True))
        self.assertEqual(send.await_count, 2)
        self.assertGreaterEqual(sleep.await_args.args[0], 2)
        self.assertFalse(main._pending_replies)

    async def test_batch_rate_limit_never_falls_back_to_new_send_even_with_full_queue(self):
        with patch.object(main, "PENDING_REPLY_MAX_ITEMS", 1):
            await main._safe_send(
                AsyncMock(side_effect=_connect_failure()), "older receipt", "test", queue_on_failure=True
            )
            edit = AsyncMock(side_effect=RetryAfter(60))
            send = AsyncMock(return_value=SimpleNamespace(edit_text=edit))
            stub = SimpleNamespace(AddOfflineFiles=AsyncMock(return_value=SimpleNamespace(success=True)))
            await self._handle(
                "magnet:?xt=urn:btih:" + "A" * 40 + "\nmagnet:?xt=urn:btih:" + "B" * 40,
                send, stub,
            )
        self.assertEqual(edit.await_count, 1)
        self.assertEqual(send.await_count, 1, "不能通过新消息绕过 Telegram 限流")
        self.assertEqual(len(main._pending_replies), 1)

    async def test_flush_uses_bounded_batch_and_drops_permanent_failures(self):
        with patch.object(main, "PENDING_REPLY_BATCH_SIZE", 1):
            first = AsyncMock(side_effect=_connect_failure())
            second = AsyncMock(side_effect=_connect_failure())
            for send in (first, second):
                await main._safe_send(send, "text", "test", queue_on_failure=True)
            first.side_effect = BadRequest("chat not found")
            first_before, second_before = first.await_count, second.await_count
            await self._flush()
            self.assertEqual(first.await_count, first_before + 1)
            self.assertEqual(second.await_count, second_before)
            self.assertEqual(len(main._pending_replies), 1)

    async def test_cancelled_flush_propagates_and_preserves_pending_receipt(self):
        send = AsyncMock(side_effect=_connect_failure())
        await main._safe_send(send, "text", "test", queue_on_failure=True)
        send.side_effect = asyncio.CancelledError()
        self.time += main.PENDING_REPLY_INTERVAL_SECONDS + 1
        with self.assertRaises(asyncio.CancelledError):
            await main.flush_pending_replies(self._context())
        self.assertEqual(len(main._pending_replies), 1)

    async def test_post_init_registers_resend_on_builtin_jobqueue(self):
        # run_repeating 是同步注册 API，不能用 AsyncMock 让协程落空。
        from unittest.mock import Mock
        jobs = SimpleNamespace(run_repeating=Mock(), scheduler=SimpleNamespace(add_job=lambda *_: None))
        app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock()), job_queue=jobs)
        await main.post_init(app)
        callbacks = [call.args[0] for call in jobs.run_repeating.call_args_list]
        self.assertIn(main.flush_pending_replies, callbacks)

    async def test_shutdown_stops_queue_processing(self):
        send = AsyncMock(side_effect=_connect_failure())
        await main._safe_send(send, "text", "test", queue_on_failure=True)
        before = send.await_count
        self.time += main.PENDING_REPLY_INTERVAL_SECONDS + 1
        await main.flush_pending_replies(self._context(running=False))
        self.assertEqual(send.await_count, before)
        self.assertEqual(len(main._pending_replies), 1)

    async def test_expired_identical_receipt_can_be_queued_again(self):
        send = AsyncMock(side_effect=_connect_failure())
        await main._safe_send(send, "text", "test", queue_on_failure=True)
        self.time += main.PENDING_REPLY_TTL_SECONDS + 1
        await main._safe_send(send, "text", "test", queue_on_failure=True)
        self.assertEqual(len(main._pending_replies), 1)
        self.assertGreater(main._pending_replies[0].expires_at, self.time)
        send.side_effect = None
        before = send.await_count
        await self._flush()
        self.assertEqual(send.await_count, before + 1)
        self.assertFalse(main._pending_replies)

    async def test_concurrent_defer_during_flush_does_not_send_duplicate_receipt(self):
        send = AsyncMock(side_effect=_connect_failure())
        await main._safe_send(send, "text", "test", queue_on_failure=True)
        self.time += main.PENDING_REPLY_INTERVAL_SECONDS + 1
        started, finish = asyncio.Event(), asyncio.Event()

        async def delayed_send(*_args, **_kwargs):
            started.set()
            await finish.wait()
            return object()

        send.side_effect = delayed_send
        flushing = asyncio.create_task(main.flush_pending_replies(self._context()))
        try:
            await started.wait()
            # 同一回执补发仍在途时，再次 defer 不得注册第二项；第二个 Job 也不应并发发。
            main._queue_pending_reply(send, "text", "test", {})
            await main.flush_pending_replies(self._context())
            finish.set()
            await flushing
            before = send.await_count
            await self._flush()
            self.assertEqual(send.await_count, before)
            self.assertFalse(main._pending_replies)
        finally:
            finish.set()
            await flushing

    async def test_due_snapshot_obeys_rate_limit_added_while_previous_send_is_inflight(self):
        first = AsyncMock(side_effect=_connect_failure())
        second = AsyncMock(side_effect=_connect_failure())
        for send in (first, second):
            await main._safe_send(send, "text", "test", queue_on_failure=True)
        self.time += main.PENDING_REPLY_INTERVAL_SECONDS + 1
        started, finish = asyncio.Event(), asyncio.Event()

        async def delayed_first(*_args, **_kwargs):
            started.set()
            await finish.wait()
            return object()

        first.side_effect = delayed_first
        second.side_effect = None
        before = second.await_count
        flushing = asyncio.create_task(main.flush_pending_replies(self._context()))
        try:
            await started.wait()
            # due 快照已包含第二条，前台随后收到 429；后台必须遵守最新的等待期限。
            main._queue_pending_reply(second, "text", "test", {}, delay=60)
            finish.set()
            await flushing
            self.assertEqual(second.await_count, before)
            self.assertEqual(len(main._pending_replies), 1)
            self.time += 59
            await main.flush_pending_replies(self._context())
            self.assertEqual(second.await_count, before)
            self.time += 2
            await main.flush_pending_replies(self._context())
            self.assertEqual(second.await_count, before + 1)
            self.assertFalse(main._pending_replies)
        finally:
            finish.set()
            await flushing

    async def test_attempt_limit_stops_background_resends(self):
        send = AsyncMock(side_effect=_connect_failure())
        await main._safe_send(send, "text", "test", queue_on_failure=True)
        with patch.object(main, "PENDING_REPLY_MAX_ATTEMPTS", 1):
            await self._flush()
        self.assertFalse(main._pending_replies)


if __name__ == "__main__":
    unittest.main()
