"""视频置顶评论的纯解析：bv↔av 换算 + 从 reply 接口取"UP 自己的置顶评论"。

数据源是**旧版** ``x/v2/reply`` 接口的 ``data``（只需 buvid3，不用 WBI 签名）：

    {
        "upper": {"mid": 2081532576, "top": {...reply...}},
        "top_replies": [ {...reply...}, ... ],
        "replies": [...],
    }

其中一条 reply 形如::

    {
        "rpid": 315305036289,
        "mid": 2081532576,                 # 发评论的人；== UP 的 uid 即"UP 自己发的"
        "ctime": 1790559841,
        "member": {"uname": "魔兽阿落"},
        "content": {
            "message": "9.28推荐",
            "pictures": [{"img_src": "http://i0.hdslb.com/bfs/...jpg"}],
        },
    }

这里全部是无副作用的纯函数，不 import astrbot，方便本地单测。
"""
from typing import Any, Dict, List, Optional

# 单条评论最多带几张图（与动态一致，防刷屏）
MAX_IMAGES = 3

# ---------------- bv → av 换算（B站新算法，纯函数，无网络） ----------------
# 参考: https://socialsisteryi.github.io/bilibili-API-collect/docs/misc/bvid_desc.html
_XOR_CODE = 23442827791579
_MASK_CODE = 2251799813685247
_BASE = 58
_ALPHABET = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"
_CHAR_INDEX = {c: i for i, c in enumerate(_ALPHABET)}


def bv2av(bvid: Any) -> int:
    """把 BV 号换算成 av 号（评论接口的 oid）。无法识别时返回 0。

    只认标准 ``BV`` 开头、长度 12 的号；字符不在字母表里的一律当非法（返回 0），
    不抛异常（上层可安全跳过这条）。
    """
    s = str(bvid or "").strip()
    if len(s) != 12 or not s.startswith("BV"):
        return 0
    chars = list(s)
    # 固定位置置换（B站算法）
    chars[3], chars[9] = chars[9], chars[3]
    chars[4], chars[7] = chars[7], chars[4]
    tmp = 0
    for c in chars[3:]:
        idx = _CHAR_INDEX.get(c)
        if idx is None:
            return 0
        tmp = tmp * _BASE + idx
    return (tmp & _MASK_CODE) ^ _XOR_CODE


def _fix_url(u: Optional[str]) -> str:
    """把跳转/图片链接补全成完整 https；http 也升到 https（评论图常给 http）。"""
    u = (u or "").strip()
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http://"):
        return "https://" + u[len("http://"):]
    return u


def _reply_images(reply: Dict) -> List[str]:
    content = (reply or {}).get("content") or {}
    out: List[str] = []
    for p in (content.get("pictures") or []):
        if not isinstance(p, dict):
            continue
        u = _fix_url(p.get("img_src"))
        if u:
            out.append(u)
        if len(out) >= MAX_IMAGES:
            break
    return out


def _pinned_url(bvid: Any, rpid: Any) -> str:
    """置顶评论的落地链接：视频页锚到该评论。bvid 缺失时返回空串。"""
    bv = str(bvid or "").strip()
    if not bv:
        return ""
    rp = str(rpid or "").strip()
    base = f"https://www.bilibili.com/video/{bv}/"
    return f"{base}#reply{rp}" if rp else base


def _shape_reply(reply: Dict, bvid: Any) -> Optional[Dict[str, Any]]:
    """把一条 reply 归一成统一结构；无 rpid 视为无效返回 None。"""
    if not isinstance(reply, dict):
        return None
    rpid = str(reply.get("rpid") or "").strip()
    if not rpid:
        return None
    content = reply.get("content") or {}
    member = reply.get("member") or {}
    return {
        "rpid": rpid,
        "uid": str(reply.get("mid") or "").strip(),
        "uname": str(member.get("uname") or "").strip(),
        "text": str(content.get("message") or "").strip(),
        "images": _reply_images(reply),
        "ctime": int(reply.get("ctime") or 0),
        "url": _pinned_url(bvid, rpid),
    }


def extract_pinned(reply_data: Dict, uid: Any, bvid: Any = "") -> Optional[Dict[str, Any]]:
    """从 reply 接口的 ``data`` 里取"UP 自己（mid==uid）的置顶评论"。

    优先 ``data.upper.top``（这就是 UP 的置顶位）；兜底扫 ``data.top_replies``
    里第一条 ``mid==uid`` 的。都没有则返回 None。

    返回统一结构::

        {rpid, uid, uname, text, images, ctime, url}
    """
    data = reply_data or {}
    want_uid = str(uid or "").strip()
    if not want_uid:
        return None

    upper_top = (data.get("upper") or {}).get("top")
    if isinstance(upper_top, dict) and str(upper_top.get("mid") or "").strip() == want_uid:
        shaped = _shape_reply(upper_top, bvid)
        if shaped:
            return shaped

    for reply in (data.get("top_replies") or []):
        if isinstance(reply, dict) and str(reply.get("mid") or "").strip() == want_uid:
            shaped = _shape_reply(reply, bvid)
            if shaped:
                return shaped
    return None
