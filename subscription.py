"""订阅数据的纯函数：解析 / 序列化 / 群号归一化。

配置里 subscriptions 是一个字符串列表，每行描述一个 UP 主：

    UID=群号[,群号...][ | at_all]

例：
    111111111=777777777,888888888
    222222222=777777777 | at_all
    333333333=aiocqhttp:GroupMessage:777777777

内部结构（解析后）：

    {
        "111111111": {
            "groups": ["aiocqhttp:GroupMessage:777777777", "aiocqhttp:GroupMessage:888888888"],
            "at_all": False,
        },
        ...
    }

这里全部是无副作用的纯函数，不 import astrbot，方便本地单测。
"""
from typing import Dict, List

GROUP_MSG_TYPE = "GroupMessage"


def normalize_target(entry: str, default_platform: str = "aiocqhttp") -> str:
    """把一个目标群写法归一化成 unified_msg_origin。

    - 已是完整 umo（含 ":"）：原样返回（去空白）。
    - 裸群号：补全成 ``{default_platform}:GroupMessage:{群号}``。

    只补全到单一平台，避免同一个群被多个适配器重复推送
    （这正是旧插件开播发两条的根因）。
    """
    s = (entry or "").strip()
    if not s:
        return ""
    if ":" in s:  # 已经是完整 umo
        return s
    plat = (default_platform or "aiocqhttp").strip() or "aiocqhttp"
    return f"{plat}:{GROUP_MSG_TYPE}:{s}"


def group_display(umo: str) -> str:
    """从 unified_msg_origin 里取出人类可读的群号/会话号用于展示。"""
    s = (umo or "").strip()
    if not s:
        return ""
    # platform:MessageType:session_id -> session_id
    parts = s.split(":")
    return parts[-1] if parts else s


def parse_line(line: str, default_platform: str = "aiocqhttp"):
    """解析一行订阅串，返回 (uid, info) 或 None（无效行）。

    info = {"groups": [归一化后的 umo, ...], "at_all": bool}
    """
    raw = (line or "").strip()
    if not raw or raw.startswith("#"):
        return None

    # 拆 at_all 标记： "... | at_all"
    at_all = False
    if "|" in raw:
        head, _, tail = raw.partition("|")
        if tail.strip().lower() in ("at_all", "atall", "@all", "at-all"):
            at_all = True
        raw = head.strip()

    # 拆 UID 与群列表： "UID=群,群"
    if "=" not in raw:
        return None
    uid_part, _, groups_part = raw.partition("=")
    uid = uid_part.strip()
    if not uid.isdigit():
        return None

    groups: List[str] = []
    for g in groups_part.split(","):
        umo = normalize_target(g, default_platform)
        if umo and umo not in groups:
            groups.append(umo)

    return uid, {"groups": groups, "at_all": at_all}


def parse_subscriptions(lines, default_platform: str = "aiocqhttp") -> Dict[str, Dict]:
    """把配置里的字符串列表解析成内部结构。

    同一 UID 多次出现时，合并群列表、at_all 取或。
    """
    result: Dict[str, Dict] = {}
    for line in list(lines or []):
        parsed = parse_line(line, default_platform)
        if not parsed:
            continue
        uid, info = parsed
        if uid not in result:
            result[uid] = {"groups": list(info["groups"]), "at_all": info["at_all"]}
        else:
            for g in info["groups"]:
                if g not in result[uid]["groups"]:
                    result[uid]["groups"].append(g)
            result[uid]["at_all"] = result[uid]["at_all"] or info["at_all"]
    return result


def serialize_subscriptions(subs: Dict[str, Dict]) -> List[str]:
    """把内部结构反向序列化回配置用的字符串列表。

    只输出还有目标群的 UP；空群的 UP 被丢弃。
    群写完整 umo（保证往返稳定，不因 default_platform 改变而漂移）。
    """
    lines: List[str] = []
    for uid, info in subs.items():
        groups = info.get("groups", []) if isinstance(info, dict) else []
        if not groups:
            continue
        line = f"{uid}={','.join(groups)}"
        if isinstance(info, dict) and info.get("at_all"):
            line += " | at_all"
        lines.append(line)
    return lines


def add_subscription(subs: Dict[str, Dict], uid: str, group_umo: str, at_all: bool = False) -> bool:
    """给某 UP 增加一个目标群（当前群）。返回是否发生了新增/变化。"""
    uid = str(uid).strip()
    group_umo = (group_umo or "").strip()
    if not uid.isdigit() or not group_umo:
        return False
    entry = subs.setdefault(uid, {"groups": [], "at_all": False})
    changed = False
    if group_umo not in entry["groups"]:
        entry["groups"].append(group_umo)
        changed = True
    if at_all and not entry["at_all"]:
        entry["at_all"] = True
        changed = True
    return changed


def remove_subscription(subs: Dict[str, Dict], uid: str, group_umo: str) -> bool:
    """把某 UP 从指定群移除。若该 UP 再无目标群则整条删除。返回是否变化。"""
    uid = str(uid).strip()
    group_umo = (group_umo or "").strip()
    entry = subs.get(uid)
    if not entry or group_umo not in entry.get("groups", []):
        return False
    entry["groups"].remove(group_umo)
    if not entry["groups"]:
        del subs[uid]
    return True


def subscriptions_for_group(subs: Dict[str, Dict], group_umo: str) -> List[str]:
    """列出在指定群里订阅的所有 UID（保持插入顺序），用于 /订阅列表 的序号。"""
    group_umo = (group_umo or "").strip()
    return [uid for uid, info in subs.items() if group_umo in info.get("groups", [])]


def all_uids(subs: Dict[str, Dict]) -> List[str]:
    """所有被订阅的 UID（去重），供批量查询直播状态。"""
    return list(subs.keys())
