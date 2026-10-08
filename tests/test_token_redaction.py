"""验证日志脱敏：Telegram Bot Token 不得出现在日志输出中。

背景：httpx 以 INFO 级别打印完整请求 URL，Bot Token 嵌在 URL 路径里
（https://api.telegram.org/bot<ID>:<KEY>/getUpdates），一旦随 docker logs
外泄，等同于明文泄露可完全接管机器人的凭据。
"""

import io
import logging
import unittest

import main


class TokenRedactionFilterTests(unittest.TestCase):
    # 形态与真实 Token 一致，但并非有效密钥
    FAKE_TOKEN = "1234567890:AAFakeTokenForTestingOnly_abcdefghij"
    FAKE_URL = f"https://api.telegram.org/bot{FAKE_TOKEN}/getUpdates"

    def setUp(self):
        self.redactor = main.TokenRedactionFilter()

    def _make_record(self, msg, args=()):
        return logging.LogRecord(
            name="httpx",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=args,
            exc_info=None,
        )

    def test_lazy_formatted_url_is_redacted(self):
        """httpx 的真实形态是「模板 + args」，脱敏后排查信息必须保留。"""
        record = self._make_record(
            'HTTP Request: %s %s "%s %d %s"',
            ("POST", self.FAKE_URL, "HTTP/1.1", 200, "OK"),
        )
        self.redactor.filter(record)
        text = record.getMessage()

        self.assertNotIn(self.FAKE_TOKEN, text, "Token 不得出现在日志中")
        self.assertIn("bot<TOKEN已脱敏>", text)
        # 脱敏后仍要能看出请求了哪个接口、结果如何
        self.assertIn("HTTP Request", text)
        self.assertIn("getUpdates", text)
        self.assertIn("200", text)

    def test_bare_string_message_is_redacted(self):
        record = self._make_record(f"调用失败: {self.FAKE_URL}")
        self.redactor.filter(record)
        self.assertNotIn(self.FAKE_TOKEN, record.getMessage())

    def test_plain_message_is_untouched(self):
        """不含 Telegram URL 的普通日志不得被改动。"""
        record = self._make_record("🐶 轮询看门狗已启动，检查周期 %s 秒。", (60,))
        self.redactor.filter(record)
        self.assertEqual(record.getMessage(), "🐶 轮询看门狗已启动，检查周期 60 秒。")

    def test_non_telegram_url_is_untouched(self):
        """内网地址里出现 bot 字样也不应被误伤。"""
        record = self._make_record(
            "HTTP Request: GET http://192.168.31.224:19798/botinfo"
        )
        self.redactor.filter(record)
        self.assertEqual(
            record.getMessage(), "HTTP Request: GET http://192.168.31.224:19798/botinfo"
        )

    def test_filter_never_drops_records(self):
        """脱敏不能变成屏蔽，日志必须照常输出。"""
        self.assertTrue(self.redactor.filter(self._make_record("任意消息")))

    def test_filter_is_attached_to_root_handlers(self):
        """模块导入后，根 handler 上必须已挂载脱敏过滤器。

        过滤器挂在 handler 而非 logger 上，因为子 logger 向上传播时
        不会经过 root logger 的 filter。
        """
        handlers = logging.getLogger().handlers
        self.assertTrue(handlers, "basicConfig 应已为根 logger 配置 handler")
        for handler in handlers:
            self.assertTrue(
                any(isinstance(f, main.TokenRedactionFilter) for f in handler.filters),
                f"handler {handler!r} 缺少 TokenRedactionFilter",
            )

    def test_end_to_end_child_logger_output_has_no_token(self):
        """端到端：子 logger 经 handler 输出后，文本中不得含 Token。"""
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.addFilter(main.TokenRedactionFilter())

        child = logging.getLogger("httpx.e2e_child")
        child.propagate = False
        child.setLevel(logging.INFO)
        child.addHandler(handler)
        try:
            child.info(
                'HTTP Request: %s %s "%s %d %s"',
                "POST",
                self.FAKE_URL,
                "HTTP/1.1",
                200,
                "OK",
            )
        finally:
            child.removeHandler(handler)

        output = stream.getvalue()
        self.assertNotIn(self.FAKE_TOKEN, output)
        self.assertIn("bot<TOKEN已脱敏>", output)


if __name__ == "__main__":
    unittest.main()
