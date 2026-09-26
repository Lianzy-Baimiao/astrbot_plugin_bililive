# -*- coding: utf-8 -*-
"""B 站监测 main.py 冒烟测试：桩掉 astrbot 把插件类拉起来，测面板接线与保存链路。

    python tests/test_bililive_main.py
全绿打印 OK。
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import types

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_PLUGIN_ROOT))


class _Logger:
    def info(self, *a, **k):
        pass

    warning = error = debug = info


class _EventMessageType:
    ALL = "all"
    PRIVATE_MESSAGE = "private"


def _passthrough(*d_args, **d_kwargs):
    def deco(func):
        return func

    return deco


class _Star:
    def __init__(self, context=None):
        self.context = context

    async def html_render(self, tmpl, data, options=None):
        return "http://t2i/card.png"


class _MessageChain:
    def message(self, text):
        return self

    def url_image(self, url):
        return self

    def file_image(self, path):
        return self


_DATA_ROOT = tempfile.mkdtemp(prefix="bililive-main-")


def _install_astrbot_stub():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    api.AstrBotConfig = dict

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.filter = types.SimpleNamespace(
        command=_passthrough,
        event_message_type=_passthrough,
        on_astrbot_loaded=_passthrough,
        EventMessageType=_EventMessageType,
    )
    event_mod.AstrMessageEvent = object
    event_mod.MessageChain = _MessageChain

    comp_mod = types.ModuleType("astrbot.api.message_components")
    comp_mod.Image = type("Image", (), {})
    comp_mod.Plain = type("Plain", (), {})

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = object
    star_mod.Star = _Star
    star_mod.StarTools = types.SimpleNamespace(get_data_dir=lambda name="": _DATA_ROOT)
    star_mod.register = lambda *a, **k: (lambda cls: cls)

    core = types.ModuleType("astrbot.core")
    core_utils = types.ModuleType("astrbot.core.utils")
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_data_path = lambda: _DATA_ROOT
    core.utils = core_utils
    core_utils.astrbot_path = path_mod

    api.star = star_mod
    api.event = event_mod
    api.message_components = comp_mod
    astrbot.api = api
    astrbot.core = core

    for name, mod in {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_mod,
        "astrbot.api.message_components": comp_mod,
        "astrbot.api.star": star_mod,
        "astrbot.core": core,
        "astrbot.core.utils": core_utils,
        "astrbot.core.utils.astrbot_path": path_mod,
    }.items():
        sys.modules[name] = mod


_install_astrbot_stub()

import astrbot_plugin_bililive.main as bl_main  # noqa: E402
from astrbot_plugin_bililive import subscription as sub  # noqa: E402


class FakeClient:
    def __init__(self, group_info=None, group_list=None, fail=False):
        self._info = group_info or {}
        self._list = group_list or []
        self._fail = fail
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if self._fail:
            raise RuntimeError("平台炸了")
        return self._list if action == "get_group_list" else self._info


class FakePlatformInst:
    def __init__(self, platform_id, client=None, adapter="aiocqhttp"):
        self._pid = platform_id
        self._client = client
        self._adapter = adapter

    def meta(self):
        return types.SimpleNamespace(id=self._pid, name=self._adapter)

    def get_client(self):
        return self._client


class FakeContext:
    def __init__(self, platform_insts=None):
        self.routes = []
        self._insts = list(platform_insts or [])
        # 真实 AstrBot 两套取法都有，这里保持一致
        self.platform_manager = types.SimpleNamespace(
            platform_insts=self._insts,
            get_insts=lambda: list(self._insts),
        )

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, tuple(methods), desc))


def make_event(text="订阅 111", umo="napcat:GroupMessage:777", gid="777", group_name=None):
    ev = types.SimpleNamespace(
        message_str=text,
        unified_msg_origin=umo,
        message_obj=types.SimpleNamespace(
            group_id=gid,
            group=types.SimpleNamespace(group_id=gid, group_name=group_name),
        ),
    )
    ev.get_platform_id = lambda: "napcat"
    ev.get_group_id = lambda: gid
    return ev


def make_plugin(platform_insts=None, **cfg):
    data_dir = tempfile.mkdtemp(prefix="bililive-case-")
    bl_main.get_astrbot_data_path = lambda: data_dir
    ctx = FakeContext(platform_insts=platform_insts)
    plugin = bl_main.BiliLivePlugin(ctx, cfg)
    return plugin, ctx, plugin.data_dir


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 接线
# ---------------------------------------------------------------------------


def test_import_and_routes():
    plugin, ctx, _ = make_plugin()
    paths = [p for p, *_ in ctx.routes]
    for suffix in (
        "/page/meta",
        "/page/status",
        "/page/subscriptions",
        "/page/subscriptions/save",
        "/page/notify",
        "/page/notify/save",
        "/page/groups",
    ):
        assert f"/astrbot_plugin_bililive{suffix}" in paths, suffix
    assert plugin.page.plugin is plugin
    assert plugin.groups.path.name == "group_names.json"
    assert plugin.groups.path.parent == pathlib.Path(plugin.data_dir)


def test_default_platform_from_config():
    plugin, _, _ = make_plugin(default_platform="napcat")
    assert plugin.default_platform == "napcat"
    plugin2, _, _ = make_plugin()
    assert plugin2.default_platform == "aiocqhttp"  # 探测不到平台时的回落


def test_bare_group_number_gets_routable_instance_id():
    """裸群号必须补成**实例 id**，否则 send_message 匹配不到平台 → 发不出去。

    bililive 的补全在 subscription.normalize_target 里，平台取 plugin.default_platform。
    """
    insts = [
        FakePlatformInst("webchat", adapter="webchat"),
        FakePlatformInst("napcat", adapter="aiocqhttp"),
    ]
    # 1) 配置留空：优先挑 aiocqhttp 类型的实例（不挑到 webchat）
    plugin, _, _ = make_plugin(platform_insts=insts)
    assert plugin.default_platform == "napcat"
    assert sub.normalize_target("123456", plugin.default_platform) == "napcat:GroupMessage:123456"

    # 2) 配置里填的是适配器类型名 aiocqhttp（用户截图里就是这个）→ 映射到实例 id
    plugin2, _, _ = make_plugin(platform_insts=insts, default_platform="aiocqhttp")
    assert plugin2.default_platform == "napcat"
    assert (
        sub.normalize_target("123456", plugin2.default_platform) == "napcat:GroupMessage:123456"
    )

    # 3) 配置里填的就是真实实例 id → 原样尊重
    plugin3, _, _ = make_plugin(platform_insts=insts, default_platform="webchat")
    assert (
        sub.normalize_target("123456", plugin3.default_platform) == "webchat:GroupMessage:123456"
    )

    # 4) 完整 umo 不动
    assert (
        sub.normalize_target("napcat:GroupMessage:123456", plugin3.default_platform)
        == "napcat:GroupMessage:123456"
    )
    assert sub.normalize_target("", plugin3.default_platform) == ""


def test_bare_group_number_without_platforms_keeps_old_behavior():
    plugin, _, _ = make_plugin(default_platform="aiocqhttp")
    assert plugin.default_platform == "aiocqhttp"  # 探不到平台时与旧行为一致
    plugin2, _, _ = make_plugin()
    assert plugin2.default_platform == "aiocqhttp"


def test_load_subs_uses_mapped_platform():
    insts = [FakePlatformInst("napcat", adapter="aiocqhttp")]
    plugin, _, _ = make_plugin(
        platform_insts=insts, default_platform="aiocqhttp", subscriptions=["111=777777777"]
    )
    loaded = plugin._load_subs()
    assert loaded["111"]["groups"][0]["umo"] == "napcat:GroupMessage:777777777"


def test_group_name_memory_and_refresh():
    client = FakeClient(
        group_info={"group_id": 777, "group_name": "魔兽交流群", "member_count": 300},
        group_list=[{"group_id": 777, "group_name": "魔兽交流群", "member_count": 300}],
    )
    plugin, _, data_dir = make_plugin(platform_insts=[FakePlatformInst("napcat", client)])

    run(plugin.on_any_message(make_event()))
    assert plugin.groups.get("napcat:GroupMessage:777") is not None
    assert plugin.groups.name_of("napcat:GroupMessage:777") == ""

    run(plugin._learn_group_name(client, "napcat:GroupMessage:777", "777", "napcat"))
    assert plugin.groups.name_of("napcat:GroupMessage:777") == "魔兽交流群"
    assert client.calls[0][0] == "get_group_info"

    client.calls.clear()
    assert run(plugin.refresh_group_names(force=True)) == 0  # 名字没变
    assert len(client.calls) == 1  # 但确实问了平台
    assert run(plugin.refresh_group_names()) == 0
    assert len(client.calls) == 1  # 节流窗口内不再问
    assert (pathlib.Path(data_dir) / "group_names.json").is_file()


def test_learn_group_name_failure_is_silent():
    plugin, _, _ = make_plugin()
    bad = FakeClient(fail=True)
    run(plugin._learn_group_name(bad, "napcat:GroupMessage:888", "888", "napcat"))
    assert plugin.groups.name_of("napcat:GroupMessage:888") == ""


# ---------------------------------------------------------------------------
# 保存链路（面板改完必须与命令改的一致）
# ---------------------------------------------------------------------------


def test_save_subs_writes_config_and_prunes():
    plugin, _, data_dir = make_plugin()
    plugin.group_settings = {
        "napcat:GroupMessage:777": {"notify": True},
        "napcat:GroupMessage:999": {"notify": False},  # 保存后没人引用了，应被剪掉
    }
    subs, dropped = sub.subs_from_rows(
        [{"uid": "111", "targets": [{"umo": "napcat:GroupMessage:777", "at_all": True}]}]
    )
    assert dropped == []
    run(plugin._save_subs(subs))

    assert plugin.config["subscriptions"] == ["111=napcat:777@all"]
    assert "napcat:GroupMessage:777" in plugin.group_settings
    assert "napcat:GroupMessage:999" not in plugin.group_settings

    # 面板保存后，插件的读取路径能读回同一份配置
    assert plugin._load_subs() == {
        "111": {"groups": [{"umo": "napcat:GroupMessage:777", "at_all": True}]}
    }

    # 动态订阅走另一条保存链路
    dyn, _ = sub.subs_from_rows([{"uid": "222", "targets": ["napcat:GroupMessage:777"]}])
    run(plugin._save_dyn_subs(dyn))
    assert plugin.config["dynamic_subscriptions"] == ["222=napcat:777"]
    assert plugin._load_dyn_subs()["222"]["groups"][0]["umo"] == "napcat:GroupMessage:777"
    assert (pathlib.Path(data_dir) / "groups.json").is_file()


def test_set_group_notify_persists():
    plugin, _, _ = make_plugin()
    plugin._set_group_notify("napcat:GroupMessage:777", "notify_end", False)
    saved_path = pathlib.Path(plugin.group_settings_file)
    assert saved_path.is_file()
    saved = json.loads(saved_path.read_text(encoding="utf-8"))
    assert saved["napcat:GroupMessage:777"]["notify_end"] is False
    assert plugin._group_notify_enabled("napcat:GroupMessage:777", "notify_end") is False
    assert plugin._group_notify_enabled("napcat:GroupMessage:777", "notify") is True


def test_terminate_flushes_group_names():
    plugin, _, data_dir = make_plugin()
    run(plugin.on_any_message(make_event(group_name="魔兽交流群")))
    plugin.monitor_task = None
    plugin.dyn_monitor_task = None
    plugin.session = None
    run(plugin.terminate())
    saved = json.loads(
        (pathlib.Path(data_dir) / "group_names.json").read_text(encoding="utf-8")
    )
    assert saved["groups"]["napcat:GroupMessage:777"]["group_name"] == "魔兽交流群"


def test_group_names_and_notify_settings_do_not_share_a_file():
    """群名缓存与按群通知开关必须各用各的文件，不能都落 groups.json。

    两套数据结构不同（通知开关是扁平 {umo:{notify}}，群名缓存是 {version,groups}），
    同名会互相覆盖、双双丢失。这里在同一个数据目录里两边都写一遍，断言磁盘上各是各的
    文件、各是各的格式，谁也没被对方冲掉（回归测试：曾经都用 groups.json）。
    """
    plugin, _, data_dir = make_plugin()
    plugin._set_group_notify("napcat:GroupMessage:777", "notify", False)  # 落 groups.json
    run(plugin.on_any_message(make_event(group_name="魔兽交流群")))  # 落 group_names.json

    assert plugin._group_notify_enabled("napcat:GroupMessage:777", "notify") is False
    assert plugin.groups.name_of("napcat:GroupMessage:777") == "魔兽交流群"
    assert pathlib.Path(plugin.group_settings_file).name == "groups.json"
    assert plugin.groups.path.name == "group_names.json"

    notify_disk = json.loads(
        (pathlib.Path(data_dir) / "groups.json").read_text(encoding="utf-8")
    )
    names_disk = json.loads(
        (pathlib.Path(data_dir) / "group_names.json").read_text(encoding="utf-8")
    )
    assert notify_disk["napcat:GroupMessage:777"]["notify"] is False
    assert names_disk["groups"]["napcat:GroupMessage:777"]["group_name"] == "魔兽交流群"


def test_route_umo_and_official_at_all_guard():
    """推送目标按平台实例 id 路由；官方 QQ 目标要禁用 @全体。"""
    insts = [
        FakePlatformInst("napcat", adapter="aiocqhttp"),
        FakePlatformInst("qqguan", adapter="qq_official"),
    ]
    plugin, _, _ = make_plugin(platform_insts=insts)
    # 适配器类型名 → 实例 id（否则 send_message 匹配不到、静默丢消息）
    assert plugin._route_umo("aiocqhttp:GroupMessage:123") == "napcat:GroupMessage:123"
    assert plugin._route_umo("qq_official:GroupMessage:OPENID") == "qqguan:GroupMessage:OPENID"
    # 已是实例 id / 裸群号 / 认不出来的：原样，不误改
    assert plugin._route_umo("napcat:GroupMessage:123") == "napcat:GroupMessage:123"
    assert plugin._route_umo("123") == "123"
    assert plugin._route_umo("telegram:GroupMessage:9") == "telegram:GroupMessage:9"
    # 官方 QQ 目标判定（send_* 里据此禁用 @全体）
    assert plugin._target_is_official("qqguan:GroupMessage:OPENID") is True
    assert plugin._target_is_official("qq_official:GroupMessage:OPENID") is True
    assert plugin._target_is_official("napcat:GroupMessage:123") is False


def main():
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    for name, fn in tests:
        fn()
        print(f"  {name} ok")
    print(f"OK ({len(tests)} tests)")


if __name__ == "__main__":
    main()
