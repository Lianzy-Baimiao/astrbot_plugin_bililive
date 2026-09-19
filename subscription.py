"""订阅数据的纯函数：解析 / 序列化 / 群号归一化。

配置里 subscriptions 是一个字符串列表，每行描述一个 UP 主：

    UID=目标[,目标...]
    目标写法: [平台id:]群号[@all] 或完整 umo[@all]

例：
    111111111=777777777
    222222222=napcat:777777777@all
    333333333=aiocqhttp:GroupMessage:777777777,napcat:777777777@all

兼容旧写法：行尾 ``| at_all`` 表示整行所有目标 @all。
以 # 开头的行视为注释；格式错误的行被忽略（上层负责提示）。

内部结构（解析后，at_all 落到每个目标上）：

    {
        "111111111": {
            "groups": [
                {"umo": "aiocqhttp:GroupMessage:777777777", "at_all": False},
                {"umo": "napcat:GroupMessage:777777777", "at_all": True},
            ],
        },
        ...
    }

这里全部是无副作用的纯函数，不 import astrbot，方便本地单测。
"""
from typing import Dict, List, Tuple

GROUP_MSG_TYPE = "GroupMessage"
AT_ALL_SUFFIX = "@all"


def normalize_target(entry: str, default_platform: str = "aiocqhttp") -> str:
    """把一个目标群写法归一化成完整 unified_msg_origin。

    支持三种写法（短的优先，越少啰嗦）：

    - ``群号``（裸号，无冒号）→ ``{default_platform}:GroupMessage:{群号}``
    - ``平台id:群号``（两段简写）→ ``平台id:GroupMessage:群号``
    - ``平台id:GroupMessage:群号``（完整 umo，三段及以上）→ 原样

    注意：umo 第一段是平台的**实例 id**（如 napcat / default_666666666），
    不是适配器类型（aiocqhttp / qq_official）——send_message 按 id 匹配。
    每条自带平台，多平台可混用；裸群号只补到单一平台，避免重复推送。
    """
    s = (entry or "").strip()
    if not s:
        return ""
    parts = s.split(":")
    if len(parts) == 1:  # 裸群号
        plat = (default_platform or "aiocqhttp").strip() or "aiocqhttp"
        return f"{plat}:{GROUP_MSG_TYPE}:{s}"
    if len(parts) == 2:  # 平台id:群号 简写
        plat, gid = parts[0].strip(), parts[1].strip()
        if plat and gid:
            return f"{plat}:{GROUP_MSG_TYPE}:{gid}"
        return ""
    return s  # 完整 umo，原样保留


def split_at_all(token: str) -> Tuple[str, bool]:
    """剥离目标末尾的 @all 标记：``napcat:123@all`` → ``napcat:123, True``。"""
    s = (token or "").strip()
    if s.lower().endswith(AT_ALL_SUFFIX):
        return s[: -len(AT_ALL_SUFFIX)].strip(), True
    return s, False


def shorten_target(umo: str) -> str:
    """把完整 umo 缩成最短可读写法，用于写回配置。

    ``平台id:GroupMessage:群号`` → ``平台id:群号``（GroupMessage 冗余，省掉）。
    其它消息类型（FriendMessage 等）保持完整，避免歧义。
    """
    s = (umo or "").strip()
    if not s:
        return ""
    parts = s.split(":")
    if len(parts) == 3 and parts[1] == GROUP_MSG_TYPE:
        return f"{parts[0]}:{parts[2]}"
    return s


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

    info = {"groups": [{"umo": ..., "at_all": bool}, ...]}
    行尾 ``| at_all``（旧写法）会给整行所有目标打上 at_all。
    """
    raw = (line or "").strip()
    if not raw or raw.startswith("#"):
        return None

    # 拆行级 at_all 标记（旧写法）: "... | at_all"
    line_at_all = False
    if "|" in raw:
        head, _, tail = raw.partition("|")
        if tail.strip().lower() in ("at_all", "atall", "@all", "at-all"):
            line_at_all = True
        raw = head.strip()

    # 拆 UID 与群列表： "UID=群,群"
    if "=" not in raw:
        return None
    uid_part, _, groups_part = raw.partition("=")
    uid = uid_part.strip()
    if not uid.isdigit():
        return None

    groups: List[Dict] = []
    for token in groups_part.split(","):
        target, at_all = split_at_all(token)
        umo = normalize_target(target, default_platform)
        if not umo:
            continue
        at_all = at_all or line_at_all
        existing = next((g for g in groups if g["umo"] == umo), None)
        if existing:
            existing["at_all"] = existing["at_all"] or at_all
        else:
            groups.append({"umo": umo, "at_all": at_all})

    return uid, {"groups": groups}


def parse_subscriptions(lines, default_platform: str = "aiocqhttp") -> Dict[str, Dict]:
    """把配置里的字符串列表解析成内部结构。

    同一 UID 多次出现时，合并目标列表；同一目标 at_all 取或。
    """
    result: Dict[str, Dict] = {}
    for line in list(lines or []):
        parsed = parse_line(line, default_platform)
        if not parsed:
            continue
        uid, info = parsed
        entry = result.setdefault(uid, {"groups": []})
        for g in info["groups"]:
            existing = next((x for x in entry["groups"] if x["umo"] == g["umo"]), None)
            if existing:
                existing["at_all"] = existing["at_all"] or g["at_all"]
            else:
                entry["groups"].append(dict(g))
    return result


def serialize_subscriptions(subs: Dict[str, Dict]) -> List[str]:
    """把内部结构反向序列化回配置用的字符串列表。

    只输出还有目标群的 UP；空群的 UP 被丢弃。
    目标写成 ``平台id:群号`` 简写（保证往返稳定，不因 default_platform 改变而漂移），
    at_all 用目标级 ``@all`` 后缀表达。
    """
    lines: List[str] = []
    for uid, info in subs.items():
        groups = info.get("groups", []) if isinstance(info, dict) else []
        if not groups:
            continue
        targets: List[str] = []
        for g in groups:
            umo = g.get("umo", "") if isinstance(g, dict) else str(g)
            if not umo:
                continue
            target = shorten_target(umo)
            if isinstance(g, dict) and g.get("at_all"):
                target += AT_ALL_SUFFIX
            targets.append(target)
        if targets:
            lines.append(f"{uid}={','.join(targets)}")
    return lines


def add_subscription(subs: Dict[str, Dict], uid: str, group_umo: str, at_all: bool = False) -> bool:
    """给某 UP 增加一个目标群（当前群）。返回是否发生了新增/变化。"""
    uid = str(uid).strip()
    group_umo = (group_umo or "").strip()
    if not uid.isdigit() or not group_umo:
        return False
    entry = subs.setdefault(uid, {"groups": []})
    for g in entry["groups"]:
        if g["umo"] == group_umo:
            if at_all and not g["at_all"]:
                g["at_all"] = True
                return True
            return False
    entry["groups"].append({"umo": group_umo, "at_all": bool(at_all)})
    return True


def remove_subscription(subs: Dict[str, Dict], uid: str, group_umo: str) -> bool:
    """把某 UP 从指定群移除。若该 UP 再无目标群则整条删除。返回是否变化。"""
    uid = str(uid).strip()
    group_umo = (group_umo or "").strip()
    entry = subs.get(uid)
    if not entry:
        return False
    groups = entry.get("groups", [])
    for i, g in enumerate(groups):
        if g.get("umo") == group_umo:
            del groups[i]
            if not groups:
                del subs[uid]
            return True
    return False


def subscriptions_for_group(subs: Dict[str, Dict], group_umo: str) -> List[str]:
    """列出在指定群里订阅的所有 UID（保持插入顺序），用于 /订阅列表 的序号。"""
    group_umo = (group_umo or "").strip()
    return [
        uid
        for uid, info in subs.items()
        if any(g.get("umo") == group_umo for g in info.get("groups", []))
    ]


def group_at_all(subs: Dict[str, Dict], uid: str, group_umo: str) -> bool:
    """查某个 (UP, 群) 组合是否 @all。"""
    entry = subs.get(str(uid).strip())
    if not entry:
        return False
    group_umo = (group_umo or "").strip()
    return any(
        g.get("umo") == group_umo and g.get("at_all") for g in entry.get("groups", [])
    )


def all_uids(subs: Dict[str, Dict]) -> List[str]:
    """所有被订阅的 UID（去重），供批量查询直播状态。"""
    return list(subs.keys())
