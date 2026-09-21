"""polymer 动态条目的纯解析：分类 + 提取为统一结构。

数据源是 ``x/polymer/web-dynamic/v1/feed/space`` 的 ``data.items``，
每个 item 形如::

    {
        "id_str": "1148459269840961545",
        "type": "DYNAMIC_TYPE_AV",
        "modules": {
            "module_author": {"name": "...", "pub_ts": 1766234911},
            "module_dynamic": {"desc": {...}, "major": {...}},
        },
        "orig": {...},          # 转发动态带原动态（结构同为 item）
    }

这里全部是无副作用的纯函数，不 import astrbot，方便本地单测。
"""
from typing import Any, Dict, List, Optional

# 单条通知最多带几张图（防刷屏，插件外不再加配置项）
MAX_IMAGES = 3

# 单轮最多补推几条动态（一次轮询跨了很久时不至于刷屏；B站单页最多回 12 条）
MAX_PUSH_PER_ROUND = 10

# type 字符串 → 内部简类 kind
_TYPE_KIND = {
    "DYNAMIC_TYPE_AV": "video",              # 投稿视频
    "DYNAMIC_TYPE_PGC_UNION": "video",       # 番剧/影视
    "DYNAMIC_TYPE_UGC_SEASON": "video",      # 合集
    "DYNAMIC_TYPE_DRAW": "draw",             # 图文（相簿/opus）
    "DYNAMIC_TYPE_ARTICLE": "article",       # 专栏文章
    "DYNAMIC_TYPE_WORD": "word",             # 纯文字动态
    "DYNAMIC_TYPE_FORWARD": "forward",       # 转发动态
    "DYNAMIC_TYPE_LIVE_RCMD": "live",        # 直播开播卡片
    "DYNAMIC_TYPE_LIVE": "live",
    "DYNAMIC_TYPE_COMMON_SQUARE": "video",   # 小视频（方屏/竖屏）
    "DYNAMIC_TYPE_COMMON_VERTICAL": "video",
    "DYNAMIC_TYPE_MUSIC": "music",           # 音频
}

# kind → 中文动作描述
KIND_ACTION = {
    "video": "投稿了视频",
    "draw": "发布了图文动态",
    "article": "投稿了文章",
    "word": "发布了动态",
    "forward": "转发了动态",
    "live": "开启了直播",
    "music": "投稿了音频",
    "other": "发布了新动态",
}

_T_BUF = "https://t.bilibili.com/"


def _major(item: Dict) -> Dict:
    return (((item or {}).get("modules") or {}).get("module_dynamic") or {}).get("major") or {}


def _desc_text(item: Dict) -> str:
    desc = (((item or {}).get("modules") or {}).get("module_dynamic") or {}).get("desc")
    if isinstance(desc, dict):
        return str(desc.get("text") or "")
    return ""


def _fix_url(u: Optional[str]) -> str:
    """把跳转链接补全成完整 https，空则回退动态页链接。"""
    u = (u or "").strip()
    if u.startswith("//"):
        return "https:" + u
    return u


def major_kind(major_type: str) -> str:
    """major.type（MAJOR_TYPE_*）到 kind 的兜底映射。"""
    mt = str(major_type or "").upper()
    if mt in ("MAJOR_TYPE_ARCHIVE", "MAJOR_TYPE_PGC", "MAJOR_TYPE_UGC_SEASON"):
        return "video"
    if mt == "MAJOR_TYPE_DRAW":
        return "draw"
    if mt == "MAJOR_TYPE_OPUS":
        return "article"  # opus 既可能是图文也可能是专栏，按详情再细分
    if mt == "MAJOR_TYPE_ARTICLE":
        return "article"
    if mt == "MAJOR_TYPE_WORD":
        return "word"
    if mt in ("MAJOR_TYPE_LIVE", "MAJOR_TYPE_LIVE_RCMD"):
        return "live"
    if mt == "MAJOR_TYPE_COMMON":
        return "video"
    return "other"


def classify(item: Dict) -> str:
    """把一条 dynamic item 归为内部 kind：优先顶层 type，再按 major.type 兜底。"""
    t = str((item or {}).get("type", ""))
    if t in _TYPE_KIND:
        return _TYPE_KIND[t]
    return major_kind(_major(item).get("type", ""))


def should_notify(kind: str, toggles: Dict[str, bool]) -> bool:
    """按通知开关判断某类动态是否推送。

    toggles 的键与配置项同名（video/draw/article/word/forward/live/music/other）。
    未知 kind 一律按 other 开关处理。
    """
    return bool(toggles.get(kind, toggles.get("other", True)))


# ---------------- 动态 id 游标（纯函数） ----------------
# B站动态 id 是递增的纯数字串（id_str），但**位数不固定**：
# 老动态 18 位如 598503014097476780，新动态 19 位如 1148459269840961545。
# 按字符串比大小会把新动态判成"更旧"（"1…" < "5…"），新动态永远推不出来 —— 必须按数值比。


def _id_sort_key(id_str: Any):
    """把 id_str 换成可比较的键：纯数字按数值，其余按字符串兜底（同型元组，避免比较报错）。"""
    s = str(id_str or "").strip()
    if s.isdigit():
        return (1, int(s), "")
    return (0, 0, s)


def is_newer_id(new_id: Any, old_id: Any) -> bool:
    """new_id 是否比 old_id 更新（按数值，不按字符串）。

    old_id 为空/None 视为"还没有基线"，此时任何有效 new_id 都算更新；
    new_id 本身无效则一律返回 False（游标不许被空值写坏）。
    """
    if not str(new_id or "").strip():
        return False
    if not str(old_id or "").strip():
        return True
    return _id_sort_key(new_id) > _id_sort_key(old_id)


def latest_id(items: List[Dict]) -> Optional[str]:
    """取一批动态里最新的 id_str（接口顺序不可信，取数值最大者）。无有效 id 返回 None。"""
    best: Optional[str] = None
    for it in items or []:
        if not isinstance(it, dict):
            continue
        cur = str(it.get("id_str") or "").strip()
        if not cur:
            continue
        if best is None or _id_sort_key(cur) > _id_sort_key(best):
            best = cur
    return best


def select_new_dynamics(items: List[Dict], last_id: Optional[str],
                        limit: int = MAX_PUSH_PER_ROUND) -> List[Dict]:
    """挑出比 last_id 更新的动态，按时间正序（旧的先推）返回，单轮最多 limit 条。

    ``items`` 为接口返回（最新在前，但顺序不作为判断依据）。
    ``last_id`` 为空时按"全部都是新的"处理 —— 调用方应先自行建立基线，
    否则首次运行会把存量动态当新动态刷屏（main.py 里就是这么拦的）。
    """
    valid = [
        it for it in (items or [])
        if isinstance(it, dict) and str(it.get("id_str") or "").strip()
    ]
    new_items = [it for it in valid if is_newer_id(it.get("id_str"), last_id)]
    new_items.sort(key=lambda it: _id_sort_key(it.get("id_str")))
    if limit and len(new_items) > limit:
        return new_items[-limit:]  # 只推最近的 limit 条，避免一次补推刷屏
    return new_items


def extract_dynamic(item: Dict, depth: int = 0) -> Dict[str, Any]:
    """把一条 polymer dynamic item 解析成统一结构（纯函数）。

    返回::

        {
            "id_str":   动态id字符串,
            "kind":     video/draw/article/word/forward/live/music/other,
            "uname":    UP主名,
            "action":   中文动作（如 "投稿了视频"）,
            "title":    标题（视频/文章，可为 ""）,
            "text":     正文/描述摘要（可为 ""）,
            "images":   图片URL列表（截到 MAX_IMAGES）,
            "url":      落地页链接,
            "pub_ts":   发布时间戳（int，无则 0）,
        }

    转发动态会把原动态标题/正文拼进 text（只递归一层）。
    """
    modules = (item or {}).get("modules") or {}
    author = modules.get("module_author") or {}
    dyn = modules.get("module_dynamic") or {}
    major = dyn.get("major") or {}
    major_type = str(major.get("type", ""))

    id_str = str((item or {}).get("id_str") or "")
    kind = classify(item)
    uname = str(author.get("name") or "")
    pub_ts = int(author.get("pub_ts") or 0)

    title = ""
    text = _desc_text(item)
    images: List[str] = []
    url = _T_BUF + id_str if id_str else ""

    archive = major.get("archive") or {}
    opus = major.get("opus") or {}
    draw = major.get("draw") or {}
    article = major.get("article") or {}
    live = major.get("live") or _live_rcmd(major)
    common = major.get("common") or {}
    music = major.get("music") or {}

    if archive:
        kind = "video"
        title = str(archive.get("title") or "")
        text = text or str(archive.get("desc") or "")
        cover = _fix_url(archive.get("cover"))
        if cover:
            images.append(cover)
        url = _fix_url(archive.get("jump_url")) or (
            "https://www.bilibili.com/video/" + str(archive.get("bvid")) if archive.get("bvid") else url
        )
    elif opus:
        # 有 title 视为专栏，没 title 视为图文
        otitle = str(opus.get("title") or "")
        kind = "article" if otitle else "draw"
        title = otitle
        text = text or _opus_text(opus)
        images.extend(_opus_images(opus))
        url = _fix_url(opus.get("jump_url")) or url
    elif draw and draw.get("items"):
        kind = "draw"
        images.extend(str(d.get("src") or "") for d in (draw.get("items") or []) if d.get("src"))
    elif article:
        kind = "article"
        title = str(article.get("title") or "")
        text = text or str(article.get("desc") or "")
        for c in (article.get("covers") or [])[:MAX_IMAGES]:
            if c:
                images.append(_fix_url(c))
    elif common:
        kind = "video"
        title = str(common.get("title") or "")
        text = text or str(common.get("desc") or "")
        cover = _fix_url(common.get("cover"))
        if cover:
            images.append(cover)
        url = _fix_url(common.get("jump_url")) or url
    elif music:
        kind = "music"
        title = str(music.get("title") or "")
        cover = _fix_url(music.get("cover"))
        if cover:
            images.append(cover)
    elif live:
        kind = "live"
        title = str(live.get("title") or "")
        cover = _fix_url(live.get("cover") or live.get("cover_small"))
        if cover:
            images.append(cover)
        url = _fix_url(live.get("jump_url") or live.get("link")) or url

    # 转发动态：把原动态概要拼进正文（只递归一层，防止嵌套爆炸）
    if kind == "forward" and depth == 0:
        orig = (item or {}).get("orig")
        if isinstance(orig, dict) and orig.get("modules"):
            sub = extract_dynamic(orig, depth=1)
            sub_head = f"\n⤷ 原@{sub['uname']}" if sub.get("uname") else "\n⤷ 原动态"
            sub_body = sub.get("title") or sub.get("text") or ""
            text = (text + sub_head + ("：" + sub_body if sub_body else "")).strip()
            for im in sub.get("images", []):
                if len(images) >= MAX_IMAGES:
                    break
                images.append(im)
        else:
            text = (text + "\n⤷ 原动态已删除或不可见").strip()

    return {
        "id_str": id_str,
        "kind": kind,
        "uname": uname,
        "action": KIND_ACTION.get(kind, KIND_ACTION["other"]),
        "title": title,
        "text": text,
        "images": images[:MAX_IMAGES],
        "url": url,
        "pub_ts": pub_ts,
    }


def _opus_text(opus: Dict) -> str:
    summary = opus.get("summary") or {}
    if isinstance(summary, dict):
        return str(summary.get("text") or "")
    return ""


def _opus_images(opus: Dict) -> List[str]:
    out: List[str] = []
    for p in (opus.get("pics") or [])[:MAX_IMAGES]:
        u = _fix_url(p.get("url"))
        if u:
            out.append(u)
    return out


def _live_rcmd(major: Dict) -> Dict[str, Any]:
    """live_rcmd 的内容塞在 content 字符串里，尝试解一层 JSON。"""
    raw = major.get("live_rcmd")
    if not isinstance(raw, dict):
        return {}
    content = raw.get("content")
    if isinstance(content, str) and content.strip().startswith("{"):
        try:
            import json

            parsed = json.loads(content)
            # 实际结构是 {"live_play_view": {...}}，解一层拿到真正字段
            if isinstance(parsed, dict) and isinstance(parsed.get("live_play_view"), dict):
                return parsed["live_play_view"]
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return raw
