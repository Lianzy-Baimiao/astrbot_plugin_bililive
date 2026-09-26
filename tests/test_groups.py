# -*- coding: utf-8 -*-
"""groups.py 单测：umo 解析 / 展示文字 / 群名缓存 / 平台返回值归一（不依赖 astrbot）。

    python tests/test_groups.py
全绿打印 OK。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from groups import (  # noqa: E402
    GROUP_MSG_TYPE,
    SOURCE_API,
    SOURCE_EVENT,
    GroupNameResolver,
    group_id_from_umo,
    parse_group_info,
    parse_group_list,
    session_label,
    split_umo,
    umo_for,
)

GUMO = "napcat:GroupMessage:339466990"


def _resolver(tmpdir, name="groups.json", **kw):
    return GroupNameResolver(os.path.join(tmpdir, name), **kw)


# ---------------- umo 解析 ----------------


def test_split_umo():
    assert split_umo(GUMO) == ("napcat", GROUP_MSG_TYPE, "339466990")
    assert split_umo("napcat:FriendMessage:777") == ("napcat", "FriendMessage", "777")
    # 裸群号不能把群号当成平台名
    assert split_umo("339466990") == ("", "", "339466990")
    # 两段（平台id:群号 简写）
    assert split_umo("napcat:339466990") == ("", "napcat", "339466990")
    # 段数超了：多余段还原回 session_id
    assert split_umo("plat:GroupMessage:a:b") == ("plat", "GroupMessage", "a:b")
    assert split_umo("") == ("", "", "")
    assert group_id_from_umo(GUMO) == "339466990"


def test_umo_for():
    assert umo_for("napcat", "123") == "napcat:GroupMessage:123"
    assert umo_for("napcat", 123) == "napcat:GroupMessage:123"
    assert umo_for("", "123") == "123"  # 没平台就退回裸号
    assert umo_for("napcat", "") == ""


def test_session_label():
    # 有群名：群名（群号）
    assert session_label(GUMO, {"group_name": "魔兽世界交流群"}) == "魔兽世界交流群（339466990）"
    # 没群名：退回群 群号
    assert session_label(GUMO, None) == "群 339466990"
    assert session_label(GUMO, {"group_name": "  "}) == "群 339466990"
    # 私聊不叫「群」
    assert session_label("napcat:FriendMessage:777", None) == "私聊 777"
    assert session_label("napcat:FriendMessage:777", {"group_name": "阿呆"}) == "阿呆（777）"
    # 手填的裸群号当群看
    assert session_label("339466990", None) == "群 339466990"
    # 认不出来的原样返回，不显示错的
    assert session_label("whatever", None) == "whatever"


# ---------------- 平台返回值归一 ----------------


def test_parse_group_list_variants():
    want = [{"group_id": "123", "group_name": "群一", "member_count": 5}]
    # 裸数组
    assert parse_group_list([{"group_id": 123, "group_name": "群一", "member_count": 5}]) == want
    # {"data": [...]}
    assert parse_group_list({"data": [{"group_id": "123", "group_name": "群一", "member_count": 5}]}) == want
    # {"data": {"group_list": [...]}}（NapCat 风格）
    assert parse_group_list(
        {"status": "ok", "data": {"group_list": [{"group_id": "123", "group_name": "群一", "member_count": 5}]}}
    ) == want
    # {"group_list": [...]}
    assert parse_group_list({"group_list": [{"group_id": "123", "group_name": "群一", "member_count": 5}]}) == want
    # 脏数据：没群号的跳过，member_count 坏值当 0，name 字段兜底
    assert parse_group_list([{"group_name": "没群号"}, "x", None, {"id": 9, "name": "别名群"}]) == [
        {"group_id": "9", "group_name": "别名群", "member_count": 0}
    ]
    assert parse_group_list(None) == []
    assert parse_group_list("炸了") == []


def test_parse_group_info():
    assert parse_group_info({"group_id": 1, "group_name": "群一", "member_count": 3}) == {
        "group_id": "1",
        "group_name": "群一",
        "member_count": 3,
    }
    assert parse_group_info({"data": {"group_id": "1", "group_name": "群一"}}) == {
        "group_id": "1",
        "group_name": "群一",
        "member_count": 0,
    }
    assert parse_group_info({}) == {}
    assert parse_group_info(None) == {}



# ---------------- 缓存 ----------------


def test_remember_and_label():
    with tempfile.TemporaryDirectory() as tmp:
        r = _resolver(tmp)
        assert r.remember(GUMO, group_name="魔兽世界交流群", member_count=486) is True
        assert r.name_of(GUMO) == "魔兽世界交流群"
        assert r.label(GUMO) == "魔兽世界交流群（339466990）"
        assert r.get(GUMO)["member_count"] == 486
        assert r.get(GUMO)["source"] == SOURCE_EVENT
        # 内容没变 → 不重复落盘
        assert r.remember(GUMO, group_name="魔兽世界交流群", member_count=486) is False
        # 空名字不覆盖已有名字（拿到空值就写会把好名字洗掉）
        assert r.remember(GUMO, group_name="", member_count=0) is False
        assert r.name_of(GUMO) == "魔兽世界交流群"
        # 换名字要覆盖
        assert r.remember(GUMO, group_name="新名字") is True
        assert r.name_of(GUMO) == "新名字"
        # 只有裸号时也要能存下（群号/平台 id 从 umo 里补）
        bare = _resolver(tmp, "b.json")
        bare.remember("339466990", group_name="裸号群")
        assert bare.get("339466990")["group_id"] == "339466990"
        # get 返回副本，改它不影响缓存
        got = r.get(GUMO)
        got["group_name"] = "被改了"
        assert r.name_of(GUMO) == "新名字"


def test_save_load_and_recovery():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "groups.json")
        r = GroupNameResolver(path)
        r.remember(GUMO, group_name="魔兽世界交流群", member_count=486)
        r.merge_api_groups(
            "napcat",
            [{"group_id": "2002", "group_name": "活动通知群", "member_count": 120}],
        )
        r.mark_refreshed()

        fresh = GroupNameResolver(path)
        fresh.load()
        assert fresh.name_of(GUMO) == "魔兽世界交流群"
        assert fresh.name_of("napcat:GroupMessage:2002") == "活动通知群"
        assert fresh.get("napcat:GroupMessage:2002")["source"] == SOURCE_API
        assert fresh.needs_refresh() is False  # last_refresh 也一起存活

        # 文件被写坏 → 当空库启动，不抛异常
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ 不是 json")
        broken = GroupNameResolver(path)
        broken.load()
        assert broken.entries() == {}
        broken.remember(GUMO, group_name="修好了")
        assert json.load(open(path, encoding="utf-8"))["version"] == 1

        # 单条坏数据跳过，好的留着
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "version": 1,
                    "groups": {
                        GUMO: "不是对象",
                        "napcat:GroupMessage:2": {"group_name": "好数据"},
                    },
                },
                fh,
                ensure_ascii=False,
            )
        partial = GroupNameResolver(path)
        partial.load()
        assert set(partial.entries().keys()) == {"napcat:GroupMessage:2"}



def test_merge_api_groups_and_find_name():
    with tempfile.TemporaryDirectory() as tmp:
        r = _resolver(tmp)
        changed = r.merge_api_groups(
            "napcat",
            [
                {"group_id": "1", "group_name": "群一", "member_count": 10},
                {"group_id": "2", "group_name": "群二"},
            ],
        )
        assert changed == 2
        assert (
            r.merge_api_groups(
                "napcat", [{"group_id": "1", "group_name": "群一", "member_count": 10}]
            )
            == 0
        )
        # 裸 OneBot 返回形状也能吃
        assert (
            r.merge_api_groups(
                "napcat", {"data": [{"group_id": "3", "group_name": "群三"}]}
            )
            == 1
        )
        # 跨 umo 兜底查名字
        assert r.find_name("napcat", "2") == "群二"
        assert r.find_name("", "3") == "群三"
        assert r.find_name("napcat", "不存在") == ""


def test_prune_keeps_newest():
    with tempfile.TemporaryDirectory() as tmp:
        r = _resolver(tmp, max_entries=2)
        r.remember("napcat:GroupMessage:1", group_name="群一")
        r.remember("napcat:GroupMessage:2", group_name="群二")
        r._groups["napcat:GroupMessage:1"]["updated_at"] = 1.0  # 手动做旧
        r._groups["napcat:GroupMessage:2"]["updated_at"] = 2.0
        r.remember("napcat:GroupMessage:3", group_name="群三")
        assert set(r.entries().keys()) == {"napcat:GroupMessage:2", "napcat:GroupMessage:3"}


def test_needs_refresh_and_flush():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "groups.json")
        r = GroupNameResolver(path)
        assert r.needs_refresh() is True  # 从没刷过
        r.mark_refreshed()
        assert r.needs_refresh() is False
        assert r.needs_refresh(interval=0) is True
        # 空缓存 flush 不凭空造文件
        empty_path = os.path.join(tmp, "empty.json")
        GroupNameResolver(empty_path).flush()
        assert os.path.exists(empty_path) is False
        r.remember(GUMO, group_name="群一")
        r.flush()
        assert os.path.exists(path) is True


def test_label_falls_back_to_group_id_lookup():
    """umo 拼法不同（配置的默认平台 vs 事件的实例 id）时也要认得出名字。"""
    with tempfile.TemporaryDirectory() as tmp:
        r = _resolver(tmp)
        r.remember("napcat:GroupMessage:1001", group_name="天刀交流群")
        # 精确命中
        assert r.label("napcat:GroupMessage:1001") == "天刀交流群（1001）"
        # 同一个群、换了平台前缀 → 靠「群号 + 平台兜底」找到名字
        assert r.label("aiocqhttp:GroupMessage:1001") == "天刀交流群（1001）"
        # 完全不认识的群还是「群 群号」
        assert r.label("aiocqhttp:GroupMessage:9999") == "群 9999"
        # 私聊不受影响
        assert r.label("napcat:FriendMessage:777") == "私聊 777"
        # 同名群号但平台不同：有歧义就不猜，宁可显示群号
        r.remember("telegram:GroupMessage:1001", group_name="电报群")
        assert r.label("aiocqhttp:GroupMessage:1001") == "群 1001"
        assert r.label("telegram:GroupMessage:1001") == "电报群（1001）"
        assert r.label("napcat:GroupMessage:1001") == "天刀交流群（1001）"
        # find_name 默认不跨平台，显式放开时才用唯一候选兜底
        assert r.find_name("aiocqhttp", "1001") == ""
        assert r.find_name("aiocqhttp", "1001", allow_any_platform=True) == ""
        assert r.find_name("napcat", "1001") == "天刀交流群"


def main():
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    for name, fn in tests:
        fn()
        print(f"  {name} ok")
    print(f"OK ({len(tests)} tests)")


if __name__ == "__main__":
    main()
