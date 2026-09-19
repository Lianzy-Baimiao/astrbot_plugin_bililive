"""subscription.py 纯函数单测（不依赖 astrbot，可本地直接跑）。

    python tests/test_subscription.py
全绿打印 OK。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subscription import (  # noqa: E402
    normalize_target,
    shorten_target,
    group_display,
    parse_line,
    parse_subscriptions,
    serialize_subscriptions,
    add_subscription,
    remove_subscription,
    subscriptions_for_group,
    group_at_all,
    all_uids,
)

G1 = "aiocqhttp:GroupMessage:777777777"
G2 = "aiocqhttp:GroupMessage:888888888"


def t(umo, at_all=False):
    return {"umo": umo, "at_all": at_all}


def test_normalize_bare_group():
    assert normalize_target("777777777") == G1
    assert normalize_target("777777777", "napcat") == "napcat:GroupMessage:777777777"


def test_normalize_two_part_shorthand():
    # 平台id:群号 → 补全 GroupMessage
    assert normalize_target("napcat:777777777") == "napcat:GroupMessage:777777777"
    assert normalize_target("default_666666666:777777777") == "default_666666666:GroupMessage:777777777"
    # 两段简写不受 default_platform 影响（平台已写明）
    assert normalize_target("napcat:777777777", "aiocqhttp") == "napcat:GroupMessage:777777777"


def test_normalize_full_umo_untouched():
    umo = "napcat:GroupMessage:777777777"
    assert normalize_target(umo) == umo
    # 完整 umo 不受 default_platform 影响
    assert normalize_target(umo, "qq_official") == umo


def test_normalize_empty():
    assert normalize_target("") == ""
    assert normalize_target("   ") == ""
    assert normalize_target("napcat:") == ""  # 缺群号


def test_shorten_target():
    assert shorten_target("napcat:GroupMessage:777777777") == "napcat:777777777"
    # 非群消息保持完整
    assert shorten_target("napcat:FriendMessage:123") == "napcat:FriendMessage:123"
    # 已经是简写/裸号原样
    assert shorten_target("napcat:777777777") == "napcat:777777777"
    assert shorten_target("") == ""


def test_group_display():
    assert group_display("aiocqhttp:GroupMessage:777777777") == "777777777"
    assert group_display("777777777") == "777777777"
    assert group_display("") == ""


def test_parse_line_basic():
    uid, info = parse_line("111111111=777777777")
    assert uid == "111111111"
    assert info["groups"] == [t(G1)]


def test_parse_line_multi_group():
    uid, info = parse_line("111111111=777777777,888888888")
    assert info["groups"] == [t(G1), t(G2)]


def test_parse_line_target_at_all():
    """目标级 @all 只标记那一个群。"""
    uid, info = parse_line("111111111=777777777,napcat:888888888@all")
    assert info["groups"] == [t(G1), t("napcat:GroupMessage:888888888", True)]


def test_parse_line_line_at_all_legacy():
    """旧写法：行尾 | at_all 给整行所有目标打上 at_all。"""
    uid, info = parse_line("111111111=777777777,888888888 | at_all")
    assert info["groups"] == [t(G1, True), t(G2, True)]
    # 行级标记与目标级取或
    _, info2 = parse_line("111111111=777777777@all,888888888 | at_all")
    assert info2["groups"] == [t(G1, True), t(G2, True)]


def test_parse_line_full_umo():
    uid, info = parse_line("333333333=aiocqhttp:GroupMessage:777777777")
    assert info["groups"] == [t(G1)]


def test_parse_line_invalid():
    assert parse_line("") is None
    assert parse_line("# 注释") is None
    assert parse_line("abc=123") is None  # UID 非数字
    assert parse_line("123456") is None  # 没有 =


def test_parse_line_dedup_groups():
    uid, info = parse_line("111111111=777777777,777777777")
    assert info["groups"] == [t(G1)]
    # 同目标重复但 at_all 不同时取或
    _, info2 = parse_line("111111111=777777777,777777777@all")
    assert info2["groups"] == [t(G1, True)]


def test_parse_subscriptions_merge():
    subs = parse_subscriptions([
        "111111111=777777777",
        "111111111=napcat:888888888@all",  # 同 UID 合并
        "222222222=777777777",
    ])
    assert set(subs.keys()) == {"111111111", "222222222"}
    assert subs["111111111"]["groups"] == [
        t(G1),
        t("napcat:GroupMessage:888888888", True),
    ]


def test_roundtrip():
    lines = [
        "111111111=777777777,napcat:888888888@all",
        "222222222=777777777 | at_all",
        "# 注释行",
    ]
    subs = parse_subscriptions(lines)
    out = serialize_subscriptions(subs)
    subs2 = parse_subscriptions(out)
    assert subs == subs2


def test_serialize_drops_empty():
    subs = {"111111111": {"groups": []}}
    assert serialize_subscriptions(subs) == []


def test_serialize_target_at_all():
    subs = parse_subscriptions(["111111111=777777777,888888888@all"])
    out = serialize_subscriptions(subs)
    assert out == ["111111111=aiocqhttp:777777777,aiocqhttp:888888888@all"]


def test_add_subscription():
    subs = {}
    assert add_subscription(subs, "111111111", G1) is True
    # 重复添加不算变化
    assert add_subscription(subs, "111111111", G1) is False
    # 加第二个群
    assert add_subscription(subs, "111111111", G2) is True
    assert len(subs["111111111"]["groups"]) == 2
    # 给已有目标开 at_all
    assert add_subscription(subs, "111111111", G1, at_all=True) is True
    assert subs["111111111"]["groups"][0] == t(G1, True)
    assert subs["111111111"]["groups"][1] == t(G2)
    # 已开过再开不算变化
    assert add_subscription(subs, "111111111", G1, at_all=True) is False


def test_add_subscription_invalid():
    subs = {}
    assert add_subscription(subs, "abc", "aiocqhttp:GroupMessage:1") is False
    assert add_subscription(subs, "123", "") is False
    assert subs == {}


def test_remove_subscription():
    subs = parse_subscriptions(["111111111=777777777,888888888"])
    # 移一个群，UP 还在
    assert remove_subscription(subs, "111111111", G1) is True
    assert subs["111111111"]["groups"] == [t(G2)]
    # 移最后一个群，整条删除
    assert remove_subscription(subs, "111111111", G2) is True
    assert "111111111" not in subs
    # 再移不存在的
    assert remove_subscription(subs, "111111111", G1) is False


def test_subscriptions_for_group():
    subs = parse_subscriptions([
        "111111111=777777777,888888888",
        "222222222=777777777",
        "333333333=888888888",
    ])
    assert subscriptions_for_group(subs, G1) == ["111111111", "222222222"]
    assert subscriptions_for_group(subs, G2) == ["111111111", "333333333"]


def test_group_at_all():
    subs = parse_subscriptions(["111111111=777777777@all,888888888"])
    assert group_at_all(subs, "111111111", G1) is True
    assert group_at_all(subs, "111111111", G2) is False
    assert group_at_all(subs, "999", G1) is False


def test_all_uids():
    subs = parse_subscriptions([
        "111111111=777777777",
        "222222222=777777777",
    ])
    assert set(all_uids(subs)) == {"111111111", "222222222"}


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
