"""B站 WBI 签名（纯函数，不 import astrbot，可本地单测）。

polymer 动态等 ``api.bilibili.com/x/...`` 端点要求请求带 ``wts``/``w_rid`` 签名：

1. 从 nav 接口取 ``wbi_img.img_url`` / ``wbi_img.sub_url``，文件名即 img_key/sub_key
2. ``img_key + sub_key`` 按固定置换表重排，取前 32 字符得 mixin_key
3. 参数加上 ``wts``（当前秒级时间戳）后按 key 排序做 URL 编码，
   字符串值剔除 ``!'()*`` 五个字符
4. ``md5(query_string + mixin_key)`` 即 ``w_rid``

参考: https://socialsisteryi.github.io/bilibili-API-collect/docs/misc/sign/wbi.html
"""
from hashlib import md5
from typing import Dict, Optional
from urllib.parse import urlencode

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# 值里要剔除的字符（B站签名规则）
_FILTER_CHARS = "!'()*"


def get_mixin_key(key: str) -> str:
    """img_key + sub_key 按置换表重排，截前 32 位得到 mixin_key。"""
    return "".join(key[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def encode_wbi(params: Dict, img_key: str, sub_key: str, wts: Optional[int] = None) -> Dict:
    """对参数做 WBI 签名，返回补了 wts/w_rid 的新参数 dict（不改动入参）。

    参数按 key 字典序排序后编码；字符串值剔除 ``!'()*``；
    ``params`` 本身不含 wts 时由调用方/本函数注入当前时间戳。
    """
    import time

    signed = {}
    for k, v in dict(params).items():
        if isinstance(v, str):
            v = "".join(ch for ch in v if ch not in _FILTER_CHARS)
        signed[k] = v
    signed["wts"] = int(wts if wts is not None else time.time())
    signed = {k: signed[k] for k in sorted(signed)}
    mixin_key = get_mixin_key(img_key + sub_key)
    query = urlencode(signed)
    signed["w_rid"] = md5((query + mixin_key).encode("utf-8")).hexdigest()
    return signed
