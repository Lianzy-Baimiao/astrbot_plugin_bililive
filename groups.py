# -*- coding: utf-8 -*-
"""群名解析：把 unified_msg_origin 变成「群名（群号）」，给面板的范围下拉显示。

QQ 的 OneBot 群消息事件本身不带群名，所以面板里原来只能看到一串数字群号。这个模块
把「群号 → 群名」记在本地文件里，名字来源有两处：

- 消息事件自带的群名（Telegram / Discord / QQ 官方等平台有）：``remember()`` + source=event
- 平台适配器查出来的群名（OneBot 的 get_group_list / get_group_info）：``merge_api_groups()``

数据落在 data/plugin_data/astrbot_plugin_bililive/groups.json：

    {
      "version": 1,
      "updated_at": 1766000000.0,
      "groups": {
        "napcat:GroupMessage:339466990": {
          "platform_id": "napcat",
          "group_id": "339466990",
          "group_name": "魔兽世界交流群",
          "member_count": 486,
          "source": "api",
          "updated_at": 1766000000.0
        }
      }
    }

这一层不 import astrbot：真正去问平台要群列表的活在 main.py，这里只负责解析返回、
缓存、以及拼展示文字，所以可以脱离 astrbot 直接单测。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

GROUP_MSG_TYPE = "GroupMessage"
FRIEND_MSG_TYPE = "FriendMessage"

STORE_VERSION = 1

# 名字从哪来：平台接口问出来的比事件里带的可信度一样，都覆盖旧值
SOURCE_EVENT = "event"
SOURCE_API = "api"

# 缓存条数上限，超了按 updated_at 淘汰最旧的，避免长期运行无限膨胀
MAX_GROUP_ENTRIES = 800

# 面板「刷新群列表」两次真刷之间的最小间隔（秒），防止连点打爆平台接口
REFRESH_INTERVAL = 300


# ---------------------------------------------------------------------------
# umo 小工具
# ---------------------------------------------------------------------------


def split_umo(umo: str) -> tuple[str, str, str]:
    """拆 unified_msg_origin → (platform_id, message_type, session_id)。

    段数不够时从左边补空串再按右对齐取值，所以裸群号 "339466990" 会拆成
    ("", "", "339466990")，不会把群号当成平台名。段数超过 3 段的（umo 里带冒号
    的罕见情况）把多余段还原回 session_id。
    """
    text = str(umo or "").strip()
    if not text:
        return "", "", ""
    parts = [p.strip() for p in text.split(":")]
    if len(parts) == 1:
        return "", "", parts[0]
    if len(parts) == 2:
        return "", parts[0], parts[1]
    return parts[0], parts[1], ":".join(parts[2:])


def umo_for(platform_id: str, group_id: str, message_type: str = GROUP_MSG_TYPE) -> str:
    """按 AstrBot 的约定拼 unified_msg_origin；没有平台 id 时退回裸号。"""
    gid = str(group_id or "").strip()
    if not gid:
        return ""
    plat = str(platform_id or "").strip()
    if not plat:
        return gid
    kind = str(message_type or GROUP_MSG_TYPE).strip() or GROUP_MSG_TYPE
    return f"{plat}:{kind}:{gid}"


def group_id_from_umo(umo: str) -> str:
    """取会话号（群号 / 私聊 QQ 号）。"""
    return split_umo(umo)[2]


def name_of(entry: Mapping[str, Any] | None) -> str:
    """从缓存条目里取群名，没有就返回空串。"""
    if not isinstance(entry, Mapping):
        return ""
    return str(entry.get("group_name") or "").strip()


def session_label(umo: str, entry: Mapping[str, Any] | None = None) -> str:
    """面板下拉里显示的一行文字。

    群：``群名（群号）``，没名字退成 ``群 群号``；
    私聊：``私聊 QQ号``（私聊本来也没名字）；
    其它认不出来的：原样显示 umo，至少不会显示错。
    """
    _, kind, sid = split_umo(umo)
    display = name_of(entry)
    kind_lower = kind.lower()
    if kind_lower == "groupmessage":
        return f"{display}（{sid}）" if display else f"群 {sid}"
    if kind_lower == "friendmessage":
        return f"{display}（{sid}）" if display else f"私聊 {sid}"
    if not kind and sid.isdigit():
        # 手填的裸群号
        return f"{display}（{sid}）" if display else f"群 {sid}"
    return display or sid or str(umo or "")


# ---------------------------------------------------------------------------
# 平台返回值解析（纯函数，main.py 拿到 call_action 的结果后套一遍）
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _pick_list(result: Mapping[str, Any]) -> list[Any]:
    """OneBot 各家实现把群列表塞在不同位置，挨个试一遍。"""
    for key in ("group_list", "groups", "list"):
        raw = result.get(key)
        if isinstance(raw, list):
            return raw
    return []


def parse_group_list(result: Any) -> list[dict[str, Any]]:
    """把 ``get_group_list`` 的返回归一成 [{group_id, group_name, member_count}]。

    兼容三种形状：裸数组、``{"data": [...]}``、``{"data": {"group_list": [...]}}``
    （NapCat / LLOneBot / Lagrange / go-cqhttp 各不相同）。
    """
    items: list[Any] = []
    if isinstance(result, (list, tuple)):
        items = list(result)
    elif isinstance(result, Mapping):
        data = result.get("data")
        if isinstance(data, list):
            items = data
        elif isinstance(data, Mapping):
            items = _pick_list(data)
        if not items:
            items = _pick_list(result)

    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        gid = str(item.get("group_id") or item.get("id") or "").strip()
        if not gid:
            continue
        out.append(
            {
                "group_id": gid,
                "group_name": str(
                    item.get("group_name") or item.get("name") or ""
                ).strip(),
                "member_count": _as_int(
                    item.get("member_count") or item.get("memberCount")
                ),
            }
        )
    return out


def parse_group_info(result: Any) -> dict[str, Any]:
    """把 ``get_group_info`` 的返回归一成 {group_id, group_name, member_count}。"""
    rows = parse_group_list([result] if isinstance(result, Mapping) else result)
    if rows:
        return rows[0]
    if isinstance(result, Mapping):
        data = result.get("data")
        if data is not result and data is not None:
            rows = parse_group_list(
                [data] if isinstance(data, Mapping) else data
            )
            if rows:
                return rows[0]
    return {}



# ---------------------------------------------------------------------------
# 缓存本体
# ---------------------------------------------------------------------------


class GroupNameResolver:
    """umo → 群名 的本地缓存，改动才落盘（原子写，同 store.py）。"""

    def __init__(self, path: str | Path, *, max_entries: int = MAX_GROUP_ENTRIES) -> None:
        self.path = Path(path)
        self.max_entries = max(1, int(max_entries))
        self._groups: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._last_refresh = 0.0

    # ---------------- 读写盘 ----------------

    def load(self) -> None:
        """同步读盘。文件不存在或坏了都当空库，不抛异常。"""
        self._groups = {}
        self._last_refresh = 0.0
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
            if isinstance(raw, dict):
                groups = raw.get("groups")
                if isinstance(groups, dict):
                    for umo, entry in groups.items():
                        clean = self._sanitize(umo, entry)
                        if clean:
                            self._groups[clean["_key"]] = clean
                self._last_refresh = self._as_float(raw.get("last_refresh"))
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    @staticmethod
    def _as_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _sanitize(cls, umo: Any, entry: Any) -> dict[str, Any] | None:
        """单条归一化，坏数据跳过而不是让整个文件作废。"""
        key = str(umo or "").strip()
        if not key or not isinstance(entry, Mapping):
            return None
        _, _, sid = split_umo(key)
        platform_id = str(entry.get("platform_id") or "").strip() or split_umo(key)[0]
        group_id = str(entry.get("group_id") or "").strip() or sid
        return {
            "_key": key,
            "platform_id": platform_id,
            "group_id": group_id,
            "group_name": str(entry.get("group_name") or "").strip(),
            "member_count": _as_int(entry.get("member_count")),
            "source": str(entry.get("source") or SOURCE_EVENT).strip() or SOURCE_EVENT,
            "updated_at": cls._as_float(entry.get("updated_at")) or time.time(),
        }

    def save(self) -> None:
        """原子写：先写同目录临时文件再 replace，避免写一半断电留个坏 json。"""
        self._ensure_loaded()
        payload = {
            "version": STORE_VERSION,
            "updated_at": time.time(),
            "last_refresh": self._last_refresh,
            "groups": {
                key: {k: v for k, v in entry.items() if k != "_key"}
                for key, entry in self._groups.items()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".groups-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------------- 读 ----------------

    def get(self, umo: str) -> dict[str, Any] | None:
        """取某个会话的缓存条目（副本）。"""
        self._ensure_loaded()
        entry = self._groups.get(str(umo or "").strip())
        return self._copy(entry) if entry else None

    def name_of(self, umo: str) -> str:
        return name_of(self.get(umo))

    def label(self, umo: str) -> str:
        """会话的展示文字，规则见 session_label。

        先按 umo 精确查；查不到再按「群号 + 平台」兜一次 —— 面板里的目标群往往来自
        配置（裸群号按默认平台补全，如 aiocqhttp:GroupMessage:1001），而事件里的 umo
        用的是平台实例 id（napcat:GroupMessage:1001），两者指同一个群。多平台同号时
        不猜（宁可显示群号，也不显示别的群的群名）。
        """
        entry = self.get(umo)
        if name_of(entry):
            return session_label(umo, entry)
        platform_id, _, group_id = split_umo(umo)
        if group_id:
            found = self.find_name(platform_id, group_id, allow_any_platform=True)
            if found:
                merged = dict(entry or {})
                merged["group_name"] = found
                return session_label(umo, merged)
        return session_label(umo, entry)

    def entries(self) -> dict[str, dict[str, Any]]:
        self._ensure_loaded()
        return {key: self._copy(entry) for key, entry in self._groups.items()}

    def find_name(
        self, platform_id: str, group_id: str, *, allow_any_platform: bool = False
    ) -> str:
        """按「平台 + 群号」找名字。

        同平台命中直接用。``allow_any_platform=True`` 时，若该群号只对应**一个**已知
        名字，也认（umo 里可能写的是适配器类型 aiocqhttp、事件里是实例 id napcat，
        两者指同一个群）；有歧义（多平台同号）就不猜，宁可显示群号。
        """
        self._ensure_loaded()
        plat = str(platform_id or "").strip()
        gid = str(group_id or "").strip()
        if not gid:
            return ""
        candidates: set[str] = set()
        for key, entry in self._groups.items():
            if entry.get("group_id") != gid:
                continue
            display = name_of(entry)
            if not display:
                continue
            if not plat or entry.get("platform_id") == plat or split_umo(key)[0] == plat:
                return display
            candidates.add(display)
        if allow_any_platform and len(candidates) == 1:
            return next(iter(candidates))
        return ""

    @staticmethod
    def _copy(entry: Mapping[str, Any] | None) -> dict[str, Any]:
        if not entry:
            return {}
        return {k: v for k, v in entry.items() if k != "_key"}

    # ---------------- 写 ----------------

    def remember(
        self,
        umo: str,
        *,
        group_id: str = "",
        group_name: str = "",
        platform_id: str = "",
        member_count: Any = None,
        source: str = SOURCE_EVENT,
    ) -> bool:
        """记一条会话。内容真变了才落盘，返回是否有变化。

        空群名不会覆盖已有的好名字（只有知道得更多才写），群号 / 平台 id 随时补齐，
        member_count 只在拿到正数时更新。写盘只在真的有变化时发生，所以消息热路径上
        只有首次见到某个群时会碰一次磁盘。
        """
        key = str(umo or "").strip()
        if not key:
            return False
        self._ensure_loaded()
        _, _, sid = split_umo(key)
        gid = str(group_id or "").strip() or sid
        plat = str(platform_id or "").strip() or split_umo(key)[0]
        clean_name = str(group_name or "").strip()
        count = _as_int(member_count)

        entry = self._groups.get(key)
        if entry is None:
            self._groups[key] = {
                "_key": key,
                "platform_id": plat,
                "group_id": gid,
                "group_name": clean_name,
                "member_count": count,
                "source": str(source or SOURCE_EVENT),
                "updated_at": time.time(),
            }
            self.prune()
            self.save()
            return True

        changed = False
        if gid and entry.get("group_id") != gid:
            entry["group_id"] = gid
            changed = True
        if plat and entry.get("platform_id") != plat:
            entry["platform_id"] = plat
            changed = True
        if count and entry.get("member_count") != count:
            entry["member_count"] = count
            changed = True
        if clean_name and entry.get("group_name") != clean_name:
            entry["group_name"] = clean_name
            entry["source"] = str(source or SOURCE_EVENT)
            changed = True
        if not changed:
            return False
        entry["updated_at"] = time.time()
        self.save()
        return True

    def merge_api_groups(
        self, platform_id: str, groups: Iterable[Mapping[str, Any]]
    ) -> int:
        """把平台查回来的群列表写进缓存，返回真正有变化的条数。

        groups 用 parse_group_list 归一过，也可以直接喂 OneBot 的原始返回
        （裸数组 / {"data": [...]} 都行）。
        """
        raw: Any = groups
        if raw is not None and not isinstance(raw, (list, tuple, Mapping)):
            try:
                raw = list(raw)
            except TypeError:
                raw = None
        changed = 0
        for item in parse_group_list(raw):
            umo = umo_for(platform_id, item["group_id"])
            if not umo:
                continue
            if self.remember(
                umo,
                group_id=item["group_id"],
                group_name=item["group_name"],
                platform_id=platform_id,
                member_count=item["member_count"],
                source=SOURCE_API,
            ):
                changed += 1
        return changed

    # ---------------- 刷新节流 / 淘汰 ----------------

    def needs_refresh(self, interval: int = REFRESH_INTERVAL) -> bool:
        """距上次真去问平台有没有超过 interval 秒。"""
        self._ensure_loaded()
        return (time.time() - self._last_refresh) >= max(0, int(interval))

    def last_refresh_at(self) -> float:
        self._ensure_loaded()
        return self._last_refresh

    def mark_refreshed(self) -> None:
        self._ensure_loaded()
        self._last_refresh = time.time()
        self.save()

    def prune(self) -> int:
        """超过条数上限时丢最旧的，返回丢掉的条数。"""
        self._ensure_loaded()
        overflow = len(self._groups) - self.max_entries
        if overflow <= 0:
            return 0
        ordered = sorted(
            self._groups.items(), key=lambda kv: self._as_float(kv[1].get("updated_at"))
        )
        for key, _ in ordered[:overflow]:
            self._groups.pop(key, None)
        return overflow

    def flush(self) -> None:
        """退出前兜底存盘（缓存为空时不写，省得凭空造个空文件）。"""
        self._ensure_loaded()
        if self._groups:
            self.save()
