"""wbi.py / dynamic_report.py 纯函数单测（不依赖 astrbot，可本地直接跑）。

    python tests/test_dynamic_report.py
全绿打印 OK。

fixture 依据真实抓取的 x/polymer/web-dynamic/v1/feed/space 返回结构裁剪而来。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wbi  # noqa: E402
from dynamic_report import (  # noqa: E402
    classify,
    should_notify,
    extract_dynamic,
    is_newer_id,
    latest_id,
    select_new_dynamics,
    MAX_PUSH_PER_ROUND,
    KIND_ACTION,
)

# ---------------- wbi ----------------

def test_mixin_key_length_and_stable():
    key = "7cd084941338484aae1ad9425b84077c4932aaff0c93548d8f4d5f9adcbabbcd1a"
    k1 = wbi.get_mixin_key(key)
    assert isinstance(k1, str)
    assert len(k1) == 32
    assert wbi.get_mixin_key(key) == k1  # 稳定可复算


def test_encode_wbi_adds_signature():
    params = {"host_mid": "2267573", "timezone_offset": -480}
    signed = wbi.encode_wbi(params, "aaaa" * 8, "bbbb" * 8, wts=1700000000)
    assert signed["wts"] == 1700000000
    assert "w_rid" in signed and len(signed["w_rid"]) == 32
    # 相同入参 + 相同 wts → 相同 w_rid（确定性）
    signed2 = wbi.encode_wbi(params, "aaaa" * 8, "bbbb" * 8, wts=1700000000)
    assert signed2["w_rid"] == signed["w_rid"]
    # 原入参不被改动
    assert "w_rid" not in params and "wts" not in params


def test_encode_wbi_filters_chars():
    params = {"foo": "a!b'c(d)e*f"}
    signed = wbi.encode_wbi(params, "aaaa" * 8, "bbbb" * 8, wts=1)
    # 值里的 !'()* 被剔除
    assert signed["foo"] == "abcdef"


# ---------------- classify ----------------

def _item(t, major_type=None):
    it = {"type": t, "modules": {"module_dynamic": {"major": {}}}}
    if major_type:
        it["modules"]["module_dynamic"]["major"]["type"] = major_type
    return it


def test_classify_known_types():
    assert classify(_item("DYNAMIC_TYPE_AV")) == "video"
    assert classify(_item("DYNAMIC_TYPE_DRAW")) == "draw"
    assert classify(_item("DYNAMIC_TYPE_ARTICLE")) == "article"
    assert classify(_item("DYNAMIC_TYPE_WORD")) == "word"
    assert classify(_item("DYNAMIC_TYPE_FORWARD")) == "forward"
    assert classify(_item("DYNAMIC_TYPE_LIVE_RCMD")) == "live"
    assert classify(_item("DYNAMIC_TYPE_MUSIC")) == "music"


def test_classify_fallback_to_major():
    # 未收录的 type 字符串 → 走 major.type 兜底
    assert classify(_item("SOMETHING_NEW", "MAJOR_TYPE_ARCHIVE")) == "video"
    assert classify(_item("SOMETHING_NEW", "MAJOR_TYPE_LIVE")) == "live"
    assert classify(_item("SOMETHING_NEW")) == "other"


def test_should_notify():
    togg = {"video": True, "draw": False, "other": True}
    assert should_notify("video", togg) is True
    assert should_notify("draw", togg) is False
    # 未知 kind 走 other
    assert should_notify("weird", togg) is True
    assert should_notify("weird", {"other": False}) is False


# ---------------- extract_dynamic ----------------

def _video_item():
    return {
        "id_str": "1148459269840961545",
        "type": "DYNAMIC_TYPE_AV",
        "modules": {
            "module_author": {"name": "DIYgod", "pub_ts": 1766234911},
            "module_dynamic": {
                "desc": None,
                "major": {
                    "type": "MAJOR_TYPE_ARCHIVE",
                    "archive": {
                        "bvid": "BV1hzqrBtEMP",
                        "cover": "//i2.hdslb.com/bfs/archive/xx.jpg",
                        "title": "欧洲旅游VLOG",
                        "desc": "2022年12月",
                        "jump_url": "//www.bilibili.com/video/BV1hzqrBtEMP",
                    },
                },
            },
        },
    }


def test_extract_video():
    d = extract_dynamic(_video_item())
    assert d["kind"] == "video"
    assert d["action"] == KIND_ACTION["video"] == "投稿了视频"
    assert d["uname"] == "DIYgod"
    assert d["title"] == "欧洲旅游VLOG"
    assert d["images"] == ["https://i2.hdslb.com/bfs/archive/xx.jpg"]
    assert d["url"] == "https://www.bilibili.com/video/BV1hzqrBtEMP"
    assert d["pub_ts"] == 1766234911


def test_extract_draw_opus():
    item = {
        "id_str": "598505999099772730",
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"name": "DIYgod", "pub_ts": 1638188937},
            "module_dynamic": {
                "desc": None,
                "major": {
                    "type": "MAJOR_TYPE_OPUS",
                    "opus": {
                        "jump_url": "//www.bilibili.com/opus/598505999099772730",
                        "title": "",
                        "summary": {"text": "测试图文内容"},
                        "pics": [
                            {"url": "https://i0.hdslb.com/bfs/album/a.png"},
                            {"url": "https://i0.hdslb.com/bfs/album/b.png"},
                            {"url": "https://i0.hdslb.com/bfs/album/c.png"},
                            {"url": "https://i0.hdslb.com/bfs/album/d.png"},
                        ],
                    },
                },
            },
        },
    }
    d = extract_dynamic(item)
    assert d["kind"] == "draw"
    assert d["action"] == "发布了图文动态"
    assert d["text"] == "测试图文内容"
    # 图片截到 MAX_IMAGES=3
    assert len(d["images"]) == 3
    assert d["url"] == "https://www.bilibili.com/opus/598505999099772730"


def test_extract_article_via_opus_title():
    item = {
        "id_str": "9001",
        "type": "DYNAMIC_TYPE_ARTICLE",
        "modules": {
            "module_author": {"name": "某作者", "pub_ts": 1},
            "module_dynamic": {
                "desc": None,
                "major": {
                    "type": "MAJOR_TYPE_OPUS",
                    "opus": {
                        "jump_url": "//www.bilibili.com/opus/9001",
                        "title": "长篇专栏标题",
                        "summary": {"text": "摘要正文"},
                        "pics": [],
                    },
                },
            },
        },
    }
    d = extract_dynamic(item)
    assert d["kind"] == "article"
    assert d["action"] == "投稿了文章"
    assert d["title"] == "长篇专栏标题"
    assert d["text"] == "摘要正文"


def test_extract_word():
    item = {
        "id_str": "777",
        "type": "DYNAMIC_TYPE_WORD",
        "modules": {
            "module_author": {"name": "UP", "pub_ts": 5},
            "module_dynamic": {
                "desc": {"text": "水一条纯文字动态"},
                "major": None,
            },
        },
    }
    d = extract_dynamic(item)
    assert d["kind"] == "word"
    assert d["action"] == "发布了动态"
    assert d["text"] == "水一条纯文字动态"
    assert d["images"] == []
    assert d["url"] == "https://t.bilibili.com/777"


def test_extract_forward_embeds_origin():
    orig_video = _video_item()
    orig_video["id_str"] = "orig_id"
    item = {
        "id_str": "598503014097476780",
        "type": "DYNAMIC_TYPE_FORWARD",
        "modules": {
            "module_author": {"name": "DIYgod", "pub_ts": 1638188242},
            "module_dynamic": {
                "desc": {"text": "我上电视啦！"},
                "major": None,
            },
        },
        "orig": orig_video,
    }
    d = extract_dynamic(item)
    assert d["kind"] == "forward"
    assert d["action"] == "转发了动态"
    assert "我上电视啦！" in d["text"]
    # 原动态标题/UP 拼进正文
    assert "DIYgod" in d["text"]
    assert "欧洲旅游VLOG" in d["text"]


def test_extract_forward_deleted_origin():
    item = {
        "id_str": "1",
        "type": "DYNAMIC_TYPE_FORWARD",
        "modules": {
            "module_author": {"name": "X", "pub_ts": 1},
            "module_dynamic": {"desc": {"text": "转一个"}, "major": None},
        },
        "orig": None,
    }
    d = extract_dynamic(item)
    assert d["kind"] == "forward"
    assert "已删除" in d["text"] or "不可见" in d["text"]


def test_extract_live_rcmd():
    item = {
        "id_str": "5",
        "type": "DYNAMIC_TYPE_LIVE_RCMD",
        "modules": {
            "module_author": {"name": "主播酱", "pub_ts": 9},
            "module_dynamic": {
                "desc": None,
                "major": {
                    "type": "MAJOR_TYPE_LIVE_RCMD",
                    "live_rcmd": {
                        "content": '{"live_play_view":{"title":"帮我看一下","cover":"//i0.jpg/cov.jpg","link":"//live.bilibili.com/123"}}'
                    },
                },
            },
        },
    }
    d = extract_dynamic(item)
    assert d["kind"] == "live"
    assert d["action"] == "开启了直播"
    assert d["title"] == "帮我看一下"
    assert d["images"] == ["https://i0.jpg/cov.jpg"]


# ---------------- 动态 id 游标 ----------------

def test_is_newer_id_numeric_not_lexicographic():
    """老 id 18 位、新 id 19 位：字符串比会把新动态判成更旧，必须按数值比。"""
    assert is_newer_id("1148459269840961545", "598503014097476780") is True
    assert is_newer_id("598503014097476780", "1148459269840961545") is False
    # 字符串比较的反例（说明为什么不能用 >）
    assert ("1148459269840961545" > "598503014097476780") is False


def test_is_newer_id_edge_cases():
    assert is_newer_id("100", "") is True          # 旧基线为空 = 还没有基线
    assert is_newer_id("100", None) is True
    assert is_newer_id("100", "100") is False      # 同一条不算新
    assert is_newer_id("", "100") is False         # 空新值不许写坏游标
    assert is_newer_id(None, "100") is False


def test_is_newer_id_non_numeric_fallback():
    # 非纯数字的脏数据不能抛异常，退化成字符串比较
    assert is_newer_id("abc", "100") is False      # ("0",...) < ("1",...)
    assert is_newer_id("1148459269840961x", "598503014097476780") is False


def test_latest_id_picks_numeric_max_not_first():
    items = [{"id_str": "598503014097476780"}, {"id_str": "1148459269840961545"}, {"id_str": ""}, {}]
    assert latest_id(items) == "1148459269840961545"
    assert latest_id([]) is None
    assert latest_id([{"id_str": None}]) is None


def test_select_new_dynamics_order_and_cap():
    items = [{"id_str": "200"}, {"id_str": "150"}, {"id_str": "100"}]
    # 正序（旧→新）
    assert [it["id_str"] for it in select_new_dynamics(items, "100")] == ["150", "200"]
    # 全都不新 → 空
    assert select_new_dynamics(items, "200") == []
    # 封顶：只保留最近的 limit 条
    many = [{"id_str": str(1000 + i)} for i in range(15)]
    picked = select_new_dynamics(many, "1000", limit=3)
    assert [it["id_str"] for it in picked] == ["1012", "1013", "1014"]
    assert MAX_PUSH_PER_ROUND == 10


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
