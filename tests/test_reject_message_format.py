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


if __name__ == "__main__":
    unittest.main()
