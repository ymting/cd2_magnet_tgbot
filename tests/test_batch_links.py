"""回归测试：一条消息里混合粘贴多个链接时，必须逐个提交并汇总回执。

背景（2026-10-09）：
    用户日常习惯一次把攒好的链接全贴进来（磁力 / ed2k / http 混在一起）。
    旧实现要求整条消息以链接开头（`text.startswith(...)`），因此
    「1. magnet:... 2. ed2k://...」这种带序号的列表会被整条丢弃，一个都提交不了；
    即便手动去掉序号，也只有第一个链接会被提交（整个文本被当成一个 urls 参数）。

本版行为：
    - 用正则从正文里提取**所有**链接，允许序号、项目符号与说明文字混排；
    - 逐个提交（每个链接单独走一次 AddOfflineFiles），因此每条结果的成败可以精确定位；
    - 结果汇总成一条纯文本报告，失败原因沿用已归类的人话文案。
"""

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Chat, Message, MessageEntity, Update
from telegram.ext import filters

import main


class _FakeChannel:
    """模拟 grpc.aio.insecure_channel 返回的异步上下文管理器。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


MAGNET_A = "magnet:?xt=urn:btih:" + "A" * 40
MAGNET_B = "magnet:?xt=urn:btih:" + "B" * 40
ED2K = "ed2k://|file|%E6%B5%8B%E8%AF%95.avi|1073741824|31D6CFE0D16AE931B73C59D7E0C089C0|/"
HTTP_LINK = "https://example.com/video.mp4"


class ExtractLinksTests(unittest.TestCase):
    def test_single_magnet(self):
        self.assertEqual(main._extract_links(MAGNET_A), [MAGNET_A])

    def test_numbered_list_with_mixed_schemes(self):
        """带序号的混排列表：旧实现会整条丢弃，这是本版要修的回归点。"""
        text = f"1. {MAGNET_A}\n2. {ED2K}\n3. {HTTP_LINK}"
        self.assertEqual(main._extract_links(text), [MAGNET_A, ED2K, HTTP_LINK])

    def test_links_embedded_in_prose(self):
        """正文里夹带的链接也要能识别（例如「这两个先下 magnet:... 和 ed2k://...」）。"""
        text = f"这两个先下 {MAGNET_A} 和 {ED2K} 谢谢"
        self.assertEqual(main._extract_links(text), [MAGNET_A, ED2K])

    def test_duplicates_are_removed_keeping_order(self):
        text = f"{MAGNET_A}\n{MAGNET_B}\n{MAGNET_A}"
        self.assertEqual(main._extract_links(text), [MAGNET_A, MAGNET_B])

    def test_trailing_chinese_punctuation_is_stripped(self):
        """中文标点紧跟链接（中间无空格）时必须截断，否则正文会被吞进链接。"""
        text = f"链接：{MAGNET_A}。还有{ED2K}；另一个{HTTP_LINK}）"
        self.assertEqual(main._extract_links(text), [MAGNET_A, ED2K, HTTP_LINK])

    def test_ed2k_with_raw_chinese_filename_is_kept(self):
        """ed2k 的 |file| 段常直接写中文，不能在汉字处断开。"""
        link = "ed2k://|file|某部电影.avi|1073741824|31D6CFE0D16AE931B73C59D7E0C089C0|/"
        self.assertEqual(main._extract_links(f"{link} 这个不错"), [link])

    def test_scheme_is_lowercased_but_body_is_kept(self):
        """用户手打的 Magnet: / HTTPS:// 要能识别；但 dn 参数的大小写不能被改掉。"""
        text = "Magnet:?xt=urn:btih:" + "C" * 40 + "&dn=MyFile.MKV"
        self.assertEqual(
            main._extract_links(text),
            ["magnet:?xt=urn:btih:" + "C" * 40 + "&dn=MyFile.MKV"],
        )

    def test_url_ending_with_parenthesis_is_not_broken(self):
        """ASCII 右括号可能是 URL 的一部分（维基条目名），不能被当成句尾标点剥掉。"""
        link = "https://zh.wikipedia.org/wiki/Foo_(bar)"
        self.assertEqual(main._extract_links(f"见 {link}"), [link])

    def test_bare_scheme_is_ignored(self):
        """正文里的裸 "https://" 不是链接，不能触发一次注定失败的提交。"""
        self.assertEqual(main._extract_links("把 http:// 换成 https:// 就好了"), [])

    def test_no_links_returns_empty(self):
        for text in ("", "   ", None, "今天天气不错"):
            with self.subTest(text=text):
                self.assertEqual(main._extract_links(text), [])


class LinkPasteFormatTests(unittest.TestCase):
    """各种真实粘贴格式的解析回归。

    最常见的用法是「一行一个链接」直接贴进来，所以换行类格式是主场景；
    从网页/Markdown 复制时还会带上各种包裹符号，必须一并处理干净，
    否则脏链接会被 CD2 判为非法，用户看到莫名的失败提示。
    """

    def test_newline_separated_variants(self):
        cases = {
            "LF": f"{MAGNET_A}\n{ED2K}\n{HTTP_LINK}",
            "CRLF": f"{MAGNET_A}\r\n{ED2K}\r\n{HTTP_LINK}",
            "空行": f"{MAGNET_A}\n\n{ED2K}\n\n{HTTP_LINK}",
            "序号": f"1. {MAGNET_A}\n2. {ED2K}\n3. {HTTP_LINK}",
            "半角连字符": f"- {MAGNET_A}\n- {ED2K}\n- {HTTP_LINK}",
            "项目符号": f"• {MAGNET_A}\n• {ED2K}\n• {HTTP_LINK}",
            "制表符": f"{MAGNET_A}\t{ED2K}\t{HTTP_LINK}",
            "全角空格": f"{MAGNET_A}\u3000{ED2K}\u3000{HTTP_LINK}",
            "行尾标点": f"{MAGNET_A}。\n{ED2K}；\n{HTTP_LINK}）",
            "行内说明": f"{MAGNET_A} 这个先下\n{ED2K} 这个备用\n{HTTP_LINK} 这个是直链",
        }
        for name, text in cases.items():
            with self.subTest(format=name):
                self.assertEqual(main._extract_links(text), [MAGNET_A, ED2K, HTTP_LINK])

    def test_wrapped_links_are_cleaned(self):
        """被反引号 / 方括号 / 圆括号包住的链接，包裹符号不能粘进链接。"""
        cases = {
            "单反引号": f"`{MAGNET_A}`",
            "三反引号代码块": f"```\n{MAGNET_A}\n{ED2K}\n```",
            "方括号": f"[{MAGNET_A}]",
            "Markdown 链接": f"[磁力链接]({MAGNET_A})",
            "圆括号": f"({MAGNET_A})",
            "尖括号": f"<{MAGNET_A}>",
            "包裹 + 行尾标点": f"[{MAGNET_A}]。",
        }
        for name, text in cases.items():
            with self.subTest(format=name):
                links = main._extract_links(text)
                self.assertIn(MAGNET_A, links, f"{name} 应提取出干净的链接，实际: {links}")

    def test_triple_backtick_glued_to_link(self):
        """代码块围栏紧贴链接（没有换行）时，末尾的反引号也要剥干净。"""
        self.assertEqual(main._extract_links("`" + MAGNET_A + "```"), [MAGNET_A])

    def test_zero_width_characters_are_removed_not_treated_as_separators(self):
        """零宽字符是噪音不是分隔符：删掉后链接必须仍然完整。"""
        dirty = "magnet:?xt=urn:\u200bbtih:" + "E" * 40 + "\ufeff"
        self.assertEqual(main._extract_links(dirty), ["magnet:?xt=urn:btih:" + "E" * 40])

    def test_url_own_brackets_survive(self):
        """URL 自身的括号不能被当成包裹剥掉（否则维基条目名、IPv6 地址会失效）。"""
        cases = {
            "维基条目名": "https://zh.wikipedia.org/wiki/Foo_(bar)",
            "IPv6 主机": "http://[::1]:8080/index.m3u",
        }
        for name, link in cases.items():
            with self.subTest(case=name):
                self.assertEqual(main._extract_links(link), [link])


def _make_update(text=None, caption=None, entities=None, caption_entities=None):
    """批量路径需要 reply_text 返回一个「能被 edit_text」的消息对象。"""
    edit_text = AsyncMock()
    reply_text = AsyncMock(return_value=SimpleNamespace(edit_text=edit_text))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        message=SimpleNamespace(
            text=text,
            caption=caption,
            entities=entities,
            caption_entities=caption_entities,
            reply_text=reply_text,
        ),
    )
    return update, reply_text, edit_text


def _make_message(text=None, caption=None, entities=None, caption_entities=None):
    return SimpleNamespace(
        text=text, caption=caption, entities=entities, caption_entities=caption_entities
    )


def _text_link(url, display="点此下载"):
    """构造一个 text_link 实体（正文里只有显示文字，地址藏在 url 里）。"""
    return SimpleNamespace(type="text_link", url=url, offset=0, length=len(display))


class ForwardedMessageTests(unittest.TestCase):
    """转发来的消息有三种形态，必须都能识别 —— 用户实际就是这么用的。

    1. 纯文本：转发不改写正文，`text` 原样保留（本来就支持）；
    2. 媒体帖 + 说明文字：链接在 `caption` 里、`text` 为 None —— **旧实现只读 text，
       这类转发完全没反应**，而转发种子/资源帖最常见的就是这个形态；
    3. 超链接实体：正文只有「点此下载」，地址在 `entity.url` 里，纯文本解析看不到。
    """

    def test_plain_text_is_collected(self):
        links = main._collect_message_links(_make_message(text=MAGNET_A))
        self.assertEqual(links, [MAGNET_A])

    def test_caption_only_message_is_collected(self):
        links = main._collect_message_links(_make_message(text=None, caption=MAGNET_A))
        self.assertEqual(links, [MAGNET_A])

    def test_caption_batch_is_collected(self):
        links = main._collect_message_links(
            _make_message(text=None, caption=f"{MAGNET_A}\n{ED2K}\n{HTTP_LINK}")
        )
        self.assertEqual(links, [MAGNET_A, ED2K, HTTP_LINK])

    def test_text_link_entity_magnet_is_collected(self):
        message = _make_message(text="点此下载", entities=[_text_link(MAGNET_A)])
        self.assertEqual(main._collect_message_links(message), [MAGNET_A])

    def test_text_link_entity_ed2k_is_collected(self):
        message = _make_message(text="点此下载", entities=[_text_link(ED2K)])
        self.assertEqual(main._collect_message_links(message), [ED2K])

    def test_caption_entities_are_also_checked(self):
        """说明文字里的超链接挂在 caption_entities 上，不能只看 entities。"""
        message = _make_message(caption="点此下载", caption_entities=[_text_link(MAGNET_A)])
        self.assertEqual(main._collect_message_links(message), [MAGNET_A])

    def test_http_text_link_is_ignored(self):
        """http(s) 超链接多半是频道/群组/广告，不能被当成下载链接提交。"""
        message = _make_message(
            text="加入频道", entities=[_text_link("https://t.me/some_channel")]
        )
        self.assertEqual(main._collect_message_links(message), [])

    def test_text_and_entity_duplicates_are_merged(self):
        """同一条链接同时出现在正文与实体里时只提交一次，否则会白挨一次「重复提交」。"""
        message = _make_message(text=MAGNET_A, entities=[_text_link(MAGNET_A)])
        self.assertEqual(main._collect_message_links(message), [MAGNET_A])

    def test_message_without_any_link_is_empty(self):
        for message in (
            _make_message(text="今天天气不错"),
            _make_message(text=None, caption=None),
        ):
            with self.subTest(message=message):
                self.assertEqual(main._collect_message_links(message), [])

    def test_caption_only_message_reaches_cd2_end_to_end(self):
        """端到端：转发一条「媒体 + 说明文字」的消息，链接要真的提交到 CD2。"""
        self._original_admin_ids = main.ADMIN_IDS
        main.ADMIN_IDS = [123]
        try:
            stub = SimpleNamespace(
                AddOfflineFiles=AsyncMock(
                    return_value=SimpleNamespace(success=True, errorMessage="")
                )
            )
            update, reply_text, _ = _make_update(text=None, caption=MAGNET_A)
            with patch("main.grpc.aio.insecure_channel", return_value=_FakeChannel()), patch(
                "main.clouddrive_pb2_grpc.CloudDriveFileSrvStub", return_value=stub
            ):
                asyncio.run(main.handle_link(update, None))

            self.assertEqual(stub.AddOfflineFiles.await_count, 1)
            self.assertEqual(stub.AddOfflineFiles.await_args.args[0].urls, MAGNET_A)
            self.assertIn("提交成功", reply_text.await_args.args[0])
        finally:
            main.ADMIN_IDS = self._original_admin_ids


class MessageFilterTests(unittest.TestCase):
    """处理器过滤器的回归：只写 filters.TEXT 会让转发媒体帖被静默挡在门外。"""

    _CHAT = Chat(id=1, type="private")
    _NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)

    def _update(self, **kwargs):
        return Update(
            update_id=1,
            message=Message(message_id=1, date=self._NOW, chat=self._CHAT, **kwargs),
        )

    def test_text_and_caption_are_both_accepted(self):
        self.assertTrue(main.LINK_MESSAGE_FILTER.filter(self._update(text=MAGNET_A)))
        self.assertTrue(main.LINK_MESSAGE_FILTER.filter(self._update(caption=ED2K)))

    def test_commands_are_still_rejected(self):
        update = self._update(
            text="/clean",
            entities=[MessageEntity(type="bot_command", offset=0, length=6)],
        )
        self.assertFalse(main.LINK_MESSAGE_FILTER.filter(update))

    def test_text_only_filter_would_drop_caption_messages(self):
        """回归断言：这正是旧实现的缺陷 —— 只写 filters.TEXT 时 caption 消息进不来。"""
        message = Message(message_id=1, date=self._NOW, chat=self._CHAT, caption=ED2K)
        self.assertFalse(filters.TEXT.filter(message))
        self.assertTrue(filters.CAPTION.filter(message))


class BatchSubmitTests(unittest.TestCase):
    def setUp(self):
        self._original_admin_ids = main.ADMIN_IDS
        main.ADMIN_IDS = [123]

    def tearDown(self):
        main.ADMIN_IDS = self._original_admin_ids

    def _run(self, stub, text):
        update, reply_text, edit_text = _make_update(text)
        with patch("main.grpc.aio.insecure_channel", return_value=_FakeChannel()), patch(
            "main.clouddrive_pb2_grpc.CloudDriveFileSrvStub", return_value=stub
        ):
            asyncio.run(main.handle_link(update, None))
        return reply_text, edit_text

    def test_all_success_reports_summary(self):
        stub = SimpleNamespace(
            AddOfflineFiles=AsyncMock(return_value=SimpleNamespace(success=True, errorMessage=""))
        )
        reply_text, edit_text = self._run(
            stub, f"{MAGNET_A}\n{ED2K}\n{HTTP_LINK}"
        )

        # 每个链接单独提交一次，且每次报文里只有一个链接（不能把整段文本当成 urls）
        self.assertEqual(stub.AddOfflineFiles.await_count, 3)
        self.assertEqual(
            [call.args[0].urls for call in stub.AddOfflineFiles.await_args_list],
            [MAGNET_A, ED2K, HTTP_LINK],
        )

        # 进度消息只发一次，结果编辑到同一条消息上
        self.assertEqual(reply_text.await_count, 1)
        self.assertEqual(edit_text.await_count, 1)

        report = edit_text.await_args.args[0]
        self.assertIn("成功 3 / 失败 0（共 3 个链接）", report)
        self.assertEqual(report.count("✅"), 3)
        self.assertIn("1. ✅", report)
        self.assertIn("3. ✅", report)

    def test_mixed_result_keeps_friendly_reason_and_no_raw_error(self):
        stub = SimpleNamespace(
            AddOfflineFiles=AsyncMock(
                side_effect=[
                    SimpleNamespace(success=True, errorMessage=""),
                    SimpleNamespace(success=False, errorMessage="任务已存在"),
                    SimpleNamespace(success=True, errorMessage=""),
                ]
            )
        )
        with self.assertLogs("main", level="WARNING") as logs:
            _, edit_text = self._run(stub, f"{MAGNET_A}\n{ED2K}\n{HTTP_LINK}")

        report = edit_text.await_args.args[0]
        self.assertIn("成功 2 / 失败 1（共 3 个链接）", report)
        self.assertIn("2. ❌", report)
        self.assertIn(main.DUPLICATE_REPLY, report)
        # 技术性原文只能进日志，不能出现在用户可见的报告里
        self.assertNotIn("任务已存在", report)
        self.assertTrue(
            any("任务已存在" in line for line in logs.output),
            f"原始 errorMessage 应写进日志，实际: {logs.output}",
        )

    def test_rpc_exception_in_one_link_does_not_abort_batch(self):
        """单条链接抛 gRPC 异常时，其余链接仍要继续提交。"""
        class _Boom(Exception):
            def code(self):
                return "StatusCode.UNAVAILABLE"

            def details(self):
                return "failed to connect to all addresses"

        stub = SimpleNamespace(
            AddOfflineFiles=AsyncMock(
                side_effect=[
                    SimpleNamespace(success=True, errorMessage=""),
                    _Boom("boom"),
                    SimpleNamespace(success=True, errorMessage=""),
                ]
            )
        )
        with self.assertLogs("main", level="ERROR"):
            _, edit_text = self._run(stub, f"{MAGNET_A}\n{ED2K}\n{HTTP_LINK}")

        report = edit_text.await_args.args[0]
        self.assertEqual(stub.AddOfflineFiles.await_count, 3)
        self.assertIn("成功 2 / 失败 1", report)
        self.assertIn("CD2 连接异常", report)

    def test_long_report_is_truncated_below_telegram_limit(self):
        """报告超长时必须主动截断：超限会抛 BadRequest，用户将什么都收不到。"""
        links = "\n".join(f"magnet:?xt=urn:btih:{index:040d}" for index in range(120))
        stub = SimpleNamespace(
            AddOfflineFiles=AsyncMock(return_value=SimpleNamespace(success=True, errorMessage=""))
        )
        _, edit_text = self._run(stub, links)

        report = edit_text.await_args.args[0]
        self.assertLess(len(report), 4096)
        self.assertIn("已省略", report)
        self.assertIn("成功 120 / 失败 0", report)

    def test_single_link_still_uses_original_message(self):
        """单链接路径不能被批量逻辑改写：文案与调用次数都要保持原样。"""
        stub = SimpleNamespace(
            AddOfflineFiles=AsyncMock(return_value=SimpleNamespace(success=True, errorMessage=""))
        )
        update, reply_text, edit_text = _make_update(MAGNET_A)
        with patch("main.grpc.aio.insecure_channel", return_value=_FakeChannel()), patch(
            "main.clouddrive_pb2_grpc.CloudDriveFileSrvStub", return_value=stub
        ):
            asyncio.run(main.handle_link(update, None))

        self.assertEqual(reply_text.await_count, 1)
        self.assertEqual(edit_text.await_count, 0)
        self.assertIn("提交成功", reply_text.await_args.args[0])


class BuildBatchReportTests(unittest.TestCase):
    def test_failure_line_carries_reason(self):
        report = main._build_batch_report([
            (MAGNET_A, main.SubmitOutcome(True, "✅ 提交成功", "提交成功回执")),
            (ED2K, main.SubmitOutcome(False, main.DUPLICATE_REPLY, "CD2 拒绝回执")),
        ])
        lines = report.splitlines()
        self.assertIn("1. ✅", lines[3])
        self.assertIn("2. ❌", lines[4])
        # 失败原因缩进挂在对应条目下，便于逐条对照
        self.assertIn(main.DUPLICATE_REPLY, lines[5])

    def test_markdown_characters_in_link_are_safe(self):
        """报告走纯文本，链接里的下划线/星号不会破坏发送（历史上踩过 Markdown 坑）。"""
        # 链接总长需小于 _BATCH_LINK_LABEL_LIMIT，否则会被截断而看不到这些字符
        link = "magnet:?xt=urn:btih:DDDD&dn=a_b*c[d]"
        self.assertLess(len(link), main._BATCH_LINK_LABEL_LIMIT)
        report = main._build_batch_report([
            (link, main.SubmitOutcome(True, "✅ 提交成功", "提交成功回执")),
        ])
        self.assertIn("a_b*c[d]", report)


if __name__ == "__main__":
    unittest.main()
