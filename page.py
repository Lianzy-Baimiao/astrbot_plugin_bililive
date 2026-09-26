# -*- coding: utf-8 -*-
"""B 站开播监测插件的 Web 面板后端接口（AstrBot Plugin Pages）。

路由挂在 ``/astrbot_plugin_bililive/page/*`` 下，前端 ``pages/bililive-panel/`` 通过
``window.AstrBotPluginPage`` 的 apiGet / apiPost 调用（bridge 会自动补插件名前缀）。

    GET  /page/meta                 常量与运行参数（间隔、上限、静音时段）
    GET  /page/status               运行状态（会话/任务/限流退避/Cookie/无效配置行）
    GET  /page/subscriptions        开播 + 动态订阅（UP 主 × 群 矩阵）+ 候选群
    POST /page/subscriptions/save   保存某一类订阅（写回 config，走插件的保存与剪枝）
    GET  /page/notify               按群的通知开关（开播/关播/动态）
    POST /page/notify               改一个群的通知开关
    GET  /page/groups               会话列表（带群名），refresh=1 现问平台

订阅在配置里是 ``UID=平台id:群号[@all],...`` 这样的文本行，面板里改成「一行一个 UP、
每行勾选要推的群」的矩阵，保存时用 ``subscription.subs_from_rows`` 还原成内部结构，
再交给插件既有的 ``_save_subs`` / ``_save_dyn_subs``（它们负责序列化、落盘和 groups.json
剪枝），保证与聊天命令改的是同一份数据。

AstrBot 4.26+ 提供 astrbot.api.web，更早的版本只有裸 quart，这里统一包一层兼容。
"""

from __future__ import annotations

import time
from typing import Any, Callable

try:  # AstrBot >= 4.26
    from astrbot.api.web import error_response, json_response, request

    _HAS_WEB_API = True
except (ImportError, AttributeError):  # 老版本回落到 quart
    _HAS_WEB_API = False
    try:
        from quart import jsonify as _quart_jsonify
        from quart import request  # type: ignore[assignment]
    except ImportError:  # 本地单测环境两个都没有
        request = None  # type: ignore[assignment]
        _quart_jsonify = None  # type: ignore[assignment]

    def json_response(  # type: ignore[misc]
        data: Any = None,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if _quart_jsonify is None:
            return {"status_code": status_code, "data": data}
        resp = _quart_jsonify(data)
        resp.status_code = status_code
        for key, value in (headers or {}).items():
            resp.headers[key] = value
        return resp

    def error_response(  # type: ignore[misc]
        message: str = "",
        *,
        status_code: int = 400,
        data: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return json_response(
            {"status": "error", "message": message, "data": data if data is not None else {}},
            status_code=status_code,
            headers=headers,
        )


from . import subscription as sub
from .groups import GroupNameResolver, session_label

PLUGIN_NAME = "astrbot_plugin_bililive"

# 订阅类别：与 config 的两个列表一一对应
KIND_LIVE = "live"
KIND_DYNAMIC = "dynamic"
KIND_CONFIG_KEY = {KIND_LIVE: "subscriptions", KIND_DYNAMIC: "dynamic_subscriptions"}

# 按群通知开关的种类（groups.json 里的键）
NOTIFY_KINDS = ("notify", "notify_end", "notify_dyn")
NOTIFY_LABELS = {"notify": "开播", "notify_end": "关播", "notify_dyn": "动态"}

# 标记：面板没传就沿用旧值
UNSET = object()


def _log_warn(message: str) -> None:
    try:
        from astrbot.api import logger

        logger.warning(f"[bililive] {message}")
    except Exception:
        pass


def _fmt_time(ts: float) -> str:
    if not ts or ts <= 0:
        return "从未"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (OverflowError, OSError, ValueError):
        return "未知"


class BiliLivePageController:
    """把开播监测插件的能力包成 HTTP 接口。"""

    def __init__(self, context: Any, plugin: Any = None) -> None:
        self.context = context
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register_routes(self) -> None:
        routes: list[tuple[str, Callable[..., Any], list[str], str]] = [
            ("/page/meta", self.get_meta, ["GET"], "B站监测：常量与运行参数"),
            ("/page/status", self.get_status, ["GET"], "B站监测：运行状态"),
            ("/page/subscriptions", self.get_subscriptions, ["GET"], "B站监测：订阅矩阵"),
            ("/page/subscriptions/save", self.save_subscriptions, ["POST"], "B站监测：保存订阅"),
            ("/page/subscriptions/dedupe", self.dedupe_subscriptions, ["POST"], "B站监测：合并重复群"),
            ("/page/notify", self.get_notify, ["GET"], "B站监测：按群通知开关"),
            ("/page/notify/save", self.save_notify, ["POST"], "B站监测：保存通知开关"),
            ("/page/groups", self.get_groups, ["GET"], "B站监测：会话列表（带群名）"),
        ]
        for path, handler, methods, desc in routes:
            try:
                self.context.register_web_api(
                    f"/{PLUGIN_NAME}{path}", handler, methods, desc
                )
            except Exception as exc:  # 老版本 AstrBot / 单测：注册不上也不该炸插件
                _log_warn(f"注册接口 {path} 失败: {exc}")

    # ------------------------------------------------------------------
    # 请求 / 响应小工具
    # ------------------------------------------------------------------

    @staticmethod
    def _ok(data: Any = None, message: str = "") -> Any:
        payload: dict[str, Any] = {
            "status": "ok",
            "data": data if data is not None else {},
        }
        if message:
            payload["message"] = message
        return json_response(payload)

    @staticmethod
    def _err(message: str, status_code: int = 400) -> Any:
        return error_response(message, status_code=status_code)

    @staticmethod
    def _query_get(key: str, default: str = "") -> str:
        if request is None:
            return default
        for holder in ("query", "args"):
            bag = getattr(request, holder, None)
            if bag is None:
                continue
            try:
                value = bag.get(key, default)
            except Exception:
                continue
            if value is not None:
                return str(value)
        return default

    @staticmethod
    async def _read_json() -> dict[str, Any]:
        if request is None:
            return {}
        for method_name in ("json", "get_json"):
            method = getattr(request, method_name, None)
            if not callable(method):
                continue
            try:
                data = method()
                if hasattr(data, "__await__"):
                    data = await data
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
        return {}

    async def _payload(self) -> dict[str, Any]:
        payload = await self._read_json()
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------
    # 插件侧读值
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default: Any = None) -> Any:
        config = getattr(self.plugin, "config", None)
        try:
            value = config.get(key, default)  # type: ignore[union-attr]
        except Exception:
            return default
        return default if value is None else value

    def _resolver(self) -> GroupNameResolver | None:
        resolver = getattr(self.plugin, "groups", None)
        return resolver if isinstance(resolver, GroupNameResolver) else None

    def _label(self, umo: str) -> str:
        resolver = self._resolver()
        return session_label(umo) if resolver is None else resolver.label(umo)

    def _resolver_entries(self) -> dict[str, dict[str, Any]]:
        resolver = self._resolver()
        return resolver.entries() if resolver is not None else {}

    def _load(self, kind: str) -> dict[str, Any]:
        """读某一类订阅（插件每次现读配置，保证与 WebUI/命令改动同步）。"""
        loader = "_load_subs" if kind == KIND_LIVE else "_load_dyn_subs"
        func = getattr(self.plugin, loader, None)
        if not callable(func):
            return {}
        try:
            data = func()
        except Exception as exc:
            _log_warn(f"读取 {kind} 订阅失败: {exc}")
            return {}
        return data if isinstance(data, dict) else {}

    def _invalid_lines(self, kind: str) -> list[str]:
        getter = "_invalid_sub_lines" if kind == KIND_LIVE else "_invalid_dyn_sub_lines"
        func = getattr(self.plugin, getter, None)
        if not callable(func):
            return []
        try:
            return [str(x) for x in (func() or [])]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # 接口：订阅矩阵
    # ------------------------------------------------------------------

    def _group_options(self) -> list[dict[str, Any]]:
        """候选群：按**群号**归并（同一群的不同平台写法合成一项，避免勾出重复）。"""
        rows: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        variants: dict[str, list[str]] = {}
        for kind in (KIND_LIVE, KIND_DYNAMIC):
            for row in sub.subs_to_rows(self._load(kind)):
                for target in row["targets"]:
                    umo = target["umo"]
                    gid = sub.group_id_of(umo) or umo
                    if gid not in variants:
                        variants[gid] = []
                        order.append(gid)
                    if umo not in variants[gid]:
                        variants[gid].append(umo)
        entries = self._resolver_entries()

        def add(umo: str, source: str) -> None:
            if not umo:
                return
            gid = sub.group_id_of(umo) or umo
            if gid not in variants:
                variants[gid] = []
                order.append(gid)
            if umo not in variants[gid]:
                variants[gid].append(umo)
            if source == "referenced" or gid not in rows:
                entry = entries.get(umo) or {}
                rows[gid] = {
                    "group_id": gid,
                    "group_name": str(entry.get("group_name") or ""),
                    "source": source,
                }

        for gid in list(order):
            for umo in variants[gid]:
                add(umo, "referenced")
        for umo in entries:
            add(umo, "platform")

        known = self._platform_ids()
        out: list[dict[str, Any]] = []
        for gid in order:
            info = rows.get(gid) or {"group_id": gid, "group_name": "", "source": "platform"}
            umos = variants.get(gid) or []
            chosen = sub.pick_umo(umos, known) if umos else ""
            if not chosen:
                continue
            preferred = self._label(chosen)
            out.append(
                {
                    # value 是「首选写法」：勾选后写进配置的就是它
                    "value": chosen,
                    "label": preferred,
                    "group_id": gid,
                    "group_name": info["group_name"],
                    "source": info["source"],
                    "variants": umos,
                    "variant_count": len(umos),
                }
            )
        out.sort(
            key=lambda item: (
                item["source"] != "referenced",
                not bool(item["group_name"]),
                item["group_id"] or item["label"],
            )
        )
        return out

    async def get_subscriptions(self) -> Any:
        known = self._platform_ids()
        data: dict[str, Any] = {}
        duplicates: list[dict[str, Any]] = []
        for kind in (KIND_LIVE, KIND_DYNAMIC):
            subs = self._load(kind)
            rows = sub.subs_to_rows(subs)
            for row in rows:
                for target in row["targets"]:
                    target.update(self._target(target["umo"], target.get("at_all", False)))
            data[kind] = {
                "rows": rows,
                "uids": len(rows),
                "group_links": sum(row["group_count"] for row in rows),
                "invalid_lines": self._invalid_lines(kind),
                "config_key": KIND_CONFIG_KEY[kind],
            }
            for item in sub.duplicate_targets(rows):
                duplicates.append({"kind": kind, **item})
        data["groups"] = self._group_options()
        data["max_monitors"] = self._int_attr("max_monitors", 50)
        data["default_platform"] = str(getattr(self.plugin, "default_platform", "") or "")
        data["platforms"] = known
        data["kinds"] = [KIND_LIVE, KIND_DYNAMIC]
        data["duplicates"] = duplicates
        data["duplicate_groups"] = len({(d.get("kind"), d.get("group_id")) for d in duplicates})
        return self._ok(data)

    async def save_subscriptions(self) -> Any:
        """保存一类订阅：面板行 → 内部结构 → 插件既有的序列化落盘 + 剪枝。"""
        if self.plugin is None:
            return self._err("插件未就绪", status_code=503)
        payload = await self._payload()
        kind = str(payload.get("kind", KIND_LIVE) or KIND_LIVE).strip().lower()
        if kind not in KIND_CONFIG_KEY:
            return self._err("kind 只能是 live 或 dynamic")
        rows = payload.get("rows")
        if not isinstance(rows, (list, tuple)):
            return self._err("rows 需要是数组")

        max_monitors = self._int_attr("max_monitors", 50)
        subs, dropped = sub.subs_from_rows(list(rows))
        if len(subs) > max_monitors:
            return self._err(f"订阅的 UP 主最多 {max_monitors} 个（当前 {len(subs)} 个）")

        saver_name = "_save_subs" if kind == KIND_LIVE else "_save_dyn_subs"
        saver = getattr(self.plugin, saver_name, None)
        if not callable(saver):
            return self._err("当前版本不支持从面板保存订阅，请更新插件", status_code=501)
        try:
            await saver(subs)
        except Exception as exc:
            _log_warn(f"保存 {kind} 订阅失败: {exc}")
            return self._err(f"保存失败：{exc}", status_code=500)

        saved = self._load(kind)
        links = sum(len(v.get("groups", [])) for v in saved.values())
        message = f"已保存 {len(saved)} 个 UP 主、{links} 条群订阅"
        if dropped:
            message += f"；{len(dropped)} 行 UID 非法已跳过（{', '.join(dropped[:3])}）"
        return self._ok(
            {
                "kind": kind,
                "uids": len(saved),
                "group_links": links,
                "dropped": dropped,
                "lines": list(self._cfg(KIND_CONFIG_KEY[kind], []) or []),
            },
            message,
        )

    async def dedupe_subscriptions(self) -> Any:
        """整理订阅里的「重复写法」和「发不出去的写法」。

        两步（都是显式操作，只有面板上点按钮才跑，保存订阅时不会偷偷改数据）：

        1. **改写失效前缀**：前缀不是已加载平台实例的（例如配置里填了适配器类型名
           ``aiocqhttp``，实际实例 id 是 ``napcat``），改写到同类型的实例 id 上；
        2. **合并重复群**：同一个群号有多个写法时只留一个（优先能发出去的那个）。

        ``reachable``/``type_to_instance`` 都依赖已加载平台，探不到平台时这一步直接跳过。
        """
        if self.plugin is None:
            return self._err("插件未就绪", status_code=503)
        payload = await self._payload()
        wanted = str(payload.get("kind", "") or "").strip().lower()
        kinds = [wanted] if wanted in KIND_CONFIG_KEY else [KIND_LIVE, KIND_DYNAMIC]
        known = self._platform_ids()
        type_map = self._platform_type_map()

        details: list[dict[str, Any]] = []
        rewritten: list[dict[str, Any]] = []
        for kind in kinds:
            rows = sub.subs_to_rows(self._load(kind))
            fixed_rows, rewrites = sub.rewrite_prefixes(rows, known, type_map)
            new_rows, merged = sub.dedupe_rows(fixed_rows, known)
            if not merged and not rewrites:
                continue
            saver = getattr(
                self.plugin, "_save_subs" if kind == KIND_LIVE else "_save_dyn_subs", None
            )
            if not callable(saver):
                return self._err("当前版本不支持从面板保存订阅，请更新插件", status_code=501)
            subs2, _dropped = sub.subs_from_rows(new_rows)
            try:
                await saver(subs2)
            except Exception as exc:
                _log_warn(f"整理订阅后保存失败: {exc}")
                return self._err(f"保存失败：{exc}", status_code=500)
            for item in rewrites:
                rewritten.append(
                    {
                        "kind": kind,
                        "uid": item["uid"],
                        "group_id": item["group_id"],
                        "from": item["from"],
                        "to": item["to"],
                        "label": self._label(item["to"]),
                    }
                )
            for item in merged:
                details.append(
                    {
                        "kind": kind,
                        "uid": item["uid"],
                        "group_id": item["group_id"],
                        "kept": item["kept"],
                        "kept_label": self._label(item["kept"]),
                        "dropped": item["dropped"],
                        "dropped_labels": [self._label(u) for u in item["dropped"]],
                    }
                )

        if not details and not rewritten:
            message = "没有需要整理的写法"
        else:
            parts = []
            if rewritten:
                parts.append(f"修正了 {len(rewritten)} 条发不出去的写法")
            if details:
                parts.append(f"合并了 {len(details)} 组重复群")
            message = "，".join(parts)
        return self._ok(
            {
                "merged": len(details),
                "rewritten": len(rewritten),
                "details": details,
                "rewrites": rewritten,
                "platforms": known,
                "platform_types": type_map,
            },
            message,
        )

    def _platform_type_map(self) -> dict[str, str]:
        """适配器类型名 → 实例 id（同名只取第一个），用于把失效前缀改写回实例 id。"""
        instances = getattr(self.plugin, "_platform_instances", None)
        pairs = []
        if callable(instances):
            try:
                pairs = list(instances() or [])
            except Exception:
                pairs = []
        mapping: dict[str, str] = {}
        for pid, ptype in pairs:
            pid, ptype = str(pid).strip(), str(ptype).strip()
            if pid and ptype and ptype not in mapping:
                mapping[ptype] = pid
        return mapping

    # ------------------------------------------------------------------
    # 接口：按群通知开关
    # ------------------------------------------------------------------

    def _int_attr(self, name: str, default: int) -> int:
        try:
            return int(getattr(self.plugin, name, default))
        except (TypeError, ValueError):
            return default

    def _notify_rows(self) -> list[dict[str, Any]]:
        """按**群号**归并的通知开关行：同一群的不同写法合成一行，改一次全部生效。"""
        settings = getattr(self.plugin, "group_settings", None) or {}
        variants: dict[str, list[str]] = {}
        order: list[str] = []

        def add(umo: str) -> None:
            umo = str(umo or "").strip()
            if not umo:
                return
            gid = sub.group_id_of(umo) or umo
            if gid not in variants:
                variants[gid] = []
                order.append(gid)
            if umo not in variants[gid]:
                variants[gid].append(umo)

        for kind in (KIND_LIVE, KIND_DYNAMIC):
            for row in sub.subs_to_rows(self._load(kind)):
                for target in row["targets"]:
                    add(target["umo"])
        for umo in settings:
            add(str(umo))

        read = getattr(self.plugin, "_group_notify_enabled", None)
        known = self._platform_ids()
        rows: list[dict[str, Any]] = []
        for gid in order:
            umos = variants.get(gid) or []
            chosen = sub.pick_umo(umos, known) or (umos[0] if umos else gid)
            item: dict[str, Any] = {
                # umo 是「首选写法」，umos 是这个群的全部写法（保存时一起改）
                "umo": chosen,
                "umos": umos,
                "group_id": gid,
                "variant_count": len(umos),
                "label": self._label(chosen),
            }
            mixed = False

            def state_of(umo: str, kind: str) -> bool:
                try:
                    return bool(read(umo, kind)) if callable(read) else True
                except Exception:
                    return True

            for kind in NOTIFY_KINDS:
                states = [state_of(umo, kind) for umo in umos]
                # 显示「实际会发生什么」：以首选（能路由的）写法的设置为准
                item[kind] = state_of(chosen, kind)
                if len(set(states)) > 1:
                    mixed = True
            item["mixed"] = mixed
            rows.append(item)
        rows.sort(key=lambda r: r["label"])
        return rows

    async def get_notify(self) -> Any:
        return self._ok(
            {
                "rows": self._notify_rows(),
                "kinds": [{"value": k, "label": NOTIFY_LABELS[k]} for k in NOTIFY_KINDS],
                "global": {
                    "enable_notifications": bool(
                        getattr(self.plugin, "enable_notifications", True)
                    ),
                    "enable_end_notifications": bool(
                        getattr(self.plugin, "enable_end_notifications", True)
                    ),
                },
            }
        )

    async def save_notify(self) -> Any:
        if self.plugin is None:
            return self._err("插件未就绪", status_code=503)
        payload = await self._payload()
        umo = str(payload.get("umo", "") or "").strip()
        kind = str(payload.get("kind", "") or "").strip()
        if not umo:
            return self._err("缺少 umo")
        if kind not in NOTIFY_KINDS:
            return self._err(f"kind 只能是 {'/'.join(NOTIFY_KINDS)}")
        value = payload.get("value")
        if not isinstance(value, bool):
            return self._err("value 需要是布尔值")
        setter = getattr(self.plugin, "_set_group_notify", None)
        if not callable(setter):
            return self._err("当前版本不支持从面板改通知开关", status_code=501)

        # 同一个群可能有多种平台写法：这次开关对它们全部生效，避免「改了没反应」
        gid = sub.group_id_of(umo) or umo
        targets = [umo]
        for row in self._notify_rows():
            if row.get("group_id") == gid:
                targets = list(row.get("umos") or [umo])
                break
        errors = []
        for target in targets:
            try:
                setter(target, kind, value)
            except Exception as exc:  # 单个失败不影响其它写法
                errors.append(f"{target}: {exc}")
        if len(errors) == len(targets):
            _log_warn(f"保存通知开关失败: {errors}")
            return self._err(f"保存失败：{errors[0]}", status_code=500)
        return self._ok(
            {
                "umo": umo,
                "umos": targets,
                "kind": kind,
                "value": value,
                "label": self._label(umo),
            },
            f"{self._label(umo)} 的{NOTIFY_LABELS[kind]}通知已{'开启' if value else '关闭'}"
            + (f"（已同步 {len(targets)} 种写法）" if len(targets) > 1 else ""),
        )

    # ------------------------------------------------------------------
    # 接口：状态与常量
    # ------------------------------------------------------------------

    def _task_alive(self, name: str) -> bool:
        task = getattr(self.plugin, name, None)
        if task is None:
            return False
        done = getattr(task, "done", None)
        try:
            return not bool(done()) if callable(done) else True
        except Exception:
            return True

    def _quiet_state(self) -> dict[str, Any]:
        raw = str(self._cfg("quiet_hours", "") or "").strip()
        state: dict[str, Any] = {"raw": raw, "valid": False, "active": False}
        if not raw:
            return state
        parser = getattr(self.plugin, "_parse_quiet_hours", None)
        parsed = None
        if callable(parser):
            try:
                parsed = parser()
            except Exception:
                parsed = None
        state["valid"] = parsed is not None
        if state["valid"]:
            checker = getattr(self.plugin, "_in_quiet_hours", None)
            if callable(checker):
                try:
                    state["active"] = bool(checker())
                except Exception:
                    state["active"] = False
        return state

    def _platform_ids(self) -> list[str]:
        """已加载平台的**实例 id**（send_message 按它匹配，所以只有这些前缀能真正发出去）。"""
        getter = getattr(self.plugin, "_platform_ids", None)
        if callable(getter):
            try:
                return [str(x) for x in (getter() or []) if str(x).strip()]
            except Exception:
                return []
        return []

    def _target(self, umo: str, at_all: bool = False) -> dict[str, Any]:
        """一个推送目标：带上平台前缀和「这个前缀能不能路由」的判断。"""
        platform = str(umo).split(":")[0]
        known = self._platform_ids()
        return {
            "umo": umo,
            "label": self._label(umo),
            "platform": platform,
            "at_all": bool(at_all),
            # 已知平台为空（老版本探测不到）时不报错，避免面板满屏 ⚠
            "reachable": (platform in known) if known else True,
        }

    async def get_status(self) -> Any:
        if self.plugin is None:
            return self._err("插件未就绪", status_code=503)
        plugin = self.plugin
        session = getattr(plugin, "session", None)
        session_ok = bool(session is not None and not getattr(session, "closed", True))
        live_subs = self._load(KIND_LIVE)
        dyn_subs = self._load(KIND_DYNAMIC)
        live_links = sum(len(v.get("groups", [])) for v in live_subs.values())
        dyn_links = sum(len(v.get("groups", [])) for v in dyn_subs.values())
        check_interval = self._int_attr("check_interval", 60)
        current_interval = self._int_attr("current_interval", check_interval)
        dyn_interval = self._int_attr("dynamic_check_interval", 45)
        dyn_current = self._int_attr("dyn_current_interval", dyn_interval)
        return self._ok(
            {
                "session_ok": session_ok,
                "monitor_running": self._task_alive("monitor_task"),
                "dyn_monitor_running": self._task_alive("dyn_monitor_task"),
                "check_interval": check_interval,
                "current_interval": current_interval,
                "dynamic_check_interval": dyn_interval,
                "dyn_current_interval": dyn_current,
                "backoff_live": current_interval != check_interval,
                "backoff_dynamic": dyn_current != dyn_interval,
                "monitors": len(live_subs),
                "monitor_links": live_links,
                "dyn_monitors": len(dyn_subs),
                "dyn_links": dyn_links,
                "dyn_baseline": len(getattr(plugin, "dyn_last_ids", {}) or {}),
                "live_status_cache": len(getattr(plugin, "live_status_cache", {}) or {}),
                "max_monitors": self._int_attr("max_monitors", 50),
                "default_platform": str(getattr(plugin, "default_platform", "") or ""),
                "platforms": self._platform_ids(),
                "cookie_set": bool(str(self._cfg("bilibili_cookie", "") or "").strip()),
                "quiet": self._quiet_state(),
                "global_notify": {
                    "live": bool(getattr(plugin, "enable_notifications", True)),
                    "end": bool(getattr(plugin, "enable_end_notifications", True)),
                },
                "invalid_lines": {
                    KIND_LIVE: self._invalid_lines(KIND_LIVE),
                    KIND_DYNAMIC: self._invalid_lines(KIND_DYNAMIC),
                },
                "data_dir": str(getattr(plugin, "data_dir", "") or ""),
                "group_settings_count": len(getattr(plugin, "group_settings", {}) or {}),
            }
        )

    async def get_groups(self) -> Any:
        """会话列表（带群名）。refresh=1 时先去平台要一遍群列表。"""
        refresh = self._query_get("refresh", "0").strip().lower() in {"1", "true", "yes"}
        refreshed = 0
        if refresh and self.plugin is not None:
            refresher = getattr(self.plugin, "refresh_group_names", None)
            if callable(refresher):
                try:
                    refreshed = int(await refresher(force=True) or 0)
                except Exception as exc:
                    _log_warn(f"刷新群列表失败: {exc}")
        groups = self._group_options()
        named = len([g for g in groups if g["group_name"]])
        return self._ok(
            {
                "groups": groups,
                "total": len(groups),
                "named": named,
                "refreshed": refreshed,
            }
        )

    async def get_meta(self) -> Any:
        return self._ok(
            {
                "plugin": PLUGIN_NAME,
                "kinds": [KIND_LIVE, KIND_DYNAMIC],
                "kinds_config": KIND_CONFIG_KEY,
                "notify_kinds": [{"value": k, "label": NOTIFY_LABELS[k]} for k in NOTIFY_KINDS],
                "limits": {
                    "check_interval": [30, 600],
                    "dynamic_check_interval": [30, 600],
                    "max_monitors": self._int_attr("max_monitors", 50),
                },
            }
        )
