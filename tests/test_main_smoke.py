"""main.py 冒烟测试：桩掉 astrbot 把插件类拉起来，跑动态游标/派发/消息拼装。

    python tests/test_main_smoke.py
全绿打印 OK。

本机没装 astrbot 本体，所以这里先把 `astrbot.*` 塞进 sys.modules 再 import 插件包 ——
好处是 main.py 的语法错误、名字写错、纯逻辑回归都能在本地被拦下来。
覆盖：import 健全性 / 动态游标（id 必须按数值比） / 无基线不补发 / 类型开关 /
单轮封顶 / 游标不回退 / 按群推送 / 通知模板渲染与图片上限 / @全体只在视频与直播类生效 /
本群通知开关（notify 与 notify_dyn 两级）。
"""
import asyncio
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG_DIR))  # 让 astrbot_plugin_bililive 能作为包被导入


# ---------------- astrbot 桩 ----------------

class FakeChain:
    """MessageChain 的最小桩：按顺序记录被追加的组件。"""

    def __init__(self):
        self.parts = []

    def at_all(self):
        self.parts.append(("at_all", None))
        return self

    def message(self, text):
        self.parts.append(("text", text))
        return self

    def url_image(self, url):
        self.parts.append(("image", url))
        return self

    def texts(self):
        return [v for k, v in self.parts if k == "text"]

    def images(self):
        return [v for k, v in self.parts if k == "image"]

    def has_at_all(self):
        return any(k == "at_all" for k, _ in self.parts)


def _install_astrbot_stubs():
    """无条件装桩（保证测试与真实 astrbot 解耦）。"""
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    comps = types.ModuleType("astrbot.api.message_components")

    class _Logger:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    class AstrBotConfig(dict):
        pass

    class AstrMessageEvent:
        pass

    class _Filter:
        """filter.command(...) 之类只做透传，不需要真的注册指令。"""

        class EventMessageType:
            PRIVATE_MESSAGE = "private"
            GROUP_MESSAGE = "group"
            ALL = "all"

        @staticmethod
        def command(_name=None):
            return lambda fn: fn

        @staticmethod
        def event_message_type(_type=None):
            return lambda fn: fn

        @staticmethod
        def regex(_pattern=None):
            return lambda fn: fn

        @staticmethod
        def event_message_type(_t=None):
            return lambda fn: fn

    class Star:
        def __init__(self, context=None):
            self.context = context

    class Context:
        pass

    class StarTools:
        @staticmethod
        def get_data_dir(name):
            return os.path.join(tempfile.gettempdir(), "astrbot_plugin_bililive_test", name)

    def register(*_args, **_kwargs):
        return lambda cls: cls

    api.logger = _Logger()
    api.AstrBotConfig = AstrBotConfig
    event.filter = _Filter()
    event.AstrMessageEvent = AstrMessageEvent
    event.MessageChain = FakeChain
    star.Context = Context
    star.Star = Star
    star.StarTools = StarTools
    star.register = register
    comps.Image = type("Image", (), {})
    comps.Plain = type("Plain", (), {})

    astrbot.api = api
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.api.message_components": comps,
    })


_install_astrbot_stubs()

import astrbot_plugin_bililive.main as main  # noqa: E402

GROUP = "napcat:GroupMessage:879265474"
GROUP2 = "napcat:GroupMessage:972781741"
# 真实世界的两个 id：老动态 18 位、新动态 19 位 —— 字符串比大小必翻车
OLD_ID = "598503014097476780"
NEW_ID_1 = "1148459269840961545"
NEW_ID_2 = "1148459269840961546"


def _word_item(id_str, text="水一条动态"):
    """最小可解析的纯文字动态条目。"""
    return {
        "id_str": id_str,
        "type": "DYNAMIC_TYPE_WORD",
        "modules": {
            "module_author": {"name": "UP", "pub_ts": 1},
            "module_dynamic": {"desc": {"text": text}, "major": None},
        },
    }


def _targets(*specs):
    """造订阅目标列表，形如 (umo, at_all) 或只给 umo。"""
    out = []
    for s in specs:
        if isinstance(s, tuple):
            out.append({"umo": s[0], "at_all": s[1]})
        else:
            out.append({"umo": s, "at_all": False})
    return out


def _make_plugin(config=None, group_settings=None):
    """绕过 __init__（不碰网络/事件循环）造一个插件实例。"""
    plugin = object.__new__(main.BiliLivePlugin)
    plugin.config = dict(config or {})
    plugin.dyn_last_ids = {}
    plugin.dyn_error_counts = {}
    plugin.dyn_skip_until = {}
    plugin.live_status_cache = {}
    plugin.group_settings = dict(group_settings or {})
    plugin.sent = []

    async def _record(parsed, origin, at_all):
        plugin.sent.append({"id": parsed.get("id_str"), "parsed": parsed,
                            "origin": origin, "at_all": at_all})

    plugin.send_dynamic_notification = _record
    return plugin


# ---------------- 消息拼装与发送 ----------------

def test_build_dynamic_chain_renders_template_and_caps_images():
    plugin = _make_plugin({
        "dynamic_notify_template": "📢 {uname} {action}\n{title}\n🔗 {url}",
    })
    parsed = {
        "id_str": NEW_ID_1,
        "kind": "video",
        "uname": "DIYgod",
        "action": "投稿了视频",
        "title": "欧洲旅游VLOG",
        "text": "正文",
        "images": ["https://i0.jpg/1.jpg", "https://i0.jpg/2.jpg",
                   "https://i0.jpg/3.jpg", "https://i0.jpg/4.jpg"],
        "url": "https://www.bilibili.com/video/BV1hzqrBtEMP",
    }
    chain = plugin._build_dynamic_chain("📢 {uname} {action}\n{title}\n🔗 {url}", parsed, False)

    body = "\n".join(chain.texts())
    assert "DIYgod" in body and "投稿了视频" in body and "欧洲旅游VLOG" in body
    assert len(chain.images()) == main.dynamic_report.MAX_IMAGES, "图片数要封顶防刷屏"
    assert not chain.has_at_all()


def test_bad_placeholder_kept_literally_instead_of_crashing():
    """模板里写了不存在的占位符：原样保留发出去，不抛异常。"""
    plugin = _make_plugin()
    parsed = {"kind": "word", "uname": "UP", "action": "发布了动态", "title": "",
              "text": "正文", "images": [], "url": "https://t.bilibili.com/1"}
    chain = plugin._build_dynamic_chain("📢 {uname} {unknown}", parsed, False)
    text = chain.texts()[0]
    assert "UP" in text and "{unknown}" in text


def test_send_dynamic_notification_gating():
    """总开关 / 本群 notify / 本群 notify_dyn 三级关门，外加 @全体 的适用范围。"""
    sent = []

    class _Ctx:
        async def send_message(self, origin, chain):
            sent.append((origin, chain))

    def _parsed(kind):
        return {"kind": kind, "uname": "UP", "action": "发布了动态", "title": "标题",
                "text": "", "images": [], "url": "https://t.bilibili.com/1"}

    plugin = _make_plugin({"enable_notifications": True,
                           "dynamic_notify_template": "{uname} {action}\n{title}\n🔗 {url}"})
    del plugin.send_dynamic_notification  # 这个用例要走真实发送路径
    plugin.context = _Ctx()

    asyncio.run(plugin.send_dynamic_notification(_parsed("video"), GROUP, True))
    asyncio.run(plugin.send_dynamic_notification(_parsed("word"), GROUP, True))
    assert len(sent) == 2
    assert sent[0][1].has_at_all(), "视频类动态应@全体"
    assert not sent[1][1].has_at_all(), "纯文字动态不@全体（避免刷屏）"

    # 本群 /关闭动态通知：只拦动态，开播/关播通知不受影响
    plugin.group_settings = {GROUP: {"notify_dyn": False}}
    asyncio.run(plugin.send_dynamic_notification(_parsed("video"), GROUP, True))
    assert len(sent) == 2

    # 本群 /关闭通知：连 notify 一起关掉
    plugin.group_settings = {GROUP: {"notify": False}}
    asyncio.run(plugin.send_dynamic_notification(_parsed("video"), GROUP, True))
    assert len(sent) == 2

    # 全局总开关关掉：一条都不发
    plugin.group_settings = {}
    plugin.config["enable_notifications"] = False
    asyncio.run(plugin.send_dynamic_notification(_parsed("video"), GROUP, True))
    assert len(sent) == 2


def test_send_end_notification_respects_group_master_switch():
    """关播通知也要过本群总开关 notify：/关闭通知（notify=False）应连关播一起停。"""
    sent = []

    class _Ctx:
        async def send_message(self, origin, chain):
            sent.append((origin, chain))

    info = {"uname": "UP", "room_id": 1, "cover": ""}
    plugin = _make_plugin({"enable_notifications": True, "enable_end_notifications": True})
    plugin.context = _Ctx()

    # 默认全开：关播应发
    asyncio.run(plugin.send_end_notification(info, GROUP, False))
    assert len(sent) == 1
    # 只关关播：不发
    plugin.group_settings = {GROUP: {"notify_end": False}}
    asyncio.run(plugin.send_end_notification(info, GROUP, False))
    assert len(sent) == 1
    # /关闭通知（notify=False）：关播也应停（修复点，此前会漏发出去）
    plugin.group_settings = {GROUP: {"notify": False}}
    asyncio.run(plugin.send_end_notification(info, GROUP, False))
    assert len(sent) == 1


# （运行器在文件末尾：脚本自上而下执行，所有 test_* 必须先定义好）


# ---------------- 基础健全性 ----------------

def test_module_imports_and_commands_exist():
    names = [
        "subscribe", "unsubscribe", "list_subscriptions", "check_live",
        "dyn_subscribe", "dyn_unsubscribe", "dyn_list", "check_dynamic",
        "plugin_status", "handle_private_message",
        "enable_notify_cmd", "disable_notify_cmd",
        "enable_end_notify_cmd", "disable_end_notify_cmd",
        "enable_dyn_notify_cmd", "disable_dyn_notify_cmd",
        "monitor_live_status", "monitor_dynamics", "get_user_dynamics",
    ]
    for n in names:
        assert callable(getattr(main.BiliLivePlugin, n, None)), f"缺少 {n}"


def test_no_baseline_records_without_pushing():
    """没有基线时只记基线、不补发存量动态。"""
    plugin = _make_plugin()
    items = [_word_item(NEW_ID_2), _word_item(NEW_ID_1), _word_item(OLD_ID)]

    asyncio.run(plugin._dispatch_dynamics("1", items, _targets(GROUP), plugin.dynamic_notify_toggles))

    assert plugin.sent == [], "首次见到动态不应补发"
    assert plugin.dyn_last_ids["1"] == NEW_ID_2  # 取数值最大，而非列表第一个


# ---------------- 游标 ----------------

def test_dispatch_pushes_across_id_length():
    """18 位老 id 当基线，19 位新 id 必须被判成新动态（按数值比）。"""
    plugin = _make_plugin()
    plugin.dyn_last_ids["1"] = OLD_ID
    items = [_word_item(NEW_ID_2), _word_item(NEW_ID_1), _word_item(OLD_ID)]

    asyncio.run(plugin._dispatch_dynamics("1", items, _targets(GROUP), plugin.dynamic_notify_toggles))

    assert [s["id"] for s in plugin.sent] == [NEW_ID_1, NEW_ID_2], "应只补两条新动态且按序"
    assert plugin.dyn_last_ids["1"] == NEW_ID_2


def test_dispatch_cursor_never_moves_backwards():
    plugin = _make_plugin()
    plugin.dyn_last_ids["1"] = NEW_ID_2
    items = [_word_item(NEW_ID_1), _word_item(OLD_ID)]

    asyncio.run(plugin._dispatch_dynamics("1", items, _targets(GROUP), plugin.dynamic_notify_toggles))

    assert plugin.sent == []
    assert plugin.dyn_last_ids["1"] == NEW_ID_2, "游标不许回退"


def test_dispatch_caps_per_round_keeping_newest():
    plugin = _make_plugin()
    plugin.dyn_last_ids["1"] = OLD_ID
    items = [_word_item(f"1148459269840961{i:03d}") for i in range(15)]

    asyncio.run(plugin._dispatch_dynamics("1", items, _targets(GROUP), plugin.dynamic_notify_toggles))

    assert len(plugin.sent) == main.dynamic_report.MAX_PUSH_PER_ROUND
    pushed = [s["id"] for s in plugin.sent]
    assert pushed == sorted(pushed), "补推顺序应为旧→新"
    assert pushed == [f"1148459269840961{i:03d}" for i in range(5, 15)], "只推最近的若干条"


def test_dispatch_skips_by_type_toggle_but_advances_cursor():
    plugin = _make_plugin({"dyn_notify_word": False})
    plugin.dyn_last_ids["1"] = OLD_ID

    asyncio.run(plugin._dispatch_dynamics("1", [_word_item(NEW_ID_2)], _targets(GROUP),
                                          plugin.dynamic_notify_toggles))

    assert plugin.sent == [], "开关关掉的类型不推"
    assert plugin.dyn_last_ids["1"] == NEW_ID_2, "但游标仍要前进，别反复重算同一条"


def test_dispatch_uses_each_group_own_at_all():
    """一个UP订了多个群：每个群按自己的 @all 标记收，不能串味。"""
    plugin = _make_plugin()
    plugin.dyn_last_ids["1"] = OLD_ID

    asyncio.run(plugin._dispatch_dynamics(
        "1", [_word_item(NEW_ID_2)], _targets((GROUP, True), (GROUP2, False)),
        plugin.dynamic_notify_toggles))

    assert [(s["origin"], s["at_all"]) for s in plugin.sent] == [(GROUP, True), (GROUP2, False)]


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    if failed:
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    _run()

