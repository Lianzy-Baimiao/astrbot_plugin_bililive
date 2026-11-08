"""输出屏蔽的发送边界回归：python tests/test_output_filter.py。"""

import types
import unittest
from unittest.mock import AsyncMock, patch

import test_main_smoke as harness
from astrbot_plugin_bililive.output_filter import blocked_keyword, message_text
from astrbot_plugin_bililive.bili_login import BilibiliLoginManager

main = harness.main
GROUP = harness.GROUP


class Chain(harness.FakeChain):
    @property
    def chain(self):
        return [types.SimpleNamespace(text=value) if kind == "text" else types.SimpleNamespace()
                for kind, value in self.parts]


class Config(dict):
    def save_config(self):
        self.saved = dict(self)


def unwrap(response):
    return response["data"]


def event(text):
    return types.SimpleNamespace(
        message_str=text, unified_msg_origin=GROUP,
        plain_result=lambda text: Chain().message(text), make_result=Chain,
    )


async def collect(replies):
    return [reply async for reply in replies]


class OutputFilterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = harness._make_comment_plugin()
        del self.plugin.send_dynamic_notification
        del self.plugin.send_comment_notification
        self.plugin.context = types.SimpleNamespace(send_message=AsyncMock())
        self.send = self.plugin.context.send_message
        self.chain_patch = patch.object(main, "MessageChain", Chain)
        self.chain_patch.start()
        self.addCleanup(self.chain_patch.stop)

    def test_default_list_empty_blanks_and_literal_matching(self):
        self.assertEqual(blocked_keyword({}, "无限可能"), "无限")
        self.assertIsNone(blocked_keyword({"output_block_keywords": []}, "无限"))
        self.assertIsNone(blocked_keyword({"output_block_keywords": ["", " "]}, "正常"))
        cfg = {"output_block_keywords": [" 广告 ", "a.b"]}
        self.assertEqual(blocked_keyword(cfg, "新广告"), "广告")
        self.assertEqual(blocked_keyword(cfg, "a.b"), "a.b")
        self.assertIsNone(blocked_keyword(cfg, "无限 axb A.B"))

    async def test_live_and_end_block_title_with_cover_and_at_all(self):
        status = {"uname": "UP", "title": "无限可能", "room_id": 1, "cover": "https://example.com/cover.jpg"}
        await self.plugin.send_live_notification(status, GROUP, True)
        await self.plugin.send_end_notification(status, GROUP, False)
        self.send.assert_not_awaited()
        status["title"] = "普通直播"
        await self.plugin.send_live_notification(status, GROUP, True)
        await self.plugin.send_end_notification(status, GROUP, False)
        self.assertEqual(self.send.await_count, 2)
        chain = self.send.await_args_list[0].args[1]
        self.assertTrue(chain.has_at_all())
        self.assertEqual(len(chain.images()), 1)

    async def test_templates_names_and_config_changes(self):
        status = {"uname": "无限UP", "title": "普通直播"}
        await self.plugin.send_live_notification(status, GROUP, True)
        self.send.assert_not_awaited()
        status["uname"] = "普通UP"
        self.plugin.config["live_notify_template"] = "无限直播"
        await self.plugin.send_live_notification(status, GROUP, True)
        self.send.assert_not_awaited()
        self.plugin.config["output_block_keywords"] = []
        await self.plugin.send_live_notification(status, GROUP, True)
        self.assertEqual(self.send.await_count, 1)
        self.plugin.config["output_block_keywords"] = ["直播"]
        await self.plugin.send_live_notification(status, GROUP, True)
        self.assertEqual(self.send.await_count, 1)

    async def test_panel_keywords_roundtrip_changes_output(self):
        self.plugin.config = Config(dyn_comment_watch_hours=6)
        ctrl = main.BiliLivePageController(self.plugin.context, self.plugin)
        self.assertEqual(unwrap(await ctrl.get_config())["data"]["output_block_keywords"], ["无限"])
        for values, expected in (([" 广告 ", "", "广告"], ["广告"]), ([], [])):
            with patch.object(ctrl, "_payload", AsyncMock(return_value={"output_block_keywords": values})):
                result = unwrap(await ctrl.save_config())
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["data"]["output_block_keywords"], expected)
            self.assertEqual(self.plugin.config.saved["output_block_keywords"], expected)
            self.assertEqual(self.plugin.config["dyn_comment_watch_hours"], 6)
            self.assertEqual(self.plugin._output_blocked("广告"), bool(expected))
            self.assertFalse(self.plugin._output_blocked("无限"))
        with patch.object(ctrl, "_payload", AsyncMock(return_value={"dyn_comment_watch_hours": 12})):
            self.assertEqual(unwrap(await ctrl.save_config())["status"], "ok")
        self.assertEqual(self.plugin.config["output_block_keywords"], [])

    async def test_panel_invalid_keywords_and_save_failure_leave_config_unchanged(self):
        self.plugin.config = Config(output_block_keywords=["无限"], dyn_comment_watch_hours=6)
        ctrl = main.BiliLivePageController(self.plugin.context, self.plugin)
        original = dict(self.plugin.config)
        for invalid in ("广告", None, [1], ["广告", False]):
            with patch.object(ctrl, "_payload", AsyncMock(return_value={"output_block_keywords": invalid})):
                self.assertEqual(unwrap(await ctrl.save_config())["status"], "error")
            self.assertEqual(self.plugin.config, original)
        with patch.object(ctrl, "_payload", AsyncMock(return_value={"output_block_keywords": [], "dyn_comment_watch_hours": 12})), patch.object(
            self.plugin.config, "save_config", side_effect=OSError("disk full")
        ):
            self.assertEqual(unwrap(await ctrl.save_config())["status"], "error")
        self.assertEqual(self.plugin.config, original)

    async def test_dynamic_body_is_checked_even_when_template_omits_it(self):
        parsed = {"kind": "video", "uname": "UP", "title": "正常标题", "text": "正常" * 50 + "无限",
                  "images": ["https://example.com/cover.jpg"], "url": "https://example.com/video"}
        await self.plugin.send_dynamic_notification(parsed, GROUP, True)
        self.send.assert_not_awaited()
        parsed["text"] = "普通内容"
        await self.plugin.send_dynamic_notification(parsed, GROUP, True)
        self.send.assert_awaited_once()
        self.assertTrue(self.send.await_args.args[1].has_at_all())

    async def test_blocked_dynamic_advances_cursor_without_comment_watch(self):
        self.plugin.config["comment_watch_uids"] = [harness.WATCH_UID]
        self.plugin.dyn_last_ids[harness.WATCH_UID] = harness.OLD_ID
        items = [harness._video_item(harness.NEW_ID_1, harness.WATCH_BV, "无限可能")]
        await self.plugin._dispatch_dynamics(harness.WATCH_UID, items, harness._targets((GROUP, True)),
                                            self.plugin.dynamic_notify_toggles)
        self.assertEqual(self.plugin.dyn_last_ids[harness.WATCH_UID], harness.NEW_ID_1)
        self.assertEqual(self.plugin.dyn_comment_watch, {})
        self.send.assert_not_awaited()

    async def test_comment_title_body_and_template_skip_images(self):
        pinned = {"text": "无限攻略", "images": ["https://example.com/comment.jpg"]}
        await self.plugin.send_comment_notification("UP", "普通标题", pinned, GROUP, True)
        pinned["text"] = "普通评论"
        await self.plugin.send_comment_notification("UP", "无限视频", pinned, GROUP, True)
        self.plugin.config["comment_notify_template"] = "无限模板"
        await self.plugin.send_comment_notification("UP", "普通标题", pinned, GROUP, True)
        self.send.assert_not_awaited()

    async def test_manual_live_query_skips_whole_message_chain(self):
        status = {"uname": "UP", "title": "无限可能", "live_status": 1,
                  "cover": "https://example.com/cover.jpg"}
        self.plugin.get_live_status = AsyncMock(return_value=status)
        self.assertEqual(await collect(self.plugin.check_live(event("检查直播 123"))), [])
        status["title"] = "普通标题"
        replies = await collect(self.plugin.check_live(event("检查直播 123")))
        self.assertEqual(len(replies), 1)
        self.assertEqual(len(replies[0].images()), 1)

    async def test_manual_dynamic_checks_full_text_before_truncation(self):
        self.plugin.get_user_card = AsyncMock(return_value={"name": "UP"})
        self.plugin.get_user_dynamics = AsyncMock(return_value=[
            harness._word_item(harness.NEW_ID_1, "正常" * 50 + "无限")
        ])
        self.assertEqual(await collect(self.plugin.check_dynamic(event("检查动态 123"))), [])

    async def test_reply_guard_checks_plain_results(self):
        self.plugin.config["output_block_keywords"] = ["用法"]
        self.assertEqual(await collect(self.plugin.check_live(event("检查直播"))), [])
        self.plugin.config["output_block_keywords"] = []
        replies = await collect(self.plugin.check_live(event("检查直播")))
        self.assertIn("用法", message_text(replies[0]))

    async def test_manual_pinned_comment_and_poll_deduplication(self):
        uid = harness.WATCH_UID
        self.plugin.config["comment_watch_uids"] = [uid]
        self.plugin.get_video_top_comment = AsyncMock(return_value={
            "upper": {"mid": int(uid), "top": {"rpid": 123, "mid": int(uid),
                "member": {"uname": "UP"}, "content": {"message": "无限攻略",
                "pictures": [{"img_src": "https://example.com/pinned.jpg"}]}}}
        })
        self.assertEqual(await collect(self.plugin.check_top_comment(event(f"检查置顶评论 {harness.WATCH_BV}"))), [])
        entry = {"uid": uid, "aid": harness.WATCH_AID, "bvid": harness.WATCH_BV, "title": "正常标题",
                 "groups": harness._targets((GROUP, True)), "first_seen_ts": main.time.time(), "pushed_rpid": ""}
        self.plugin.dyn_comment_watch["watch"] = entry
        with patch.object(main.asyncio, "sleep", AsyncMock()):
            await self.plugin._poll_comment_watches()
            await self.plugin._poll_comment_watches()
        self.assertEqual(entry["pushed_rpid"], "123")
        self.send.assert_not_awaited()

    async def test_login_notifications_share_plugin_filter(self):
        manager = BilibiliLoginManager(self.plugin.context, AsyncMock(), output_blocked=self.plugin._output_blocked)
        ev = event("b站登录")
        ev.send = AsyncMock()
        await manager._send_to(GROUP, "无限")
        await manager._send_reply(ev, Chain().message("无限").url_image("https://example.com/qr.png"))
        self.send.assert_not_awaited()
        ev.send.assert_not_awaited()
        await manager._send_to(GROUP, "登录成功")
        await manager._send_reply(ev, ev.plain_result("登录成功"))
        self.send.assert_awaited_once()
        ev.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
