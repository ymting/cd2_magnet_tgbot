"""回归测试：提交链接时，CD2(gRPC) 故障与 Telegram 回执故障必须分开归因。

背景（2026-10-09 生产日志排查）：
    用户发送磁力链接后收到「❌ 提交失败，CD2 连接异常: httpx.ConnectError:」，
    但云盘里任务其实已经提交成功。

    根因是 handle_link 用一个 `except Exception` 同时包住了
      (a) 提交到 CD2 的 gRPC 调用
      (b) 给用户回执的 Telegram 调用
    回执走 Telegram(经代理)，抛出的 httpx.ConnectError 被 PTB 包装为
    NetworkError("httpx.ConnectError: ")，随即被当成「CD2 连接异常」上报，
    而且旧代码链路里没有任何日志，现场完全无法回溯。
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import NetworkError

import main


class FakeAioRpcError(Exception):
    """模拟 grpc.aio.AioRpcError，用于验证错误类型名会被带上。"""


class FakeRpcErrorWithDetails(Exception):
    """更接近真实的 AioRpcError：带 code() 与 details()，str() 是多行 repr。

    2026-10-09 生产实测：115open 对重复链接返回的是
        StatusCode.INTERNAL + "api error Cloud 115open(5975675) api error:
        code: 10008, message: 任务已存在，请勿输入重复的链接地址"
    它是**抛异常**回来的，不是 success=False，所以必须走异常路径的归类。
    """

    def __init__(self, code_name: str, details: str):
        self._code = code_name
        self._details = details
        super().__init__(
            "<AioRpcError of RPC that terminated with:\n"
            f'status = {code_name}\n'
            f'details = "{details}"\n'
            f'debug_error_string = "INTERNAL:{details}"\n>'
        )

    def code(self):
        return self._code

    def details(self):
        return self._details


# 用户 2026-10-09 在 bot 上收到的真实报错（原文照抄）
REAL_DUPLICATE_DETAILS = (
    "api error Cloud 115open(5975675) api error: "
    "code: 10008, message: 任务已存在，请勿输入重复的链接地址"
)


class _FakeChannel:
    """模拟 grpc.aio.insecure_channel 返回的异步上下文管理器。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _make_update(text: str = "magnet:?xt=urn:btih:" + "A" * 40, reply_side_effect=None):
    """构造一个最小可用的 Update 替身。"""
    reply_text = AsyncMock(side_effect=reply_side_effect)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        message=SimpleNamespace(text=text, reply_text=reply_text),
    )
    return update, reply_text


class SubmitErrorAttributionTests(unittest.TestCase):
    def setUp(self):
        self._original_admin_ids = main.ADMIN_IDS
        self._original_retry_delay = main.REPLY_RETRY_DELAY_SECONDS
        main.ADMIN_IDS = [123]
        # 测试里不需要真的等待退避
        main.REPLY_RETRY_DELAY_SECONDS = 0

    def tearDown(self):
        main.ADMIN_IDS = self._original_admin_ids
        main.REPLY_RETRY_DELAY_SECONDS = self._original_retry_delay

    def _run_with_stub(self, stub, update):
        """在伪 gRPC 通道下执行 handle_link。"""
        with patch("main.grpc.aio.insecure_channel", return_value=_FakeChannel()), patch(
            "main.clouddrive_pb2_grpc.CloudDriveFileSrvStub", return_value=stub
        ):
            asyncio.run(main.handle_link(update, None))

    def test_cd2_failure_is_reported_as_cd2_error(self):
        """阶段一失败：只有这种情况才允许说「CD2 连接异常」。"""
        stub = SimpleNamespace(
            AddOfflineFiles=AsyncMock(
                side_effect=FakeAioRpcError("StatusCode.UNAVAILABLE, failed to connect to all addresses")
            )
        )
        update, reply_text = _make_update()

        with self.assertLogs("main", level="ERROR") as logs:
            self._run_with_stub(stub, update)

        self.assertEqual(reply_text.await_count, 1)
        message = reply_text.await_args.args[0]
        self.assertIn("CD2 连接异常", message)
        # 错误类型名要带上，否则用户只看到一句没有信息量的「连接异常」
        self.assertIn("FakeAioRpcError", message)
        self.assertTrue(
            any("提交 CD2 离线下载失败" in line for line in logs.output),
            f"应留下可排查的异常日志，实际: {logs.output}",
        )

    def test_reply_failure_after_success_never_blames_cd2(self):
        """阶段二首次回执失败：重试后必须给用户「提交成功」，绝不能报 CD2 异常。"""
        stub = SimpleNamespace(
            AddOfflineFiles=AsyncMock(return_value=SimpleNamespace(success=True, errorMessage=""))
        )
        # 第一次发送撞上代理/Telegram 瞬时故障，第二次恢复
        update, reply_text = _make_update(
            reply_side_effect=[NetworkError("httpx.ConnectError: "), None]
        )

        self._run_with_stub(stub, update)

        self.assertEqual(reply_text.await_count, 2)
        for call in reply_text.await_args_list:
            text = call.args[0]
            self.assertNotIn("CD2 连接异常", text)
            self.assertNotIn("提交失败", text)
        self.assertIn("提交成功", reply_text.await_args_list[-1].args[0])

    def test_reply_failure_is_retried_and_logged(self):
        """回执始终发不出去：按上限重试，并明确记录「任务已提交成功」。"""
        stub = SimpleNamespace(
            AddOfflineFiles=AsyncMock(return_value=SimpleNamespace(success=True, errorMessage=""))
        )
        update, reply_text = _make_update(
            reply_side_effect=NetworkError("httpx.ConnectError: ")
        )

        with self.assertLogs("main", level="ERROR") as logs:
            self._run_with_stub(stub, update)

        self.assertEqual(reply_text.await_count, main.REPLY_MAX_ATTEMPTS)
        self.assertTrue(
            any("成功回执未能送达用户" in line for line in logs.output),
            f"回执丢失必须留日志，实际: {logs.output}",
        )

    def test_duplicate_via_rpc_exception_is_friendly(self):
        """真实场景：115open 把「重复提交」以 gRPC INTERNAL 异常抛出。

        这条路径上一版漏掉了 —— 用户看到的仍是 AioRpcError 的整段 repr。
        """
        stub = SimpleNamespace(
            AddOfflineFiles=AsyncMock(
                side_effect=FakeRpcErrorWithDetails("StatusCode.INTERNAL", REAL_DUPLICATE_DETAILS)
            )
        )
        update, reply_text = _make_update()

        with self.assertLogs("main", level="ERROR") as logs:
            self._run_with_stub(stub, update)

        self.assertEqual(reply_text.await_count, 1)
        message = reply_text.await_args.args[0]
        self.assertEqual(message, main.DUPLICATE_REPLY)
        # 三样都不能再出现：多行 repr、debug_error_string、误报的「连接异常」
        self.assertNotIn("AioRpcError", message)
        self.assertNotIn("debug_error_string", message)
        self.assertNotIn("连接异常", message)
        # 原始细节必须留在日志里
        self.assertTrue(
            any("任务已存在" in line for line in logs.output),
            f"原始 details 应写进日志，实际: {logs.output}",
        )

    def test_duplicate_rejection_is_friendly(self):
        """CD2 判定重复提交：转成人话，不把技术性 errorMessage 甩给用户。"""
        stub = SimpleNamespace(
            AddOfflineFiles=AsyncMock(
                return_value=SimpleNamespace(success=False, errorMessage="任务已存在")
            )
        )
        update, reply_text = _make_update()

        with self.assertLogs("main", level="WARNING") as logs:
            self._run_with_stub(stub, update)

        # 简化的是用户侧文案，排查用的原始 errorMessage 必须完整留在日志里
        self.assertTrue(
            any("任务已存在" in line for line in logs.output),
            f"原始 errorMessage 应写进日志，实际: {logs.output}",
        )
        self.assertEqual(reply_text.await_count, 1)
        message = reply_text.await_args.args[0]
        self.assertEqual(message, main.DUPLICATE_REPLY)
        # 原始 errorMessage 不得出现在用户看到的文案里
        self.assertNotIn("任务已存在", message)
        # 但也绝不能伪装成连接异常
        self.assertNotIn("连接异常", message)

    def test_non_cd2_success_still_replies_success(self):
        """正常路径不能被重构破坏。"""
        stub = SimpleNamespace(
            AddOfflineFiles=AsyncMock(return_value=SimpleNamespace(success=True, errorMessage=""))
        )
        update, reply_text = _make_update()

        self._run_with_stub(stub, update)

        self.assertEqual(reply_text.await_count, 1)
        self.assertIn("提交成功", reply_text.await_args.args[0])


class SafeSendTests(unittest.TestCase):
    def setUp(self):
        self._original_retry_delay = main.REPLY_RETRY_DELAY_SECONDS
        main.REPLY_RETRY_DELAY_SECONDS = 0

    def tearDown(self):
        main.REPLY_RETRY_DELAY_SECONDS = self._original_retry_delay

    def test_non_network_error_is_not_retried(self):
        """非网络异常（如 Markdown 解析失败）重试无意义，只尝试一次。"""
        send = AsyncMock(side_effect=ValueError("can't parse entities"))
        with self.assertLogs("main", level="ERROR"):
            result = asyncio.run(main._safe_send(send, "hello", description="测试消息"))
        self.assertFalse(result)
        self.assertEqual(send.await_count, 1)

    def test_network_error_is_retried_until_success(self):
        send = AsyncMock(side_effect=[NetworkError("httpx.ConnectError: "), None, None])
        result = asyncio.run(main._safe_send(send, "hello", description="测试消息"))
        self.assertTrue(result)
        self.assertEqual(send.await_count, 2)

    def test_all_attempts_exhausted_returns_false(self):
        send = AsyncMock(side_effect=NetworkError("httpx.ConnectError: "))
        with self.assertLogs("main", level="ERROR"):
            result = asyncio.run(main._safe_send(send, "hello", description="测试消息"))
        self.assertFalse(result)
        self.assertEqual(send.await_count, main.REPLY_MAX_ATTEMPTS)

    def test_send_result_is_returned_to_caller(self):
        """批量提交需要拿到消息对象才能把进度消息 edit 成最终报告，返回值必须透传。"""
        message = object()
        send = AsyncMock(return_value=message)
        self.assertIs(asyncio.run(main._safe_send(send, "hello", description="测试消息")), message)


class MaskLinkTests(unittest.TestCase):
    def test_short_link_is_kept(self):
        self.assertEqual(main._mask_link("magnet:?xt=urn:btih:AAA"), "magnet:?xt=urn:btih:AAA")

    def test_long_link_is_truncated(self):
        long_link = "magnet:?xt=urn:btih:" + "B" * 300
        masked = main._mask_link(long_link, limit=40)
        self.assertTrue(masked.startswith("magnet:?xt=urn:btih:"))
        self.assertIn("共 320 字符", masked)
        self.assertLess(len(masked), len(long_link))


if __name__ == "__main__":
    unittest.main()
