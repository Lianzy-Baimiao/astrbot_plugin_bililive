"""comment_report.py 纯函数单测（不依赖 astrbot，可本地直接跑）。

    python tests/test_comment_report.py
全绿打印 OK。

fixture 依据真实抓取的 x/v2/reply 返回结构裁剪而来
（视频 BV1V9aq6CEeR / UP 2081532576 的置顶评论）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from comment_report import bv2av, extract_pinned, MAX_IMAGES  # noqa: E402


# ---------------- bv2av ----------------

def test_bv2av_known():
    # 真实验证过的一组：BV1V9aq6CEeR -> 117346039501000
    assert bv2av("BV1V9aq6CEeR") == 117346039501000


def test_bv2av_invalid():
    assert bv2av("") == 0
    assert bv2av(None) == 0
    assert bv2av("av12345") == 0
    assert bv2av("BV1V9aq6CEe") == 0        # 长度不对（11 位）
    assert bv2av("XX1V9aq6CEeR") == 0       # 非 BV 开头
    assert bv2av("BV1V9aq6CEe!") == 0       # 含字母表外字符


# ---------------- extract_pinned ----------------

def _reply(rpid, mid, msg="", pics=None, uname="魔兽阿落", ctime=1790559841):
    content = {"message": msg}
    if pics is not None:
        content["pictures"] = pics
    return {
        "rpid": rpid,
        "mid": mid,
        "ctime": ctime,
        "member": {"uname": uname},
        "content": content,
    }


def test_extract_pinned_from_upper_top():
    data = {
        "upper": {
            "mid": 2081532576,
            "top": _reply(
                315305036289, 2081532576, "9.28推荐",
                pics=[{"img_src": "http://i0.hdslb.com/bfs/new_dyn/abc.jpg"}],
            ),
        },
        "top_replies": [],
    }
    out = extract_pinned(data, "2081532576", "BV1V9aq6CEeR")
    assert out is not None
    assert out["rpid"] == "315305036289"
    assert out["uid"] == "2081532576"
    assert out["uname"] == "魔兽阿落"
    assert out["text"] == "9.28推荐"
    # http 图升到 https
    assert out["images"] == ["https://i0.hdslb.com/bfs/new_dyn/abc.jpg"]
    # 落地链接锚到该评论
    assert out["url"] == "https://www.bilibili.com/video/BV1V9aq6CEeR/#reply315305036289"


def test_extract_pinned_uid_mismatch_returns_none():
    # upper.top 的 mid 不是目标 UP（别人的置顶不推）
    data = {"upper": {"mid": 999, "top": _reply(1, 999, "别人的")}, "top_replies": []}
    assert extract_pinned(data, "2081532576", "BV1V9aq6CEeR") is None


def test_extract_pinned_fallback_top_replies():
    # upper.top 缺失/不符时，从 top_replies 里找 mid==uid 的
    data = {
        "upper": {},
        "top_replies": [
            _reply(11, 999, "路人置顶"),
            _reply(22, 2081532576, "UP的置顶", pics=[]),
        ],
    }
    out = extract_pinned(data, "2081532576")
    assert out is not None
    assert out["rpid"] == "22"
    assert out["text"] == "UP的置顶"
    assert out["images"] == []
    # 没给 bvid 时链接为空
    assert out["url"] == ""


def test_extract_pinned_empty_data():
    assert extract_pinned({}, "2081532576") is None
    assert extract_pinned(None, "2081532576") is None
    assert extract_pinned({"upper": {"top": _reply(1, 2081532576)}}, "") is None


def test_extract_pinned_images_capped():
    pics = [{"img_src": f"https://x/{i}.jpg"} for i in range(10)]
    data = {"upper": {"mid": 1, "top": _reply(1, 1, "多图", pics=pics)}}
    out = extract_pinned(data, "1", "BV1V9aq6CEeR")
    assert len(out["images"]) == MAX_IMAGES


def test_extract_pinned_no_rpid_is_invalid():
    data = {"upper": {"mid": 1, "top": _reply("", 1, "无id")}}
    assert extract_pinned(data, "1") is None


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
