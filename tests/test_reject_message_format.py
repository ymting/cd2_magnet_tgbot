"""回归测试：CD2 拒绝请求时，给用户的文案必须是人话。

背景（2026-10-09）：
    用户重复提交同一个磁力链接后，收到的是原样转发的 CD2 errorMessage ——
    夹带云盘 API 原文、错误码等开发者措辞，看不懂也没必要看。
    现改为按关键词归类成简短提示，其中最常见的一类就是「重复提交」。
    约束：简化只针对用户侧文案，完整原文必须留在日志里。
"""

import unittest

import main


class DuplicateHintTests(unittest.TestCase):
    def test_chinese_duplicate_hint(self):
        for raw in ("任务已存在", "添加离线下载任务失败: 该任务已存在", "链接已经添加过了", "重复的任务"):
            with self.subTest(raw=raw):
                self.assertEqual(main._friendly_reject_reason(raw), main.DUPLICATE_REPLY)

    def test_english_duplicate_hint_is_case_insensitive(self):
        for raw in (
            "Task Already Exists",
            "duplicate task detected",
            "Add offline file failed: file already exists",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(main._friendly_reject_reason(raw), main.DUPLICATE_REPLY)

    def test_duplicate_wins_over_other_hints(self):
        """重复提交本身也是 CloudDrive2 的业务拒绝，优先级必须高于其它归类。"""
        raw = "duplicate task, token not needed"
        self.assertEqual(main._friendly_reject_reason(raw), main.DUPLICATE_REPLY)


class OtherReasonTests(unittest.TestCase):
    def test_auth_failure_is_explained(self):
        reason = main._friendly_reject_reason("Unauthenticated: invalid token")
        self.assertIn("CD2_TOKEN", reason)

    def test_unsupported_link_is_explained(self):
        reason = main._friendly_reject_reason("cloud 115 does not support this url")
        self.assertIn("不支持", reason)

    def test_empty_message_has_fallback(self):
        for raw in ("", "   ", None):
            with self.subTest(raw=raw):
                self.assertIn("CD2 未说明原因", main._friendly_reject_reason(raw))

    def test_unknown_error_is_shortened_not_dropped(self):
        """识别不出类别时：去掉动作前缀 + 截断，但保留可读的关键信息。"""
        long_tail = "x" * 300
        reason = main._friendly_reject_reason(f"添加离线下载任务失败: {long_tail}")
        self.assertTrue(reason.startswith("❌ 提交失败："))
        self.assertNotIn("添加离线下载任务失败:", reason)
        self.assertTrue(reason.endswith("…"))
        # 前缀 12 字符 + 截断上限 80 + "…"，整体应远短于原文
        self.assertLess(len(reason), 100)

    def test_short_unknown_error_is_kept_verbatim(self):
        reason = main._friendly_reject_reason("云盘配额不足")
        self.assertEqual(reason, "❌ 提交失败：云盘配额不足")


class SubmitFailureDescriptionTests(unittest.TestCase):
    """异常路径（gRPC 抛错）的文案：业务拒绝与传输故障必须区分开。

    2026-10-09 生产实测：115open 对重复链接返回的是 gRPC 异常
    （StatusCode.INTERNAL + code 10008 + 「任务已存在，请勿输入重复的链接地址」），
    不是 res.success=False。上一版只覆盖了后者，用户看到的还是整段 AioRpcError repr。
    """

    REAL_DUPLICATE_DETAILS = (
        "api error Cloud 115open(5975675) api error: "
        "code: 10008, message: 任务已存在，请勿输入重复的链接地址"
    )

    class _RpcError(Exception):
        """带 code()/details() 的最小 AioRpcError 替身，str() 模拟多行 repr。"""

        def __init__(self, code_name, details):
            self._code = code_name
            self._details = details
            super().__init__(
                "<AioRpcError of RPC that terminated with:\n"
                f'status = {code_name}\ndetails = "{details}"\n'
                f'debug_error_string = "INTERNAL:{details}"\n>'
            )

        def code(self):
            return self._code

        def details(self):
            return self._details

    def _rpc_error(self, code_name, details):
        return self._RpcError(code_name, details)

    def test_business_rejection_via_exception_is_friendly(self):
        error = self._rpc_error("StatusCode.INTERNAL", self.REAL_DUPLICATE_DETAILS)
        self.assertEqual(main._describe_submit_failure(error), main.DUPLICATE_REPLY)

    def test_transport_failure_still_says_connection_error(self):
        error = self._rpc_error("StatusCode.UNAVAILABLE", "failed to connect to all addresses")
        message = main._describe_submit_failure(error)
        self.assertIn("CD2 连接异常", message)

    def test_internal_error_is_not_called_connection_error(self):
        """INTERNAL 不等于连不上；未归类也应如实说失败，不能误导成连接问题。"""
        error = self._rpc_error("StatusCode.INTERNAL", "api error: code 500, message server exploded")
        message = main._describe_submit_failure(error)
        self.assertNotIn("连接异常", message)
        self.assertIn("提交失败", message)
        self.assertIn("server exploded", message)

    def test_no_multiline_repr_leaks_to_user(self):
        """多行 repr 与 debug_error_string 绝不能出现在用户消息里。"""
        error = self._rpc_error("StatusCode.INTERNAL", "api error: something unknown happened")
        message = main._describe_submit_failure(error)
        self.assertNotIn("\n", message)
        self.assertNotIn("AioRpcError of RPC", message)
        self.assertNotIn("debug_error_string", message)
        self.assertLessEqual(len(message), 140)

    def test_plain_exception_is_shortened(self):
        message = main._describe_submit_failure(ValueError("x" * 500))
        self.assertIn("ValueError", message)
        self.assertLess(len(message), 140)

    def test_grpc_details_preferred_over_repr(self):
        error = self._rpc_error("StatusCode.INTERNAL", self.REAL_DUPLICATE_DETAILS)
        self.assertEqual(main._grpc_error_raw(error), self.REAL_DUPLICATE_DETAILS)

    def test_transport_detection_falls_back_to_text(self):
        """非 gRPC 异常没有 code() 属性，靠文本判断也不能漏掉连接类故障。"""
        self.assertTrue(main._is_transport_failure(Exception("Connection refused")))
        self.assertFalse(main._is_transport_failure(Exception("api error: bad request")))


if __name__ == "__main__":
    unittest.main()
