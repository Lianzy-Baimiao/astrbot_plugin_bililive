# -*- coding: utf-8 -*-
"""B 站监测面板单测：订阅矩阵双向转换 + page.py 各接口（桩掉 astrbot）。

    python tests/test_bililive_panel.py
全绿打印 OK。
"""
import asyncio
import os
import sys
import tempfile
import types

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_PLUGIN_ROOT))  # 让 astrbot_plugin_bililive 可作包导入


class _Logger:
    def info(self, *a, **k):
        pass

    warning = error = debug = info


def _install_astrbot_stub():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api


_install_astrbot_stub()

import astrbot_plugin_bililive.page as page_mod  # noqa: E402
from astrbot_plugin_bililive import subscription as sub  # noqa: E402
from astrbot_plugin_bililive.groups import GroupNameResolver  # noqa: E402


# ---------------------------------------------------------------------------
# 假 request / 假插件（订阅读写用真的 subscription.py，保证往返一致）
# ---------------------------------------------------------------------------


class FakeQuery:
    def __init__(self, data):
        self._data = {k: str(v) for k, v in (data or {}).items()}

    def get(self, key, default=""):
        return self._data.get(key, default)


class FakeRequest:
    def __init__(self, query=None, body=None, method="GET"):
        self.query = FakeQuery(query)
        self.args = self.query
        self.method = method
        self._body = body

    async def json(self, default=None):
        return self._body if self._body is not None else default


def with_request(req):
    page_mod.request = req


def unwrap(resp):
    payload = resp["data"] if "data" in resp and isinstance(resp.get("data"), dict) else resp
    if "status" in payload:
        return (
            payload["status"] == "ok",
            payload.get("data") or {},
            payload.get("message", ""),
        )
    return False, {}, payload.get("message", "")


class FakeConfig(dict):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.save_count = 0

    def save_config(self):
        self.save_count += 1


class FakeTask:
    def __init__(self, alive=True):
        self._alive = alive

    def done(self):
        return not self._alive


def run(coro):
    return asyncio.run(coro)


class FakeContext:
    def __init__(self):
        self.routes = []

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, tuple(methods), desc))


class FakePlugin:
    """够 page.py 用；订阅的解析/序列化复用真实的 subscription.py。"""

    def __init__(self, data_dir, subscriptions=None, dynamic_subscriptions=None,
                 group_settings=None, **cfg):
        self.data_dir = data_dir
        cfg.setdefault("subscriptions", list(subscriptions or []))
        cfg.setdefault("dynamic_subscriptions", list(dynamic_subscriptions or []))
        self.config = FakeConfig(**cfg)
        self.groups = GroupNameResolver(os.path.join(data_dir, "groups.json"))
        self.groups.load()
        self.group_settings = dict(group_settings or {})
        self.default_platform = str(cfg.get("default_platform") or "aiocqhttp")
        self.enable_notifications = True
        self.enable_end_notifications = True
        self.check_interval = 60
        self.dynamic_check_interval = 45
        self.current_interval = 60
        self.dyn_current_interval = 45
        self.max_monitors = 50
        self.session = types.SimpleNamespace(closed=False)
        self.monitor_task = FakeTask(True)
        self.dyn_monitor_task = FakeTask(False)
        self.dyn_last_ids = {"111": "abc"}
        self.live_status_cache = {"111": 1}
        self.save_calls = []
        self.refresh_calls = []

    # ---- 订阅读写（与真实插件同构）----
    def _load_subs(self):
        return sub.parse_subscriptions(
            self.config.get("subscriptions", []), self.default_platform
        )

    def _load_dyn_subs(self):
        return sub.parse_subscriptions(
            self.config.get("dynamic_subscriptions", []), self.default_platform
        )

    def _invalid_sub_lines(self):
        return [
            str(s) for s in (self.config.get("subscriptions") or [])
            if str(s).strip() and not str(s).strip().startswith("#")
            and sub.parse_line(str(s), self.default_platform) is None
        ]

    def _invalid_dyn_sub_lines(self):
        return [
            str(s) for s in (self.config.get("dynamic_subscriptions") or [])
            if str(s).strip() and not str(s).strip().startswith("#")
            and sub.parse_line(str(s), self.default_platform) is None
        ]

    async def _save_subs(self, subs):
        self.config["subscriptions"] = sub.serialize_subscriptions(subs)
        self.config.save_config()
        self.save_calls.append(("live", subs))

    async def _save_dyn_subs(self, subs):
        self.config["dynamic_subscriptions"] = sub.serialize_subscriptions(subs)
        self.config.save_config()
        self.save_calls.append(("dynamic", subs))

    # ---- 通知开关 ----
    def _group_notify_enabled(self, umo, kind):
        st = self.group_settings.get(umo or "")
        if not isinstance(st, dict):
            return True
        return bool(st.get(kind, True))

    def _set_group_notify(self, umo, kind, value):
        self.group_settings.setdefault(umo, {})[kind] = bool(value)

    # ---- 其他 ----
    def _platform_ids(self):
        return [pid for pid, _ptype in self._platform_instances()]

    def _platform_instances(self):
        # 与用户现场一致：两个 aiocqhttp 实例（napcat / default_*）+ 一个 webchat
        return [("napcat", "aiocqhttp"), ("default_102737249", "aiocqhttp"), ("webchat", "webchat")]

    def _parse_quiet_hours(self):
        raw = str(self.config.get("quiet_hours", "") or "")
        return ("23:00", "08:00") if "-" in raw else None

    def _in_quiet_hours(self):
        return True

    async def refresh_group_names(self, force=False, interval=300):
        self.refresh_calls.append(force)
        return 5


def make_case(**cfg):
    data_dir = tempfile.mkdtemp(prefix="bililive-panel-")
    plugin = FakePlugin(data_dir, **cfg)
    ctrl = page_mod.BiliLivePageController(None, plugin)
    return plugin, ctrl, data_dir


# ---------------------------------------------------------------------------
# 纯函数：矩阵双向转换
# ---------------------------------------------------------------------------


def test_subs_roundtrip_via_rows():
    subs = sub.parse_subscriptions(
        ["111111111=777777777@all,napcat:888888888", "222222222=aiocqhttp:777777777"],
        "aiocqhttp",
    )
    rows = sub.subs_to_rows(subs)
    assert [r["uid"] for r in rows] == ["111111111", "222222222"]
    assert rows[0]["group_count"] == 2
    assert rows[0]["targets"][0] == {"umo": "aiocqhttp:GroupMessage:777777777", "at_all": True}
    assert rows[0]["targets"][1] == {"umo": "napcat:GroupMessage:888888888", "at_all": False}

    again, dropped = sub.subs_from_rows(rows)
    assert dropped == []
    assert sub.serialize_subscriptions(again) == sub.serialize_subscriptions(subs)


def test_subs_from_rows_validation():
    rows = [
        {"uid": "111", "targets": [{"umo": "napcat:GroupMessage:1", "at_all": True},
                                   {"umo": "napcat:GroupMessage:1"},
                                   {"umo": "  "}]},
        {"uid": "abc", "targets": [{"umo": "napcat:GroupMessage:2"}]},   # UID 非数字
        {"uid": "", "targets": []},
        {"uid": "222", "targets": []},                                    # 无目标 → 丢掉
        "不是对象",
        {"uid": "333", "targets": ["napcat:GroupMessage:3"]},              # 字符串目标也认
    ]
    subs, dropped = sub.subs_from_rows(rows)
    assert sorted(subs) == ["111", "333"]
    assert subs["111"]["groups"] == [{"umo": "napcat:GroupMessage:1", "at_all": True}]
    assert subs["333"]["groups"] == [{"umo": "napcat:GroupMessage:3", "at_all": False}]
    assert dropped == ["abc", "(空)"]


def test_subs_to_rows_skips_broken_entries():
    rows = sub.subs_to_rows(
        {
            "111": {"groups": [{"umo": "a"}, {"umo": ""}, "坏的", None, {"umo": "b", "at_all": 1}]},
            "222": "不是字典",
        }
    )
    assert [r["uid"] for r in rows] == ["111", "222"]
    assert rows[0]["targets"] == [
        {"umo": "a", "at_all": False},
        {"umo": "b", "at_all": True},
    ]
    assert rows[1]["targets"] == [] and rows[1]["group_count"] == 0
    assert sub.subs_to_rows(None) == []


# ---------------------------------------------------------------------------
# 路由与基础
# ---------------------------------------------------------------------------


def test_page_module_without_astrbot_web():
    assert page_mod._HAS_WEB_API is False
    resp = page_mod.BiliLivePageController._ok({"a": 1}, "hi")
    assert resp["data"]["status"] == "ok" and resp["data"]["message"] == "hi"
    assert page_mod.BiliLivePageController._err("x", 501)["status_code"] == 501


def test_routes_registered():
    ctx = FakeContext()
    ctrl = page_mod.BiliLivePageController(ctx, None)
    ctrl.register_routes()
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
    assert all(p.startswith("/astrbot_plugin_bililive/") for p in paths)


# ---------------------------------------------------------------------------
# 接口：订阅
# ---------------------------------------------------------------------------


def test_get_subscriptions_with_labels():
    plugin, ctrl, _ = make_case(
        subscriptions=["111111111=777777777@all", "222222222=napcat:888888888"],
        dynamic_subscriptions=["333333333=777777777"],
        oops_line="坏行",
    )
    plugin.config["subscriptions"] = ["111111111=777777777@all", "222222222=napcat:888888888", "坏行"]
    plugin.groups.remember("aiocqhttp:GroupMessage:777777777", group_name="魔兽交流群")
    plugin.groups.remember("napcat:GroupMessage:888888888", group_name="活动群")

    with_request(FakeRequest())
    ok, data, _ = unwrap(run(ctrl.get_subscriptions()))
    assert ok
    live = data["live"]
    assert live["uids"] == 2 and live["group_links"] == 2
    assert live["config_key"] == "subscriptions"
    assert live["invalid_lines"] == ["坏行"]
    by_uid = {r["uid"]: r for r in live["rows"]}
    assert by_uid["111111111"]["targets"][0]["label"] == "魔兽交流群（777777777）"
    assert by_uid["111111111"]["targets"][0]["at_all"] is True
    assert by_uid["222222222"]["targets"][0]["label"] == "活动群（888888888）"
    assert data["dynamic"]["uids"] == 1
    # 候选群：被引用的排前面
    values = [g["value"] for g in data["groups"]]
    assert values[0] in ("aiocqhttp:GroupMessage:777777777", "napcat:GroupMessage:888888888")
    assert "napcat:GroupMessage:888888888" in values
    assert data["default_platform"] == "aiocqhttp"
    assert data["max_monitors"] == 50


def test_save_subscriptions_live_and_dynamic():
    plugin, ctrl, _ = make_case()
    rows = [
        {"uid": "111111111", "targets": [{"umo": "napcat:GroupMessage:777", "at_all": True}]},
        {"uid": "222222222", "targets": [{"umo": "napcat:GroupMessage:888"}]},
    ]
    with_request(FakeRequest(body={"kind": "live", "rows": rows}, method="POST"))
    ok, data, msg = unwrap(run(ctrl.save_subscriptions()))
    assert ok
    assert plugin.config["subscriptions"] == [
        "111111111=napcat:777@all",
        "222222222=napcat:888",
    ]
    assert plugin.config.save_count == 1
    assert data["kind"] == "live" and data["uids"] == 2 and data["group_links"] == 2
    assert data["lines"] == plugin.config["subscriptions"]
    assert "已保存 2 个 UP 主" in msg
    assert plugin.save_calls[0][0] == "live"

    with_request(
        FakeRequest(
            body={"kind": "dynamic", "rows": [{"uid": "333", "targets": ["napcat:GroupMessage:9"]}]},
            method="POST",
        )
    )
    ok, data, _ = unwrap(run(ctrl.save_subscriptions()))
    assert ok and plugin.config["dynamic_subscriptions"] == ["333=napcat:9"]
    assert plugin.save_calls[-1][0] == "dynamic"


def test_save_subscriptions_validation():
    plugin, ctrl, _ = make_case()
    with_request(FakeRequest(body={"kind": "nope", "rows": []}, method="POST"))
    ok, _, msg = unwrap(run(ctrl.save_subscriptions()))
    assert not ok and "kind" in msg

    with_request(FakeRequest(body={"kind": "live", "rows": "x"}, method="POST"))
    ok, _, msg = unwrap(run(ctrl.save_subscriptions()))
    assert not ok and "rows" in msg

    many = [{"uid": str(i), "targets": [{"umo": "napcat:GroupMessage:1"}]} for i in range(60)]
    with_request(FakeRequest(body={"kind": "live", "rows": many}, method="POST"))
    ok, _, msg = unwrap(run(ctrl.save_subscriptions()))
    assert not ok and "最多" in msg
    assert plugin.config["subscriptions"] == []  # 没落盘


def test_save_subscriptions_reports_dropped_rows():
    plugin, ctrl, _ = make_case()
    rows = [
        {"uid": "111", "targets": [{"umo": "napcat:GroupMessage:1"}]},
        {"uid": "abc", "targets": [{"umo": "napcat:GroupMessage:2"}]},
    ]
    with_request(FakeRequest(body={"kind": "live", "rows": rows}, method="POST"))
    ok, data, msg = unwrap(run(ctrl.save_subscriptions()))
    assert ok and data["dropped"] == ["abc"] and "已跳过" in msg


def test_save_subscriptions_without_plugin_support():
    _, ctrl, _ = make_case()
    ctrl.plugin = types.SimpleNamespace()
    with_request(FakeRequest(body={"kind": "live", "rows": []}, method="POST"))
    ok, _, msg = unwrap(run(ctrl.save_subscriptions()))
    assert not ok and "不支持" in msg


# ---------------------------------------------------------------------------
# 接口：通知开关 / 状态 / 群列表
# ---------------------------------------------------------------------------


def test_notify_rows_and_save():
    plugin, ctrl, _ = make_case(
        subscriptions=["111=napcat:GroupMessage:777"],
        group_settings={"napcat:GroupMessage:777": {"notify_end": False}},
    )
    plugin.groups.remember("napcat:GroupMessage:777", group_name="魔兽交流群")
    plugin.group_settings["napcat:GroupMessage:999"] = {"notify_dyn": False}

    with_request(FakeRequest())
    ok, data, _ = unwrap(run(ctrl.get_notify()))
    assert ok
    by_umo = {r["umo"]: r for r in data["rows"]}
    assert by_umo["napcat:GroupMessage:777"]["label"] == "魔兽交流群（777）"
    assert by_umo["napcat:GroupMessage:777"]["notify"] is True
    assert by_umo["napcat:GroupMessage:777"]["notify_end"] is False
    assert by_umo["napcat:GroupMessage:999"]["notify_dyn"] is False
    assert [k["value"] for k in data["kinds"]] == ["notify", "notify_end", "notify_dyn"]
    assert data["global"]["enable_notifications"] is True

    with_request(
        FakeRequest(
            body={"umo": "napcat:GroupMessage:777", "kind": "notify_end", "value": True},
            method="POST",
        )
    )
    ok, data, msg = unwrap(run(ctrl.save_notify()))
    assert ok and plugin.group_settings["napcat:GroupMessage:777"]["notify_end"] is True
    assert "已开启" in msg and data["label"] == "魔兽交流群（777）"


def test_save_notify_validation():
    plugin, ctrl, _ = make_case()
    with_request(FakeRequest(body={"kind": "notify", "value": True}, method="POST"))
    ok, _, msg = unwrap(run(ctrl.save_notify()))
    assert not ok and "umo" in msg

    with_request(FakeRequest(body={"umo": "napcat:GroupMessage:1", "kind": "x", "value": True}, method="POST"))
    ok, _, msg = unwrap(run(ctrl.save_notify()))
    assert not ok and "kind" in msg

    with_request(FakeRequest(body={"umo": "napcat:GroupMessage:1", "kind": "notify", "value": "yes"}, method="POST"))
    ok, _, msg = unwrap(run(ctrl.save_notify()))
    assert not ok and "布尔" in msg


def test_status_reports_runtime_state():
    plugin, ctrl, _ = make_case(
        subscriptions=["111=napcat:GroupMessage:777"],
        dynamic_subscriptions=["222=napcat:GroupMessage:777"],
        quiet_hours="23:00-08:00",
        bilibili_cookie="SESSDATA=xx",
        subscriptions_bad=None,
    )
    plugin.config["subscriptions"] = ["111=napcat:GroupMessage:777", "坏行"]
    plugin.current_interval = 240  # 退避中
    with_request(FakeRequest())
    ok, data, _ = unwrap(run(ctrl.get_status()))
    assert ok
    assert data["session_ok"] is True
    assert data["monitor_running"] is True
    assert data["dyn_monitor_running"] is False
    assert data["backoff_live"] is True and data["current_interval"] == 240
    assert data["backoff_dynamic"] is False
    assert data["monitors"] == 1 and data["monitor_links"] == 1
    assert data["dyn_monitors"] == 1 and data["dyn_baseline"] == 1
    assert data["live_status_cache"] == 1
    assert data["cookie_set"] is True
    assert data["quiet"] == {"raw": "23:00-08:00", "valid": True, "active": True}
    assert data["invalid_lines"]["live"] == ["坏行"]
    assert data["platforms"] == ["napcat", "default_102737249", "webchat"]
    assert data["group_settings_count"] == 0


def test_status_quiet_invalid_and_slack_session():
    plugin, ctrl, _ = make_case(quiet_hours="乱写")
    plugin.session = types.SimpleNamespace(closed=True)
    plugin.monitor_task = None
    with_request(FakeRequest())
    ok, data, _ = unwrap(run(ctrl.get_status()))
    assert ok
    assert data["session_ok"] is False and data["monitor_running"] is False
    assert data["quiet"] == {"raw": "乱写", "valid": False, "active": False}


def test_groups_endpoint_refresh():
    plugin, ctrl, _ = make_case(subscriptions=["111=napcat:GroupMessage:777"])
    plugin.groups.remember("napcat:GroupMessage:777", group_name="魔兽交流群")

    with_request(FakeRequest())
    ok, data, _ = unwrap(run(ctrl.get_groups()))
    assert ok and plugin.refresh_calls == []
    assert data["total"] == 1 and data["named"] == 1
    assert data["groups"][0]["source"] == "referenced"

    with_request(FakeRequest(query={"refresh": "1"}))
    ok, data, _ = unwrap(run(ctrl.get_groups()))
    assert ok and data["refreshed"] == 5 and plugin.refresh_calls == [True]


def test_meta_endpoint():
    _, ctrl, _ = make_case()
    with_request(FakeRequest())
    ok, data, _ = unwrap(run(ctrl.get_meta()))
    assert ok
    assert data["kinds"] == ["live", "dynamic"]
    assert data["kinds_config"]["live"] == "subscriptions"
    assert [k["label"] for k in data["notify_kinds"]] == ["开播", "关播", "动态"]
    assert data["limits"]["max_monitors"] == 50


# ---------------------------------------------------------------------------
# 同一个群的不同平台写法（重复群）
# ---------------------------------------------------------------------------


def test_group_id_of_and_group_targets():
    assert sub.group_id_of("napcat:GroupMessage:123") == "123"
    assert sub.group_id_of("123") == "123"
    assert sub.group_id_of("") == ""
    assert sub.group_id_of(None) == ""

    grouped = sub.group_targets(
        [
            {"umo": "aiocqhttp:GroupMessage:123", "at_all": False},
            {"umo": "napcat:GroupMessage:123", "at_all": True},
            {"umo": "napcat:GroupMessage:456"},
            "napcat:GroupMessage:789",
            {"umo": ""},
            "坏的",
        ]
    )
    assert [g["group_id"] for g in grouped] == ["123", "456", "789", "坏的"]
    assert grouped[0]["umos"] == ["aiocqhttp:GroupMessage:123", "napcat:GroupMessage:123"]
    assert grouped[0]["at_all"] is True  # 任一写法勾了 @全体 就保留
    assert grouped[1]["umos"] == ["napcat:GroupMessage:456"]
    assert grouped[2]["umos"] == ["napcat:GroupMessage:789"]  # 字符串写法也认


def test_pick_umo_prefers_reachable_platform():
    umos = ["aiocqhttp:GroupMessage:123", "napcat:GroupMessage:123"]
    assert sub.pick_umo(umos, ["napcat"]) == "napcat:GroupMessage:123"
    assert sub.pick_umo(umos, []) == "aiocqhttp:GroupMessage:123"  # 没有偏好就保序最早
    assert sub.pick_umo([], ["napcat"]) == ""


def test_dedupe_targets_and_rows():
    kept, merged = sub.dedupe_targets(
        [
            {"umo": "aiocqhttp:GroupMessage:123", "at_all": False},
            {"umo": "napcat:GroupMessage:123", "at_all": True},
            {"umo": "napcat:GroupMessage:456"},
        ],
        ["napcat"],
    )
    assert kept == [
        {"umo": "napcat:GroupMessage:123", "at_all": True},
        {"umo": "napcat:GroupMessage:456", "at_all": False},
    ]
    assert len(merged) == 1
    assert merged[0]["group_id"] == "123"
    assert merged[0]["kept"] == "napcat:GroupMessage:123"
    assert merged[0]["dropped"] == ["aiocqhttp:GroupMessage:123"]

    rows, report = sub.dedupe_rows(
        [
            {
                "uid": "111",
                "targets": [
                    {"umo": "aiocqhttp:GroupMessage:9", "at_all": False},
                    {"umo": "napcat:GroupMessage:9", "at_all": False},
                ],
            },
            {"uid": "222", "targets": [{"umo": "napcat:GroupMessage:8"}]},
            "不是对象",
            {"uid": "", "targets": []},
        ],
        ["napcat"],
    )
    assert [r["uid"] for r in rows] == ["111", "222"]
    assert rows[0]["targets"] == [{"umo": "napcat:GroupMessage:9", "at_all": False}]
    assert report == [
        {
            "uid": "111",
            "group_id": "9",
            "kept": "napcat:GroupMessage:9",
            "dropped": ["aiocqhttp:GroupMessage:9"],
            "duplicates": 0,
            "at_all": False,
        }
    ]


def test_dedupe_counts_exact_duplicates():
    """同一个写法写了两遍也算重复（否则配置变了却不报告）。"""
    kept, merged = sub.dedupe_targets(
        [
            {"umo": "napcat:GroupMessage:9", "at_all": False},
            {"umo": "napcat:GroupMessage:9", "at_all": True},
        ],
        ["napcat"],
    )
    assert kept == [{"umo": "napcat:GroupMessage:9", "at_all": True}]
    assert len(merged) == 1
    assert merged[0]["dropped"] == [] and merged[0]["duplicates"] == 1


def test_duplicate_targets_reports():
    rows = [
        {
            "uid": "111",
            "targets": [
                {"umo": "aiocqhttp:GroupMessage:9"},
                {"umo": "napcat:GroupMessage:9"},
                {"umo": "napcat:GroupMessage:8"},
            ],
        }
    ]
    dup = sub.duplicate_targets(rows)
    assert dup == [
        {"uid": "111", "group_id": "9", "umos": ["aiocqhttp:GroupMessage:9", "napcat:GroupMessage:9"]}
    ]
    assert sub.duplicate_targets([]) == []


# ---------------------------------------------------------------------------
# 面板接口：重复群的展示与合并
# ---------------------------------------------------------------------------


def test_subscriptions_reports_platform_and_duplicates():
    """用户那种情况：同一个群既有 aiocqhttp 写法又有 napcat 写法。"""
    plugin, ctrl, _ = make_case(
        subscriptions=["111=aiocqhttp:GroupMessage:879265474,napcat:GroupMessage:879265474"],
    )
    plugin.groups.remember("napcat:GroupMessage:879265474", group_name="公熊猫粉丝团")
    with_request(FakeRequest())
    ok, data, _ = unwrap(run(ctrl.get_subscriptions()))
    assert ok
    assert data["platforms"] == ["napcat", "default_102737249", "webchat"]
    targets = data["live"]["rows"][0]["targets"]
    assert [t["platform"] for t in targets] == ["aiocqhttp", "napcat"]
    # 只有 napcat 能路由，aiocqhttp 是死写法 → 面板会标红
    assert [t["reachable"] for t in targets] == [False, True]
    assert all(t["label"] == "公熊猫粉丝团（879265474）" for t in targets)
    assert data["duplicate_groups"] == 1
    assert data["duplicates"][0]["group_id"] == "879265474"
    assert data["duplicates"][0]["kind"] == "live"


def test_group_options_collapse_by_group_id():
    plugin, ctrl, _ = make_case(
        subscriptions=["111=aiocqhttp:GroupMessage:879265474"],
    )
    plugin.groups.remember("napcat:GroupMessage:879265474", group_name="公熊猫粉丝团")
    groups = ctrl._group_options()
    # 同一个群只出现一次（勾选后写入的是能路由的首选写法）
    assert len(groups) == 1
    assert groups[0]["group_id"] == "879265474"
    assert groups[0]["value"] == "napcat:GroupMessage:879265474"
    assert groups[0]["variant_count"] == 2
    assert sorted(groups[0]["variants"]) == [
        "aiocqhttp:GroupMessage:879265474",
        "napcat:GroupMessage:879265474",
    ]


def test_dedupe_subscriptions_merges_and_saves():
    """两种写法**都能路由**时，保留先出现的那个（不再看平台类型）。"""
    plugin, ctrl, _ = make_case(
        subscriptions=["111=default_102737249:GroupMessage:111,napcat:GroupMessage:111"],
        dynamic_subscriptions=["222=default_102737249:GroupMessage:222,default_102737249:GroupMessage:333"],
    )
    plugin.groups.remember("napcat:GroupMessage:111", group_name="狼人杀群")
    with_request(FakeRequest(body={}, method="POST"))
    ok, data, msg = unwrap(run(ctrl.dedupe_subscriptions()))
    assert ok
    assert data["rewritten"] == 0  # 两个前缀都是已加载实例，不用改写
    assert data["merged"] == 1
    assert plugin.config["subscriptions"] == ["111=default_102737249:111"]
    # 动态那类没有重复群 → 不写盘，配置原样不动
    assert plugin.config["dynamic_subscriptions"] == [
        "222=default_102737249:GroupMessage:222,default_102737249:GroupMessage:333"
    ]
    assert "合并了 1 组" in msg
    assert data["details"][0]["kept"] == "default_102737249:GroupMessage:111"
    assert data["details"][0]["dropped"] == ["napcat:GroupMessage:111"]

    # 再点一次：已经没得整理
    with_request(FakeRequest(body={}, method="POST"))
    ok, data, msg = unwrap(run(ctrl.dedupe_subscriptions()))
    assert ok and data["merged"] == 0 and "没有需要整理的写法" in msg


def test_dedupe_subscriptions_rewrites_dead_prefix():
    """只有一种写法、但前缀发不出去（aiocqhttp 不是已加载实例）→ 改写到同类型实例上。"""
    plugin, ctrl, _ = make_case(
        subscriptions=[
            "111=aiocqhttp:GroupMessage:111",
            "222=aiocqhttp:GroupMessage:222,aiocqhttp:GroupMessage:333",
            "333=napcat:GroupMessage:444",
        ],
    )
    with_request(FakeRequest(body={}, method="POST"))
    ok, data, msg = unwrap(run(ctrl.dedupe_subscriptions()))
    assert ok
    assert data["rewritten"] == 3  # 111/222/333 三条都是死写法
    assert data["merged"] == 0  # 群号各不相同，没有重复
    assert data["platform_types"] == {"aiocqhttp": "napcat", "webchat": "webchat"}
    lines = plugin.config["subscriptions"]
    assert "111=napcat:111" in lines
    assert "222=napcat:222,napcat:333" in lines
    # 已经是有效实例 id 的写法不动
    assert "333=napcat:444" in lines
    assert "修正了 3 条" in msg
    assert data["rewrites"][0]["from"] == "aiocqhttp:GroupMessage:111"
    assert data["rewrites"][0]["to"] == "napcat:GroupMessage:111"


def test_dedupe_subscriptions_rewrite_then_merge():
    """先改写失效前缀，再合并同一个群的多种写法。"""
    plugin, ctrl, _ = make_case(
        subscriptions=[
            "111=aiocqhttp:GroupMessage:9,napcat:GroupMessage:9",
            "222=aiocqhttp:GroupMessage:8",
        ],
    )
    with_request(FakeRequest(body={}, method="POST"))
    ok, data, _ = unwrap(run(ctrl.dedupe_subscriptions()))
    assert ok
    # 111 那条：aiocqhttp 先改写成 napcat → 与原有 napcat 重复 → 合并成一个
    assert data["merged"] == 1
    assert data["rewritten"] == 2
    assert "111=napcat:9" in plugin.config["subscriptions"]
    assert "222=napcat:8" in plugin.config["subscriptions"]


def test_dedupe_skips_rewrite_when_no_platforms_known():
    """探不到平台时不敢猜，什么都不改。"""
    plugin, ctrl, _ = make_case(subscriptions=["111=aiocqhttp:GroupMessage:9"])
    plugin._platform_instances = lambda: []
    plugin._platform_ids = lambda: []
    with_request(FakeRequest(body={}, method="POST"))
    ok, data, msg = unwrap(run(ctrl.dedupe_subscriptions()))
    assert ok
    assert data["rewritten"] == 0 and data["merged"] == 0
    assert plugin.config["subscriptions"] == ["111=aiocqhttp:GroupMessage:9"]
    assert "没有需要整理的写法" in msg


def test_dedupe_subscriptions_single_kind_and_unsupported():
    plugin, ctrl, _ = make_case(
        subscriptions=["111=aiocqhttp:GroupMessage:9,napcat:GroupMessage:9"],
        dynamic_subscriptions=["222=aiocqhttp:GroupMessage:8,napcat:GroupMessage:8"],
    )
    with_request(FakeRequest(body={"kind": "dynamic"}, method="POST"))
    ok, data, _ = unwrap(run(ctrl.dedupe_subscriptions()))
    assert ok and data["merged"] == 1
    # 只处理了 dynamic：开播那类原样没动
    assert plugin.config["subscriptions"] == [
        "111=aiocqhttp:GroupMessage:9,napcat:GroupMessage:9"
    ]
    assert plugin.config["dynamic_subscriptions"] == ["222=napcat:8"]

    plugin2, ctrl2, _ = make_case(
        subscriptions=["111=aiocqhttp:GroupMessage:9,napcat:GroupMessage:9"],
    )
    plugin2._save_subs = None  # 模拟老版本插件没有保存方法
    with_request(FakeRequest(body={}, method="POST"))
    ok, _, msg = unwrap(run(ctrl2.dedupe_subscriptions()))
    assert not ok and "不支持" in msg


def test_notify_rows_collapse_and_save_all_variants():
    plugin, ctrl, _ = make_case(
        subscriptions=["111=aiocqhttp:GroupMessage:777,napcat:GroupMessage:777"],
        group_settings={"napcat:GroupMessage:777": {"notify_end": False}},
    )
    plugin.groups.remember("napcat:GroupMessage:777", group_name="天刀群")

    with_request(FakeRequest())
    ok, data, _ = unwrap(run(ctrl.get_notify()))
    assert ok
    assert len(data["rows"]) == 1  # 一个群一行
    row = data["rows"][0]
    assert row["group_id"] == "777"
    assert row["umo"] == "napcat:GroupMessage:777"  # 首选（能路由的）写法
    assert sorted(row["umos"]) == ["aiocqhttp:GroupMessage:777", "napcat:GroupMessage:777"]
    assert row["variant_count"] == 2
    assert row["notify"] is True and row["notify_end"] is False
    assert row["mixed"] is True  # 两种写法设置不一致时会提示

    # 保存时两种写法一起改，避免「改了没反应」
    with_request(
        FakeRequest(
            body={"umo": "aiocqhttp:GroupMessage:777", "kind": "notify_end", "value": True},
            method="POST",
        )
    )
    ok, data, msg = unwrap(run(ctrl.save_notify()))
    assert ok
    assert plugin.group_settings["aiocqhttp:GroupMessage:777"]["notify_end"] is True
    assert plugin.group_settings["napcat:GroupMessage:777"]["notify_end"] is True
    assert len(data["umos"]) == 2 and "已同步 2 种写法" in msg


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
