import asyncio
import aiohttp
import html
import json
import os
import re
import time
from datetime import datetime
from urllib.parse import urlencode
from typing import Any, Dict, List, Optional
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from .bili_login import BilibiliLoginManager
from . import subscription as sub
from . import dynamic_report
from . import wbi
from .groups import (
    SOURCE_API,
    GroupNameResolver,
    parse_group_info,
    parse_group_list,
)
from .page import BiliLivePageController

PLUGIN_NAME = "astrbot_plugin_bililive"


class _SafeFormatDict(dict):
    """format_map 用：未知占位符原样保留而不是抛 KeyError，模板写错不炸推送。"""

    def __missing__(self, key):
        return "{" + key + "}"


@register("astrbot_plugin_bililive", "BB0813", "B站UP主开播监测与动态推送插件", "2.3.5",
          "https://github.com/Lianzy-Baimiao/astrbot_plugin_bililive")
class BiliLivePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        # 直播状态缓存 {uid: last_live_status}，每个UP一份（开播是UP的属性，与群无关）
        self.live_status_cache: Dict[str, int] = {}
        self.uid_error_counts: Dict[str, int] = {}
        self.uid_skip_until: Dict[str, float] = {}
        self.current_interval = max(30, min(600, self._cfg_int("check_interval", 60)))
        self._last_rate_limited = False

        # 动态监测状态 {uid: 已推送过的最新动态id_str}
        # 动态 id 是B站全局递增数字串，比时间戳更适合做"是否已推"判断
        self.dyn_last_ids: Dict[str, str] = {}
        self.dyn_error_counts: Dict[str, int] = {}
        self.dyn_skip_until: Dict[str, float] = {}
        self.dyn_current_interval = float(self.dynamic_check_interval)
        self._dyn_last_rate_limited = False

        # WBI 签名素材 {img_key, sub_key, fetched_at} / buvid 访客指纹 {b3, b4, fetched_at}
        self._wbi_keys: Dict[str, Any] = {}
        self._buvid: Dict[str, Any] = {}

        self._init_lock = asyncio.Lock()
        self._initialized = False
        self.monitor_task = None
        self.dyn_monitor_task = None
        self.session = None

        # 数据目录（规范路径：data/plugin_data/astrbot_plugin_bililive/）
        self.data_dir = self._get_data_dir()
        self.state_file = os.path.join(self.data_dir, "state.json")
        self.group_settings_file = os.path.join(self.data_dir, "groups.json")
        self.group_settings: Dict[str, Dict] = {}  # {umo: {notify: bool, notify_end: bool}}

        # 登录管理器
        self.login_manager = BilibiliLoginManager(
            context, self._save_cookie_to_config,
            admin_ids_provider=self._admin_ids,
            admin_targets_provider=self._admin_notify_targets,
        )
        # initialize() 由框架在加载插件后自动 await（官方生命周期钩子）

        # Web 面板：群号 → 群名缓存（OneBot 事件不带群名，自己攒）+ 接口注册
        # 注意：文件名用 group_names.json，**不能**用 groups.json —— 那个已被上面的
        # group_settings_file（按群通知开关）占用，同名会两套数据互相覆盖、双双丢失。
        self.groups = GroupNameResolver(os.path.join(self.data_dir, "group_names.json"))
        self.groups.load()
        self._group_name_tasks: Dict[str, asyncio.Task] = {}
        self.page = BiliLivePageController(context, self)
        self.page.register_routes()

    # ---------- 配置读取小工具 ----------
    def _cfg(self, key: str, default=None):
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default))
        except Exception:
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        val = self._cfg(key, default)
        return bool(val) if val is not None else default

    @property
    def enable_notifications(self) -> bool:
        return self._cfg_bool("enable_notifications", True)

    @property
    def enable_end_notifications(self) -> bool:
        return self._cfg_bool("enable_end_notifications", True)

    @property
    def check_interval(self) -> int:
        # 钳制在 30-600 秒：太小会被 B 站限流，太大失去监测意义
        return max(30, min(600, self._cfg_int("check_interval", 60)))

    @property
    def dynamic_check_interval(self) -> int:
        """动态检查间隔（秒）。动态接口风控比直播状态接口敏感得多，同样钳制 30-600。"""
        return max(30, min(600, self._cfg_int("dynamic_check_interval", 45)))

    @property
    def dynamic_notify_toggles(self) -> Dict[str, bool]:
        """各类动态的推送开关，键与 dynamic_report 的 kind 一一对应。"""
        return {
            "video": self._cfg_bool("dyn_notify_video", True),
            "draw": self._cfg_bool("dyn_notify_draw", True),
            "article": self._cfg_bool("dyn_notify_article", True),
            "word": self._cfg_bool("dyn_notify_word", True),
            "forward": self._cfg_bool("dyn_notify_forward", True),
            "live": self._cfg_bool("dyn_notify_live", False),
            "music": self._cfg_bool("dyn_notify_music", True),
            "other": self._cfg_bool("dyn_notify_other", False),
        }

    @property
    def max_monitors(self) -> int:
        return self._cfg_int("max_monitors", 50)

    @property
    def default_platform(self) -> str:
        """裸群号补全用的平台『实例id』。

        AstrBot 的 send_message 按**实例 id** 匹配平台，所以配置里若填了适配器类型名
        （aiocqhttp / qq_official），这里会映射到同类型的第一个实例 id；都探不到才回落
        aiocqhttp（与旧行为一致）。
        """
        return self._bare_id_platform()

    def _platform_ids(self) -> List[str]:
        """当前已加载平台的实例 id 列表；取不到返回空。"""
        return [pid for pid, _ptype in self._platform_instances()]

    def _platform_instances(self) -> List[tuple]:
        """当前已加载平台的 (实例id, 适配器类型名) 列表；取不到返回空。

        兼容两种取法：老版本 platform_manager.platform_insts，以及带 get_insts() 的版本。
        """
        for get in (
            lambda: self.context.platform_manager.platform_insts,
            lambda: self.context.get_platform_insts(),
            lambda: self.context.platform_manager.get_insts(),
        ):
            try:
                insts = list(get() or [])
            except Exception:
                continue
            out = []
            for p in insts:
                try:
                    meta = p.meta()
                    out.append((str(meta.id), str(meta.name)))
                except Exception:
                    continue
            if out:
                return out
        return []

    def _bare_id_platform(self) -> str:
        """把「裸群号」挂到哪个平台实例 id 上。"""
        configured = str(self._cfg("default_platform", "") or "").strip()
        insts = self._platform_instances()
        ids = [pid for pid, _ in insts]
        if configured:
            if not ids or configured in ids:
                return configured
            # 配置里填的是适配器类型名 → 映射到同类型的第一个实例 id
            for pid, ptype in insts:
                if ptype == configured:
                    return pid
            return configured  # 都映射不上就尊重用户填的值（至少行为可预期）
        for pid, ptype in insts:
            if ptype == "aiocqhttp":
                return pid
        return ids[0] if ids else "aiocqhttp"

    # ---------- 订阅数据：config 是唯一真相源 ----------
    def _load_subs(self) -> Dict[str, Dict]:
        """从 config 读取并解析订阅（每次现读现解析，保证与WebUI改动同步）。"""
        return sub.parse_subscriptions(self._cfg("subscriptions", []) or [], self.default_platform)

    def _invalid_sub_lines(self) -> List[str]:
        """配置里无法解析的订阅行（排除空行和 # 注释），用于日志与状态提示。"""
        lines = self._cfg("subscriptions", []) or []
        bad = []
        for ln in lines:
            s = str(ln).strip()
            if s and not s.startswith("#") and sub.parse_line(s, self.default_platform) is None:
                bad.append(s)
        return bad

    async def _save_subs(self, subs: Dict[str, Dict]):
        """把订阅结构序列化回 config 并落盘（WebUI 与命令共用）。"""
        lines = sub.serialize_subscriptions(subs)
        try:
            self.config["subscriptions"] = lines
        except Exception as e:
            logger.error(f"写入订阅到配置失败: {e}")
            return
        await self._persist_config()
        self._prune_group_settings(subs)

    # ---------- 动态订阅（与开播订阅完全独立，格式相同） ----------
    def _load_dyn_subs(self) -> Dict[str, Dict]:
        """从 config 读取并解析动态订阅（每次现读现解析，保证与WebUI改动同步）。"""
        return sub.parse_subscriptions(self._cfg("dynamic_subscriptions", []) or [], self.default_platform)

    def _invalid_dyn_sub_lines(self) -> List[str]:
        """动态订阅里无法解析的行（排除空行和 # 注释），用于日志与状态提示。"""
        lines = self._cfg("dynamic_subscriptions", []) or []
        bad = []
        for ln in lines:
            s = str(ln).strip()
            if s and not s.startswith("#") and sub.parse_line(s, self.default_platform) is None:
                bad.append(s)
        return bad

    async def _save_dyn_subs(self, subs: Dict[str, Dict]):
        """把动态订阅序列化回 config 并落盘（WebUI 与命令共用）。"""
        lines = sub.serialize_subscriptions(subs)
        try:
            self.config["dynamic_subscriptions"] = lines
        except Exception as e:
            logger.error(f"写入动态订阅到配置失败: {e}")
            return
        await self._persist_config()
        self._prune_group_settings(dyn_subs=subs)

    async def _persist_config(self):
        """调用 AstrBotConfig 的保存方法，兼容同步/异步两种。"""
        try:
            if hasattr(self.config, "save_config_async"):
                await self.config.save_config_async()
            elif hasattr(self.config, "save_config"):
                self.config.save_config()
            else:
                logger.warning("配置对象无 save_config 方法，改动可能不持久")
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    def _get_data_dir(self) -> str:
        """规范数据目录：data/plugin_data/astrbot_plugin_bililive/"""
        try:
            from astrbot.api.star import StarTools
            base = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path
                base = os.path.join(get_astrbot_data_path(), "plugin_data", PLUGIN_NAME)
            except Exception:
                base = os.path.join(os.path.expanduser("~"), ".astrbot", "plugin_data", PLUGIN_NAME)
        os.makedirs(base, exist_ok=True)
        return base

    # ---------- Web 面板：群名与群列表 ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """任何消息都顺手记一下群名（面板要按群名选推送目标）。不产出回复。"""
        try:
            self._remember_group(event)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"记录群名失败: {e}")

    @staticmethod
    def _event_group_name(event: AstrMessageEvent) -> str:
        """事件自带的群名（Telegram / Discord 等平台有，OneBot 没有）。"""
        group = getattr(getattr(event, "message_obj", None), "group", None)
        return str(getattr(group, "group_name", "") or "").strip()

    @staticmethod
    def _event_platform_id(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_platform_id", None)
        if callable(getter):
            try:
                return str(getter() or "")
            except Exception:  # noqa: BLE001
                pass
        meta = getattr(event, "platform_meta", None)
        return str(getattr(meta, "id", "") or "")

    @staticmethod
    def _event_group_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_group_id() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _remember_group(self, event: AstrMessageEvent) -> None:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        gid = self._event_group_id(event)
        self.groups.remember(
            umo,
            group_id=gid,
            group_name=self._event_group_name(event),
            platform_id=self._event_platform_id(event),
        )
        if gid and not self.groups.name_of(umo):
            self._schedule_group_name_lookup(event, umo, gid)

    def _schedule_group_name_lookup(self, event: AstrMessageEvent, umo: str, gid: str) -> None:
        """首次见到某个群时后台问一次 get_group_info（不阻塞消息处理）。"""
        if umo in self._group_name_tasks:
            return
        client = getattr(event, "bot", None) or getattr(event, "client", None)
        if client is None or not callable(getattr(client, "call_action", None)):
            return
        try:
            task = asyncio.create_task(
                self._learn_group_name(client, umo, gid, self._event_platform_id(event))
            )
        except RuntimeError:  # 没有运行中的事件循环
            return
        self._group_name_tasks[umo] = task
        task.add_done_callback(lambda _t, key=umo: self._group_name_tasks.pop(key, None))

    async def _learn_group_name(self, client, umo: str, gid: str, platform_id: str) -> None:
        try:
            result = await client.call_action(
                "get_group_info", group_id=int(gid) if str(gid).isdigit() else gid
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"查询群 {gid} 名称失败: {e}")
            return
        info = parse_group_info(result)
        name = str(info.get("group_name") or "").strip()
        if not name:
            return
        self.groups.remember(
            umo,
            group_id=gid or str(info.get("group_id") or ""),
            group_name=name,
            platform_id=platform_id,
            member_count=info.get("member_count"),
            source=SOURCE_API,
        )

    @staticmethod
    def _inst_platform_id(inst) -> str:
        meta = getattr(inst, "meta", None)
        if callable(meta):
            try:
                return str(getattr(meta(), "id", "") or "")
            except Exception:  # noqa: BLE001
                pass
        config = getattr(inst, "config", None)
        if isinstance(config, dict):
            return str(config.get("id") or "")
        return ""

    def _platform_clients(self):
        """列出 (平台实例 id, 客户端对象)；取不到平台管理器时一个都不返回。"""
        manager = getattr(self.context, "platform_manager", None)
        getter = getattr(manager, "get_insts", None)
        if not callable(getter):
            return
        try:
            insts = list(getter() or [])
        except Exception as e:  # noqa: BLE001
            logger.debug(f"取平台实例失败: {e}")
            return
        for inst in insts:
            client = None
            get_client = getattr(inst, "get_client", None)
            if callable(get_client):
                try:
                    client = get_client()
                except Exception:  # noqa: BLE001
                    client = None
            if client is None:
                client = getattr(inst, "bot", None) or getattr(inst, "client", None)
            if client is not None:
                yield self._inst_platform_id(inst), client

    async def refresh_group_names(self, force: bool = False, interval: int = 300) -> int:
        """去平台要一遍群列表（OneBot get_group_list）补齐群名，返回有变化的群数。"""
        if not force and not self.groups.needs_refresh(interval):
            return 0
        changed = 0
        for platform_id, client in self._platform_clients():
            action = getattr(client, "call_action", None)
            if not callable(action):
                continue
            try:
                result = await action("get_group_list")
            except Exception as e:  # noqa: BLE001
                logger.debug(f"平台 {platform_id or '?'} 取群列表失败: {e}")
                continue
            changed += self.groups.merge_api_groups(platform_id, parse_group_list(result))
        self.groups.mark_refreshed()
        return changed

    # ---------- 按群通知开关（groups.json） ----------
    def _load_group_settings(self):
        try:
            if os.path.exists(self.group_settings_file):
                with open(self.group_settings_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.group_settings = {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except Exception as e:
            logger.error(f"加载群通知设置失败: {e}")

    def _save_group_settings(self):
        try:
            with open(self.group_settings_file, "w", encoding="utf-8") as f:
                json.dump(self.group_settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存群通知设置失败: {e}")

    def _group_notify_enabled(self, origin: str, kind: str) -> bool:
        """某会话的通知开关（kind: notify / notify_end），未设置默认开启。"""
        st = self.group_settings.get(origin or "")
        if not isinstance(st, dict):
            return True
        return bool(st.get(kind, True))

    def _set_group_notify(self, origin: str, kind: str, value: bool):
        st = self.group_settings.setdefault(origin or "", {})
        st[kind] = bool(value)
        self._save_group_settings()

    def _prune_group_settings(self, subs: Dict[str, Dict] = None, dyn_subs: Dict[str, Dict] = None):
        """清掉已无任何订阅（开播或动态）引用的会话设置，避免 groups.json 无限膨胀。

        两套订阅共用同一份 groups.json，所以剪枝必须同时看两边：
        只订了动态没订开播的群、只订了开播没订动态的群，都算"还有引用"。
        传进来的那一边用内存里的最新结构，另一边从 config 现读（保存流程已先写好 config）。
        """
        if subs is None:
            subs = self._load_subs()
        if dyn_subs is None:
            dyn_subs = self._load_dyn_subs()
        referenced = set()
        for data in (subs, dyn_subs):
            referenced.update(
                g.get("umo")
                for info in data.values()
                for g in info.get("groups", [])
                if isinstance(g, dict)
            )
        stale = [k for k in self.group_settings if k not in referenced]
        if stale:
            for k in stale:
                del self.group_settings[k]
            self._save_group_settings()

    # ---------- 管理员 ----------
    def _admin_ids(self) -> List[str]:
        """插件配置的管理员QQ号列表（归一化为字符串）。"""
        raw = self._cfg("admin_ids", []) or []
        return [str(x).strip() for x in raw if str(x).strip()]

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """插件配置的 admin_ids 优先；未配置时退回 AstrBot 全局管理员判定。"""
        ids = self._admin_ids()
        if ids:
            try:
                return str(event.get_sender_id()) in ids
            except Exception:
                return False
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _admin_notify_targets(self) -> List[str]:
        """Cookie 失效等要私聊提醒的管理员目标（unified_msg_origin）。"""
        plat = self.default_platform
        return [f"{plat}:FriendMessage:{qq}" for qq in self._admin_ids()]

    # ---------- 静音时段 ----------
    def _parse_quiet_hours(self):
        """解析静音时段配置 'HH:MM-HH:MM'，返回 (time, time) 或 None。"""
        raw = str(self._cfg("quiet_hours", "") or "").strip()
        if not raw:
            return None
        m = re.match(r"^(\d{1,2}):(\d{2})\s*[-~至到]\s*(\d{1,2}):(\d{2})$", raw)
        if not m:
            return None
        sh, sm, eh, em = (int(x) for x in m.groups())
        if not (0 <= sh < 24 and 0 <= eh <= 24 and 0 <= sm < 60 and 0 <= em < 60):
            return None
        if eh == 24 and em:
            return None
        from datetime import time as dtime
        return dtime(sh, sm), dtime(eh % 24, em)

    def _in_quiet_hours(self) -> bool:
        span = self._parse_quiet_hours()
        if not span:
            return False
        start, end = span
        now = datetime.now().time()
        if start <= end:
            return start <= now < end
        return now >= start or now < end  # 跨零点，如 23:00-08:00

    # ---------- 通知模板 ----------
    def _render_template(self, template: str, **fields) -> str:
        """容错格式化：未知占位符原样保留，花括号不配对时按原文发送。"""
        try:
            return template.format_map(_SafeFormatDict(**fields))
        except Exception as e:
            logger.warning(f"通知模板格式有误（花括号未配对?），按原文发送: {e}")
            return template

    def _validate_templates(self):
        samples = {
            "live_notify_template": {"uname": "x", "title": "x", "room_id": 0},
            "end_notify_template": {"uname": "x", "room_id": 0},
            "dynamic_notify_template": {
                "uname": "x", "action": "x", "title": "x", "text": "x", "url": "x",
            },
        }
        for name, fields in samples.items():
            t = str(self._cfg(name, "") or "")
            if not t:
                continue
            try:
                t.format_map(_SafeFormatDict(**fields))
            except Exception as e:
                logger.warning(f"配置项 {name} 存在未配对的花括号，推送时将按原文发送: {e}")

    # ---------- 运行时状态持久化（直播状态 + 动态基线） ----------
    def _load_state(self):
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cache = data.get("live_status_cache", {})
                if isinstance(cache, dict):
                    self.live_status_cache = {str(k): int(v) for k, v in cache.items()}
                dyn = data.get("dyn_last_ids", {})
                if isinstance(dyn, dict):
                    self.dyn_last_ids = {
                        str(k): str(v) for k, v in dyn.items() if str(v or "").strip()
                    }
                logger.info(
                    f"已加载状态缓存：直播 {len(self.live_status_cache)} 条、"
                    f"动态基线 {len(self.dyn_last_ids)} 条"
                )
        except Exception as e:
            logger.error(f"加载状态缓存失败: {e}")

    def _save_state(self):
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "live_status_cache": self.live_status_cache,
                        "dyn_last_ids": self.dyn_last_ids,
                    },
                    f, ensure_ascii=False, indent=2,
                )
        except Exception as e:
            logger.error(f"保存状态缓存失败: {e}")

    # ---------- 生命周期 ----------
    async def ensure_session(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                connector=aiohttp.TCPConnector(limit=10, limit_per_host=5),
            )
            logger.info("HTTP会话已创建")

    async def initialize(self):
        async with self._init_lock:
            if self._initialized:
                return
            try:
                logger.info("正在初始化B站开播监测插件...")
                await self.ensure_session()
                self._load_state()
                self._load_group_settings()
                self._validate_templates()

                bad_lines = self._invalid_sub_lines()
                if bad_lines:
                    logger.warning(f"订阅配置中有 {len(bad_lines)} 行无法解析（已忽略），示例: {bad_lines[:3]}")

                subs = self._load_subs()
                # 首次运行时，给已订阅的UP播下当前状态，避免把正在直播的误判为"刚开播"
                if subs:
                    await self._seed_status_cache(list(subs.keys()))

                if not self.monitor_task or self.monitor_task.done():
                    self.monitor_task = asyncio.create_task(self.monitor_live_status())
                    logger.info("监控任务已启动")

                # 动态监测不在这里播种：动态接口只能按UP逐个拉，播种会拖慢初始化。
                # monitor_dynamics 首轮"只记基线不补发"，效果一样且不阻塞启动。
                dyn_subs = self._load_dyn_subs()
                bad_dyn = self._invalid_dyn_sub_lines()
                if bad_dyn:
                    logger.warning(
                        f"动态订阅配置中有 {len(bad_dyn)} 行无法解析（已忽略），示例: {bad_dyn[:3]}"
                    )
                if not self.dyn_monitor_task or self.dyn_monitor_task.done():
                    self.dyn_monitor_task = asyncio.create_task(self.monitor_dynamics())
                    logger.info(f"动态监控任务已启动（{len(dyn_subs)} 个UP）")

                self._initialized = True
                total = sum(len(v.get("groups", [])) for v in subs.values())
                dyn_total = sum(len(v.get("groups", [])) for v in dyn_subs.values())
                logger.info(
                    f"B站开播监测插件初始化完成，开播 {len(subs)} 个UP/{total} 条群订阅，"
                    f"动态 {len(dyn_subs)} 个UP/{dyn_total} 条群订阅"
                )
            except Exception as e:
                logger.error(f"插件初始化失败: {e}")
                await self._cleanup_resources()
                raise

    async def _seed_status_cache(self, uids: List[str]):
        """初始化时用当前真实状态填充缓存，防止重启后误报开播。"""
        try:
            status_map = await self.get_live_status_batch(uids)
            for uid in uids:
                if uid not in self.live_status_cache:
                    self.live_status_cache[uid] = status_map.get(uid, {}).get("live_status", 0)
            self._save_state()
        except Exception as e:
            logger.error(f"初始化状态缓存失败: {e}")

    async def _cleanup_resources(self):
        try:
            # 直播与动态两条监控循环都要收干净，否则重载插件会留下僵尸任务
            for attr in ("monitor_task", "dyn_monitor_task"):
                task = getattr(self, attr, None)
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        logger.error(f"取消 {attr} 时出错: {e}")
                    finally:
                        setattr(self, attr, None)
            if self.session and not self.session.closed:
                await self.session.close()
                self.session = None
        except Exception as e:
            logger.error(f"清理资源时出错: {e}")

    async def terminate(self):
        try:
            logger.info("正在停止B站开播监测插件...")
            for task in list(self._group_name_tasks.values()):
                task.cancel()
            self._group_name_tasks.clear()
            try:
                self.groups.flush()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"退出前保存群名缓存失败: {e}")
            self._save_state()
            await self._cleanup_resources()
            logger.info("B站开播监测插件已完全停止")
        except Exception as e:
            logger.error(f"插件销毁时出错: {e}")
            try:
                await self._cleanup_resources()
            except Exception as ce:
                logger.error(f"强制清理资源时出错: {ce}")

    # ---------- Cookie ----------
    def _get_bilibili_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        cookie = self._cfg("bilibili_cookie", "") or ""
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def _save_cookie_to_config(self, cookie: str):
        """登录管理器回调：Cookie 直接写回插件配置（走规范 save_config）。"""
        try:
            self.config["bilibili_cookie"] = cookie
            await self._persist_config()
            logger.info("Cookie已保存到插件配置")
        except Exception as e:
            logger.error(f"保存Cookie到配置失败: {e}")

    # ---------- B站 API ----------
    async def get_live_status(self, uid: str) -> Dict:
        try:
            batch = await self.get_live_status_batch([uid])
            if uid in batch:
                return batch[uid]
        except Exception as e:
            logger.error(f"获取UID {uid} 直播状态失败: {e}")
        return {"live_status": 0, "room_id": 0, "title": "", "uname": "", "cover": ""}

    async def get_live_status_batch(self, uids: List[str]) -> Dict[str, Dict]:
        result_map: Dict[str, Dict] = {}
        if not uids:
            return result_map
        try:
            await self.ensure_session()
            url = "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
            data = {"uids": [int(u) for u in uids]}
            headers = self._get_bilibili_headers()
            timeout = aiohttp.ClientTimeout(total=10)
            async with self.session.post(url, json=data, headers=headers, timeout=timeout) as response:
                if response.status == 200:
                    body = await response.json()
                    if body.get("code") == 0:
                        self._last_rate_limited = False
                        data_obj = body.get("data", {})
                        if isinstance(data_obj, dict):
                            for u in uids:
                                ud = data_obj.get(str(u))
                                if ud:
                                    result_map[str(u)] = self._extract_status(ud)
                        elif isinstance(data_obj, list):
                            by_uid = {}
                            for entry in data_obj:
                                uid_val = str(entry.get("uid") or entry.get("mid") or "")
                                if uid_val:
                                    by_uid[uid_val] = entry
                            for u in uids:
                                entry = by_uid.get(str(u))
                                if entry:
                                    result_map[str(u)] = self._extract_status(entry)
                    else:
                        logger.warning(f"B站API返回错误码: {body.get('code')}, 消息: {body.get('message', '未知错误')}")
                        if body.get("code") == -101:
                            cookie = self._cfg("bilibili_cookie", "") or ""
                            if cookie:
                                asyncio.create_task(
                                    self.login_manager.check_and_notify_cookie_invalid(cookie, "B站API返回-101错误")
                                )
                elif response.status == 429:
                    self._last_rate_limited = True
                    logger.warning("B站API请求频率限制 (429)")
                else:
                    logger.warning(f"B站API请求失败，状态码: {response.status}")
        except Exception as e:
            logger.error(f"批量获取直播状态失败: {e}")
        finally:
            for u in uids:
                result_map.setdefault(str(u), {"live_status": 0, "room_id": 0, "title": "", "uname": "", "cover": ""})
        return result_map

    @staticmethod
    def _extract_status(d: Dict) -> Dict:
        return {
            "live_status": d.get("live_status", 0),
            "room_id": d.get("room_id", 0),
            # B站接口的标题常带 HTML 实体（&quot; 等），转义后更可读
            "title": html.unescape(d.get("title", "") or ""),
            "uname": d.get("uname", ""),
            "cover": d.get("cover_from_user", "") or d.get("cover", ""),
        }

    # ---------- 用户名片（动态订阅时拿昵称：UP主不一定开过直播） ----------
    async def get_user_card(self, uid: str) -> Dict:
        """按 UID 取用户名片，匿名可读。返回 {"mid", "name"}，失败返回 {}。"""
        try:
            await self.ensure_session()
            url = f"https://api.bilibili.com/x/web-interface/card?mid={uid}&photo=false"
            timeout = aiohttp.ClientTimeout(total=10)
            async with self.session.get(url, headers=self._dynamic_headers(uid), timeout=timeout) as resp:
                if resp.status != 200:
                    logger.warning(f"获取用户名片失败 UID {uid}，HTTP {resp.status}")
                    return {}
                body = await resp.json()
                if body.get("code") == 0:
                    card = (body.get("data") or {}).get("card") or {}
                    return {"mid": str(card.get("mid") or ""), "name": str(card.get("name") or "")}
                logger.warning(f"用户名片API返回错误码 UID {uid}: {body.get('code')} {body.get('message')}")
        except Exception as e:
            logger.error(f"获取用户名片异常 UID {uid}: {e}")
        return {}

    # ---------- buvid 访客指纹（动态接口风控必需） ----------
    async def _ensure_buvid(self):
        """近一天内已有 buvid 就直接用，否则向 spi 接口换一个。

        不带 buvid 硬打动态接口会吃到风控（-352/412），这是绕过去的关键。
        拿不到也不致命：请求会退化成匿名尝试。
        """
        TTL = 24 * 3600
        if self._buvid.get("b3") and time.time() - self._buvid.get("fetched_at", 0) < TTL:
            return
        try:
            await self.ensure_session()
            headers = self._get_bilibili_headers()
            headers["Referer"] = "https://www.bilibili.com"
            timeout = aiohttp.ClientTimeout(total=10)
            async with self.session.get(
                "https://api.bilibili.com/x/frontend/finger/spi",
                headers=headers, timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"获取访客指纹失败，HTTP {resp.status}")
                    return
                body = await resp.json()
                if body.get("code") == 0:
                    data = body.get("data") or {}
                    self._buvid = {
                        "b3": str(data.get("b_3") or ""),
                        "b4": str(data.get("b_4") or ""),
                        "fetched_at": time.time(),
                    }
                    logger.info("buvid 访客指纹已刷新")
                else:
                    logger.warning(f"指纹API返回错误码: {body.get('code')} {body.get('message')}")
        except Exception as e:
            logger.error(f"获取访客指纹失败: {e}")

    def _dynamic_cookie(self) -> str:
        """拼动态接口用的 Cookie：buvid 指纹 + 用户配置的登录 Cookie（若有）。"""
        parts: List[str] = []
        b3, b4 = self._buvid.get("b3", ""), self._buvid.get("b4", "")
        if b3:
            parts.append(f"buvid3={b3}")
            parts.append(f"buvid_fp={b3}")
            parts.append(f"b_nut={int(self._buvid.get('fetched_at') or time.time())}")
        if b4:
            parts.append(f"buvid4={b4}")
        user_cookie = (self._cfg("bilibili_cookie", "") or "").strip()
        if user_cookie:
            parts.append(user_cookie)
        return "; ".join(parts)

    def _dynamic_headers(self, uid: str) -> Dict[str, str]:
        """空间域接口（名片/动态）要带空间域 Referer+Origin，否则容易被风控。"""
        headers = self._get_bilibili_headers()
        headers["Referer"] = f"https://space.bilibili.com/{uid}"
        headers["Origin"] = "https://space.bilibili.com"
        headers["Cookie"] = self._dynamic_cookie()
        return headers

    # ---------- WBI 签名素材（polymer 动态接口要求带签名） ----------
    async def _ensure_wbi_keys(self, force: bool = False) -> bool:
        """从 nav 接口拿 img_key/sub_key，近12小时缓存；被风控时 force 重取。"""
        TTL = 12 * 3600
        if not force and self._wbi_keys.get("img_key") and time.time() - self._wbi_keys.get("fetched_at", 0) < TTL:
            return True
        try:
            await self.ensure_session()
            await self._ensure_buvid()
            headers = self._get_bilibili_headers()
            headers["Referer"] = "https://www.bilibili.com"
            headers["Cookie"] = self._dynamic_cookie()
            timeout = aiohttp.ClientTimeout(total=10)
            async with self.session.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers=headers, timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"获取WBI密钥失败，HTTP {resp.status}")
                    return False
                body = await resp.json()
                wbi_img = ((body.get("data") or {}).get("wbi_img") or {})
                img_url = str(wbi_img.get("img_url") or "")
                sub_url = str(wbi_img.get("sub_url") or "")
                if not img_url or not sub_url:
                    logger.warning(f"WBI密钥响应缺少img/sub链接: code={body.get('code')}")
                    return False
                # 链接的文件名去掉扩展名就是 key：.../7cd084941338484aae1ad9425b84077c.png
                img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
                sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
                self._wbi_keys = {"img_key": img_key, "sub_key": sub_key, "fetched_at": time.time()}
                logger.info("WBI 签名密钥已刷新")
                return True
        except Exception as e:
            logger.error(f"获取WBI密钥异常: {e}")
            return False

    # ---------- 动态拉取（polymer feed/space：需 WBI 签名 + buvid 指纹） ----------
    DYNAMIC_FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"

    async def get_user_dynamics(self, uid: str, page_size: int = 12) -> List[Dict]:
        """拉某个 UID 的空间动态（最新在前）。失败/风控返回 []，由调用方按错误次数退避。"""
        await self.ensure_session()
        if not await self._ensure_wbi_keys():
            return []

        base_params = {
            "host_mid": str(uid),
            "timezone_offset": -480,
            "features": "itemOpusStyle",
            "offset": "",
        }
        headers = self._dynamic_headers(uid)
        timeout = aiohttp.ClientTimeout(total=10)

        # 第一次尝试；若签名被判风控（-352/-403）则强制重取密钥再来一次
        for attempt in range(2):
            params = wbi.encode_wbi(base_params, self._wbi_keys["img_key"], self._wbi_keys["sub_key"])
            params["offset"] = ""
            try:
                full_url = f"{self.DYNAMIC_FEED_URL}?{urlencode(params)}"
                async with self.session.get(full_url, headers=headers, timeout=timeout) as resp:
                    if resp.status == 412:
                        # 风控，多半是 IP 问题，退避即可
                        self._dyn_last_rate_limited = True
                        logger.warning(f"动态接口被风控 HTTP 412 (UID {uid})")
                        return []
                    if resp.status != 200:
                        logger.warning(f"动态接口 HTTP {resp.status} (UID {uid})")
                        return []
                    body = await resp.json()
                    code = body.get("code")
                    if code == 0:
                        self._dyn_last_rate_limited = False
                        items = (body.get("data") or {}).get("items") or []
                        return items[:page_size]
                    if code in (-352, -403) and attempt == 0:
                        logger.info(f"动态接口风控(code={code}，签名校验失败)，强制刷新WBI密钥后重试")
                        await self._ensure_wbi_keys(force=True)
                        continue
                    if code == -636:
                        # -636：feed/space 不接受匿名调用，需要登录 Cookie
                        has_cookie = bool((self._cfg("bilibili_cookie", "") or "").strip())
                        hint = "cookie 可能已失效" if has_cookie else "未配置 bilibili_cookie"
                        logger.warning(f"动态接口需要登录(code=-636，{hint})")
                        return []
                    logger.warning(f"动态接口错误码 {code} (UID {uid}): {body.get('message')}")
                    return []
            except Exception as e:
                logger.error(f"拉取动态异常 (UID {uid}，第{attempt + 1}次): {e}")
                return []
        return []

    # ---------- 监控循环 ----------
    async def monitor_live_status(self):
        consecutive_errors = 0
        max_consecutive_errors = 5

        while True:
            try:
                # 静音时段：暂停检查与推送，期间的状态变化靠缓存差异在结束后补推
                if self._in_quiet_hours():
                    await asyncio.sleep(min(60, self.current_interval))
                    continue

                subs = self._load_subs()
                if not subs:
                    await asyncio.sleep(self.check_interval)
                    continue

                all_uid_list = sub.all_uids(subs)
                now = asyncio.get_running_loop().time()
                uids_to_check = [u for u in all_uid_list if self.uid_skip_until.get(u, 0) <= now]
                if not uids_to_check:
                    await asyncio.sleep(self.current_interval)
                    continue

                status_map = await self.get_live_status_batch(uids_to_check)

                # 逐 UP 判断状态变化，命中就推给它订阅的所有群，最后统一更新缓存
                for uid in uids_to_check:
                    current = status_map.get(uid, {"live_status": 0})
                    cur_status = current.get("live_status", 0)
                    prev_status = self.live_status_cache.get(uid, 0)

                    info = subs.get(uid, {})
                    groups = info.get("groups", [])

                    if cur_status == 1 and prev_status != 1:
                        for g in groups:
                            await self.send_live_notification(current, g["umo"], g.get("at_all", False))
                    elif prev_status == 1 and cur_status != 1:
                        for g in groups:
                            await self.send_end_notification(current, g["umo"], False)

                    self.live_status_cache[uid] = cur_status

                    # 错误统计与退避（uname 空且 room_id 为 0 视为查询失败）
                    is_empty = (not current.get("uname")) and current.get("room_id", 0) == 0
                    if is_empty:
                        cnt = self.uid_error_counts.get(uid, 0) + 1
                        self.uid_error_counts[uid] = cnt
                        self.uid_skip_until[uid] = now + min(300, 30 * cnt)
                    else:
                        self.uid_error_counts.pop(uid, None)
                        self.uid_skip_until.pop(uid, None)

                self._save_state()
                consecutive_errors = 0

                await asyncio.sleep(self.current_interval)
                if self._last_rate_limited:
                    self.current_interval = min(300, max(self.check_interval, int(self.current_interval * 2)))
                else:
                    self.current_interval = max(self.check_interval, int(self.current_interval * 0.75))

            except asyncio.CancelledError:
                logger.info("监控任务被取消")
                break
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"监控任务出错 (第{consecutive_errors}次): {e}")
                if consecutive_errors >= max_consecutive_errors:
                    wait_time = min(300, 60 * consecutive_errors)
                    logger.warning(f"连续错误{consecutive_errors}次，等待{wait_time}秒后重试")
                    await asyncio.sleep(wait_time)
                else:
                    await asyncio.sleep(self.current_interval)

    # ---------- 动态监控循环 ----------
    async def monitor_dynamics(self):
        """动态轮询：每个订阅UP按节奏逐个拉，有新动态就推。

        节流思路与直播监控一致（自适应间隔 + 单UP退避），但动态接口风控更敏感：
        单UP之间额外留 2 秒间隔，被限流时整机间隔翻倍（最多 900 秒）。
        """
        consecutive_errors = 0
        max_consecutive_errors = 5

        while True:
            try:
                # 静音时段：与直播监控保持一致，暂停检查与推送
                if self._in_quiet_hours():
                    await asyncio.sleep(min(60, self.dyn_current_interval))
                    continue

                subs = self._load_dyn_subs()
                if not subs:
                    await asyncio.sleep(self.dynamic_check_interval)
                    continue

                now = asyncio.get_running_loop().time()
                uids_to_check = [u for u in sub.all_uids(subs) if self.dyn_skip_until.get(u, 0) <= now]
                if not uids_to_check:
                    await asyncio.sleep(self.dyn_current_interval)
                    continue

                toggles = self.dynamic_notify_toggles
                for uid in uids_to_check:
                    items = await self.get_user_dynamics(uid)
                    if not items:
                        # 查询失败/风控：累计错误并按次退避，别把接口打爆
                        cnt = self.dyn_error_counts.get(uid, 0) + 1
                        self.dyn_error_counts[uid] = cnt
                        self.dyn_skip_until[uid] = now + min(1200, 60 * cnt)
                        continue
                    self.dyn_error_counts.pop(uid, None)
                    self.dyn_skip_until.pop(uid, None)

                    groups = (subs.get(uid) or {}).get("groups", []) or []
                    await self._dispatch_dynamics(uid, items, groups, toggles)

                    # 单UP之间留节奏：动态接口比直播状态接口敏感得多
                    await asyncio.sleep(2.0)

                self._save_state()
                consecutive_errors = 0

                await asyncio.sleep(self.dyn_current_interval)
                if self._dyn_last_rate_limited:
                    self.dyn_current_interval = min(
                        900, max(float(self.dynamic_check_interval), self.dyn_current_interval * 2))
                else:
                    self.dyn_current_interval = max(
                        float(self.dynamic_check_interval), self.dyn_current_interval * 0.9)

            except asyncio.CancelledError:
                logger.info("动态监控任务被取消")
                break
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"动态监控任务出错 (第{consecutive_errors}次): {e}")
                if consecutive_errors >= max_consecutive_errors:
                    wait_time = min(900, 120 * consecutive_errors)
                    logger.warning(f"动态监控连续错误{consecutive_errors}次，等待{wait_time}秒后重试")
                    await asyncio.sleep(wait_time)
                else:
                    await asyncio.sleep(self.dyn_current_interval)

    async def _dispatch_dynamics(self, uid: str, items: List[Dict],
                                 groups: List[Dict], toggles: Dict[str, bool]):
        """比较动态 id 游标，把真正的新动态推给该UP的每一个订阅群（groups 为 [{umo, at_all}]）。"""
        if not items:
            return
        last_id = self.dyn_last_ids.get(uid)

        # 还没有基线（首次运行、或刚在WebUI里加了动态订阅）：先记基线不补发，
        # 否则会把空间里的存量动态当成"新动态"一口气刷出去
        if not str(last_id or "").strip():
            baseline = dynamic_report.latest_id(items)
            if baseline:
                self.dyn_last_ids[uid] = baseline
                logger.info(f"UID {uid} 首次见到动态，建立基线 {baseline}（不补发存量）")
            return

        # 挑选/排序/单轮封顶都在纯函数里做（id 必须按数值比，见 dynamic_report）
        for it in dynamic_report.select_new_dynamics(items, last_id):
            parsed = dynamic_report.extract_dynamic(it)
            if not self._should_notify_dynamic(parsed, toggles):
                continue
            for g in groups:
                await self.send_dynamic_notification(parsed, g.get("umo", ""), g.get("at_all", False))

        # 游标只往前走不回退：接口可能把置顶/旧动态排在列表前面
        latest = dynamic_report.latest_id(items)
        if dynamic_report.is_newer_id(latest, last_id):
            self.dyn_last_ids[uid] = latest

    def _should_notify_dynamic(self, parsed: Dict, toggles: Dict[str, bool]) -> bool:
        return dynamic_report.should_notify(parsed.get("kind", "other"), toggles)

    # ---------- 推送目标路由（send_message 按平台实例 id 匹配）----------
    def _route_umo(self, umo: str) -> str:
        """把目标 umo 的平台段从适配器类型名换成实例 id。

        send_message 按平台**实例 id** 精确匹配：配置里若写的是适配器类型名
        （aiocqhttp / qq_official）而实例 id 是别的（napcat / 自定义），不改写就会
        静默丢消息。已是实例 id、裸群号、或认不出来的都原样返回。
        """
        s = str(umo or "").strip()
        if ":" not in s:
            return s
        parts = s.split(":")
        plat = parts[0].strip()
        insts = self._platform_instances()
        if not plat or any(plat == pid for pid, _ in insts):
            return s
        for pid, ptype in insts:
            if ptype == plat:
                parts[0] = pid
                return ":".join(parts)
        return s

    def _target_is_official(self, umo: str) -> bool:
        """目标是否落在官方 QQ 机器人（qq_official*）上——那边不支持 @全体成员。"""
        plat = str(umo or "").split(":", 1)[0].strip()
        for pid, ptype in self._platform_instances():
            if plat in (pid, ptype):
                return "qq_official" in (ptype or "")
        return "qq_official" in plat

    # ---------- 通知 ----------
    def _build_message_chain(self, template: str, uname: str, title: str, room_id, cover: str,
                             at_all: bool = False) -> MessageChain:
        text = self._render_template(template, uname=uname, title=title, room_id=room_id)
        chain = MessageChain()
        if at_all:
            chain.at_all()
        chain.message(text)
        if cover and self._cfg_bool("send_cover", True):
            chain.url_image(cover)
        return chain

    async def send_live_notification(self, status_info: Dict, origin: str, at_all: bool):
        try:
            if not self.enable_notifications:
                return
            if not self._group_notify_enabled(origin, "notify"):
                return
            uname = status_info.get("uname", "未知UP主")
            title = status_info.get("title", "无标题")
            room_id = status_info.get("room_id", 0)
            cover = status_info.get("cover", "")
            template = self._cfg("live_notify_template",
                                 "🔴 {uname} 开播啦！\n📺 直播标题: {title}\n🔗 直播间: https://live.bilibili.com/{room_id}")
            target = self._route_umo(origin)
            chain = self._build_message_chain(
                template, uname, title, room_id, cover,
                at_all and not self._target_is_official(target),
            )
            await self.context.send_message(target, chain)
            logger.info(f"开播通知已发送: {uname} -> {target}")
        except Exception as e:
            logger.error(f"发送开播通知失败: {e}")

    async def send_end_notification(self, status_info: Dict, origin: str, at_all: bool):
        try:
            if not self.enable_notifications or not self.enable_end_notifications:
                return
            # notify 是本群总开关（/关闭通知 = 一关全关，含关播）；关播还要再过 notify_end
            if not self._group_notify_enabled(origin, "notify"):
                return
            if not self._group_notify_enabled(origin, "notify_end"):
                return
            uname = status_info.get("uname", "未知UP主")
            room_id = status_info.get("room_id", 0)
            cover = status_info.get("cover", "")
            template = self._cfg("end_notify_template", "⚫ {uname} 已结束直播")
            target = self._route_umo(origin)
            chain = self._build_message_chain(template, uname, "", room_id, cover, False)
            await self.context.send_message(target, chain)
            logger.info(f"关播通知已发送: {uname} -> {target}")
        except Exception as e:
            logger.error(f"发送关播通知失败: {e}")

    # ---------- 动态通知 ----------
    def _build_dynamic_chain(self, template: str, parsed: Dict, at_all: bool) -> MessageChain:
        """按模板拼动态通知（未知占位符原样保留，模板写错也照发不炸）。"""
        text = self._render_template(
            template,
            uname=parsed.get("uname", "未知UP主"),
            action=parsed.get("action", "发布了新动态"),
            title=parsed.get("title", ""),
            text=parsed.get("text", ""),
            url=parsed.get("url", ""),
        )
        chain = MessageChain()
        if at_all:
            chain.at_all()
        chain.message(text)
        # 动态的图就是内容本身（图文/相簿），因此不受 send_cover（那项只管直播封面）影响
        for img in (parsed.get("images") or [])[:dynamic_report.MAX_IMAGES]:
            if img:
                chain.url_image(img)
        return chain

    async def send_dynamic_notification(self, parsed: Dict, origin: str, at_all: bool):
        """把一条动态推到某个会话。总开关、本会话开关、类型开关分别在派发处与这里把关。"""
        try:
            if not self.enable_notifications or not origin:
                return
            # notify 是本会话总开关（/关闭通知 一关全关），notify_dyn 只收动态
            if not self._group_notify_enabled(origin, "notify"):
                return
            if not self._group_notify_enabled(origin, "notify_dyn"):
                return
            uname = parsed.get("uname", "未知UP主")
            template = self._cfg("dynamic_notify_template",
                                 "📢 {uname} {action}\n{title}\n🔗 {url}")
            target = self._route_umo(origin)
            # @全体只在视频/直播卡片两类生效（图文、专栏也@全体就成刷屏了）；
            # 官方 QQ 机器人不支持 @全体，命中就别加，免得整条推送发失败。
            want_at_all = (
                at_all
                and parsed.get("kind") in ("video", "live")
                and not self._target_is_official(target)
            )
            chain = self._build_dynamic_chain(template, parsed, want_at_all)
            await self.context.send_message(target, chain)
            logger.info(f"动态通知已发送: {uname}({parsed.get('kind')}) -> {target}")
        except Exception as e:
            logger.error(f"发送动态通知失败: {e}")

    # ---------- 命令 ----------
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def handle_private_message(self, event: AstrMessageEvent):
        """私聊消息入口：处理管理员扫码登录命令（官方 event_message_type 过滤器）。"""
        if await self.login_manager.handle_admin_command(event):
            event.stop_event()  # 登录关键词已消费，不再交给后续处理器/LLM

    @filter.command("订阅")
    async def subscribe(self, event: AstrMessageEvent):
        """/订阅 <UID> [at_all] —— 把当前群加入该UP的推送目标。"""
        try:
            args = event.message_str.strip().split()
            if len(args) < 2:
                yield event.plain_result("❌ 用法: /订阅 <UID> [at_all]\n例: /订阅 111111111\n加 at_all 表示开播时@全体成员")
                return
            uid = args[1]
            if not uid.isdigit():
                yield event.plain_result("❌ UID必须是数字")
                return

            origin = event.unified_msg_origin
            subs = self._load_subs()

            # 数量限制（按UP总数）
            if uid not in subs and len(subs) >= self.max_monitors:
                yield event.plain_result(f"❌ 监控UP数量已达上限({self.max_monitors})")
                return

            # 已在当前群订阅？
            if uid in subs and any(g.get("umo") == origin for g in subs[uid].get("groups", [])):
                yield event.plain_result(f"❌ 本群已订阅UID {uid}，请勿重复添加")
                return

            status_info = await self.get_live_status(uid)
            if not status_info.get("uname"):
                yield event.plain_result(f"❌ 未找到UID为 {uid} 的UP主")
                return

            at_all = "at_all" in args
            sub.add_subscription(subs, uid, origin, at_all)
            await self._save_subs(subs)
            # 播下当前状态，避免正在直播的被误判"刚开播"
            if uid not in self.live_status_cache:
                self.live_status_cache[uid] = status_info.get("live_status", 0)
                self._save_state()

            uname = status_info.get("uname", "未知UP主")
            tip = "（开播@全体成员）" if at_all else ""
            yield event.plain_result(f"✅ 已在本群订阅 {uname}(UID:{uid}){tip}")
        except Exception as e:
            logger.error(f"订阅失败: {e}")
            yield event.plain_result("❌ 订阅失败，请稍后重试")

    @filter.command("退订")
    async def unsubscribe(self, event: AstrMessageEvent):
        """/退订 <UID或序号> —— 从当前群移除某UP。序号来自 /订阅列表。"""
        try:
            args = event.message_str.strip().split()
            if len(args) < 2:
                yield event.plain_result("❌ 用法: /退订 <UID或序号>\n先用 /订阅列表 查看序号")
                return

            origin = event.unified_msg_origin
            subs = self._load_subs()
            group_uids = sub.subscriptions_for_group(subs, origin)

            token = args[1]
            uid = None
            # 支持按序号删（1 起）
            if token.isdigit() and 1 <= int(token) <= len(group_uids) and token not in group_uids:
                uid = group_uids[int(token) - 1]
            elif token in group_uids:
                uid = token
            elif token.isdigit() and token in subs and any(
                    g.get("umo") == origin for g in subs[token].get("groups", [])):
                uid = token

            if not uid:
                yield event.plain_result(f"❌ 本群未订阅 {token}（可用 /订阅列表 查看序号）")
                return

            uname = subs.get(uid, {}).get("uname", "")
            sub.remove_subscription(subs, uid, origin)
            await self._save_subs(subs)
            label = f"{uname}(UID:{uid})" if uname else f"UID {uid}"
            yield event.plain_result(f"✅ 已从本群退订 {label}")
        except Exception as e:
            logger.error(f"退订失败: {e}")
            yield event.plain_result("❌ 退订失败，请稍后重试")

    @filter.command("订阅列表")
    async def list_subscriptions(self, event: AstrMessageEvent):
        """列出当前群订阅的UP（带序号，供 /退订 用）。"""
        try:
            origin = event.unified_msg_origin
            subs = self._load_subs()
            group_uids = sub.subscriptions_for_group(subs, origin)
            if not group_uids:
                yield event.plain_result("📝 本群没有订阅任何UP主\n用 /订阅 <UID> 添加")
                return

            status_map = await self.get_live_status_batch(group_uids)
            message = "📝 本群订阅列表:\n"
            for i, uid in enumerate(group_uids, 1):
                st = status_map.get(uid, {})
                uname = st.get("uname") or "未知UP主"
                live = "🔴 直播中" if st.get("live_status") == 1 else "⚫ 未开播"
                g_entry = next(
                    (g for g in subs.get(uid, {}).get("groups", []) if g.get("umo") == origin), {})
                at_tip = " 📢@all" if g_entry.get("at_all") else ""
                message += f"{i}. {uname}(UID:{uid}) - {live}{at_tip}\n"
            message += "\n退订用 /退订 <序号>"
            yield event.plain_result(message.strip())
        except Exception as e:
            logger.error(f"获取订阅列表失败: {e}")
            yield event.plain_result("❌ 获取订阅列表失败，请稍后重试")

    @filter.command("检查直播")
    async def check_live(self, event: AstrMessageEvent):
        """/检查直播 <UID> —— 手动查一次状态。"""
        try:
            args = event.message_str.strip().split()
            if len(args) < 2:
                yield event.plain_result("❌ 用法: /检查直播 <UID>")
                return
            uid = args[1]
            if not uid.isdigit():
                yield event.plain_result("❌ UID必须是数字")
                return

            status_info = await self.get_live_status(uid)
            if not status_info.get("uname"):
                yield event.plain_result(f"❌ 未找到UID为 {uid} 的UP主")
                return

            uname = status_info.get("uname", "未知UP主")
            if status_info.get("live_status") == 1:
                title = status_info.get("title", "无标题")
                room_id = status_info.get("room_id", 0)
                cover = status_info.get("cover", "")
                message = f"🔴 {uname} 正在直播\n📺 {title}\n🔗 https://live.bilibili.com/{room_id}"
                if cover:
                    yield event.make_result().message(message).url_image(cover)
                    return
            else:
                message = f"⚫ {uname} 当前未开播"
            yield event.plain_result(message)
        except Exception as e:
            logger.error(f"检查直播状态失败: {e}")
            yield event.plain_result("❌ 检查直播状态失败，请稍后重试")

    # ---------- 动态订阅命令 ----------
    @filter.command("动态订阅")
    async def dyn_subscribe(self, event: AstrMessageEvent):
        """/动态订阅 <UID> [at_all] —— 把本群加入该UP的动态推送目标。"""
        try:
            args = event.message_str.strip().split()
            if len(args) < 2:
                yield event.plain_result(
                    "❌ 用法: /动态订阅 <UID> [at_all]\n例: /动态订阅 111111111\n"
                    "加 at_all 表示视频/直播类动态@全体成员"
                )
                return
            uid = args[1]
            if not uid.isdigit():
                yield event.plain_result("❌ UID必须是数字")
                return

            origin = event.unified_msg_origin
            dyn_subs = self._load_dyn_subs()
            if uid not in dyn_subs and len(dyn_subs) >= self.max_monitors:
                yield event.plain_result(f"❌ 动态监控UP数量已达上限({self.max_monitors})")
                return
            if any(g.get("umo") == origin for g in (dyn_subs.get(uid) or {}).get("groups", [])):
                yield event.plain_result(f"❌ 本群已订阅UID {uid} 的动态，请勿重复添加")
                return

            card = await self.get_user_card(uid)
            if not card.get("name"):
                yield event.plain_result(f"❌ 未找到UID为 {uid} 的UP主")
                return

            at_all = "at_all" in args
            sub.add_subscription(dyn_subs, uid, origin, at_all)
            await self._save_dyn_subs(dyn_subs)

            # 记基线：把当前最新动态id存下，避免刚订阅就把空间里的存量动态补发出来
            baseline = dynamic_report.latest_id(await self.get_user_dynamics(uid))
            if baseline:
                self.dyn_last_ids[uid] = baseline
                self._save_state()

            tip = "（视频/直播类动态@全体成员）" if at_all else ""
            yield event.plain_result(
                f"✅ 已在本群订阅 {card['name']}(UID:{uid}) 的动态{tip}\n"
                "支持视频/图文/专栏/转发/音频"
            )
        except Exception as e:
            logger.error(f"动态订阅失败: {e}")
            yield event.plain_result("❌ 动态订阅失败，请稍后重试")

    @filter.command("退订动态")
    async def dyn_unsubscribe(self, event: AstrMessageEvent):
        """/退订动态 <UID或序号> —— 从本群移除该UP的动态订阅。序号来自 /动态列表。"""
        try:
            args = event.message_str.strip().split()
            if len(args) < 2:
                yield event.plain_result("❌ 用法: /退订动态 <UID或序号>\n先用 /动态列表 查看序号")
                return

            origin = event.unified_msg_origin
            dyn_subs = self._load_dyn_subs()
            group_uids = sub.subscriptions_for_group(dyn_subs, origin)

            token = args[1]
            uid = None
            # 跟 /退订 一样支持按序号删（1 起）
            if token.isdigit() and 1 <= int(token) <= len(group_uids) and token not in group_uids:
                uid = group_uids[int(token) - 1]
            elif token in group_uids:
                uid = token
            elif token.isdigit() and token in dyn_subs and any(
                    g.get("umo") == origin for g in dyn_subs[token].get("groups", [])):
                uid = token

            if not uid:
                yield event.plain_result(f"❌ 本群未订阅动态 {token}（可用 /动态列表 查看序号）")
                return

            uname = (await self.get_user_card(uid)).get("name", "")
            sub.remove_subscription(dyn_subs, uid, origin)
            await self._save_dyn_subs(dyn_subs)
            label = f"{uname}(UID:{uid})" if uname else f"UID {uid}"
            yield event.plain_result(f"✅ 已从本群退订动态 {label}")
        except Exception as e:
            logger.error(f"退订动态失败: {e}")
            yield event.plain_result("❌ 退订动态失败，请稍后重试")

    @filter.command("动态列表")
    async def dyn_list(self, event: AstrMessageEvent):
        """列出本群订阅的动态UP（带序号，供 /退订动态 用）。"""
        try:
            origin = event.unified_msg_origin
            dyn_subs = self._load_dyn_subs()
            group_uids = sub.subscriptions_for_group(dyn_subs, origin)
            if not group_uids:
                yield event.plain_result("📝 本群没有订阅任何动态\n用 /动态订阅 <UID> 添加")
                return

            message = "📝 本群动态订阅列表:\n"
            for i, uid in enumerate(group_uids, 1):
                card = await self.get_user_card(uid)
                uname = card.get("name") or "未知UP主"
                g_entry = next(
                    (g for g in dyn_subs.get(uid, {}).get("groups", []) if g.get("umo") == origin), {})
                at_tip = " 📢@all" if g_entry.get("at_all") else ""
                message += f"{i}. {uname}(UID:{uid}){at_tip}\n"
            message += "\n退订用 /退订动态 <序号>"
            yield event.plain_result(message.strip())
        except Exception as e:
            logger.error(f"获取动态列表失败: {e}")
            yield event.plain_result("❌ 获取动态列表失败，请稍后重试")

    @filter.command("检查动态")
    async def check_dynamic(self, event: AstrMessageEvent):
        """/检查动态 <UID> —— 手动拉一次该UP最新动态并展示前3条（用来验证动态接口通不通）。"""
        try:
            args = event.message_str.strip().split()
            if len(args) < 2:
                yield event.plain_result("❌ 用法: /检查动态 <UID>")
                return
            uid = args[1]
            if not uid.isdigit():
                yield event.plain_result("❌ UID必须是数字")
                return

            uname = (await self.get_user_card(uid)).get("name") or "未知UP主"
            items = await self.get_user_dynamics(uid)
            if not items:
                yield event.plain_result(
                    f"⚫ 没拿到 {uname} 的动态（接口风控，或该UP没有公开动态；"
                    "动态接口建议配 bilibili_cookie）"
                )
                return
            message = f"📰 {uname} 最近动态（最多3条）:"
            for it in items[:3]:
                p = dynamic_report.extract_dynamic(it)
                title = p.get("title") or (p.get("text") or "")[:40]
                message += f"\n• [{p['kind']}] {p['action']} - {title}\n  🔗 {p['url']}"
            yield event.plain_result(message)
        except Exception as e:
            logger.error(f"检查动态失败: {e}")
            yield event.plain_result("❌ 检查动态失败，请稍后重试")

    @filter.command("开播监测状态")
    async def plugin_status(self, event: AstrMessageEvent):
        """查看插件运行状态与生效配置。"""
        try:
            origin = event.unified_msg_origin
            subs = self._load_subs()
            total_groups = sum(len(v.get("groups", [])) for v in subs.values())
            interval_tip = ""
            if int(self.current_interval) != self.check_interval:
                interval_tip = f"（限流退避中，当前 {int(self.current_interval)} 秒）"
            quiet_raw = str(self._cfg("quiet_hours", "") or "").strip()

            message = "🔧 B站开播监测状态:\n"
            message += f"• HTTP会话: {'✅ 正常' if (self.session and not self.session.closed) else '❌ 异常'}\n"
            message += f"• 监控任务: {'✅ 运行中' if (self.monitor_task and not self.monitor_task.done()) else '❌ 已停止'}\n"
            message += f"• 全局通知: 开播 {'✅' if self.enable_notifications else '❌'} / 关播 {'✅' if self.enable_end_notifications else '❌'}\n"
            message += (f"• 本会话通知: 开播 {'✅' if self._group_notify_enabled(origin, 'notify') else '❌'}"
                        f" / 关播 {'✅' if self._group_notify_enabled(origin, 'notify_end') else '❌'}"
                        f" / 动态 {'✅' if self._group_notify_enabled(origin, 'notify_dyn') else '❌'}\n")
            configured_dp = str(self._cfg("default_platform", "") or "").strip()
            message += f"• 默认平台: {self.default_platform}（{'配置指定' if configured_dp else '自动探测'}）\n"
            message += f"• 检查间隔: {self.check_interval} 秒{interval_tip}\n"
            if quiet_raw:
                if self._parse_quiet_hours() is None:
                    message += f"• 静音时段: {quiet_raw}（⚠️ 格式无效，应为 HH:MM-HH:MM，已忽略）\n"
                else:
                    state = "💤 静音中" if self._in_quiet_hours() else "非静音时段"
                    message += f"• 静音时段: {quiet_raw}（{state}）\n"
            message += f"• 监控UP: {len(subs)}/{self.max_monitors}，群订阅 {total_groups} 条\n"
            bad_lines = self._invalid_sub_lines()
            if bad_lines:
                preview = "；".join(ln[:20] for ln in bad_lines[:3])
                message += f"• ⚠️ 有 {len(bad_lines)} 行订阅配置无法解析（已忽略）: {preview}\n"
            dyn_subs = self._load_dyn_subs()
            dyn_total_groups = sum(len(v.get("groups", [])) for v in dyn_subs.values())
            message += f"• 动态监控UP: {len(dyn_subs)}/{self.max_monitors}，群订阅 {dyn_total_groups} 条\n"
            message += (f"• 动态检查间隔: {self.dynamic_check_interval} 秒"
                        f"（当前 {int(self.dyn_current_interval)} 秒）\n")
            message += f"• 动态基线缓存: {len(self.dyn_last_ids)} 个UP\n"
            bad_dyn = self._invalid_dyn_sub_lines()
            if bad_dyn:
                preview = "；".join(ln[:20] for ln in bad_dyn[:3])
                message += f"• ⚠️ 有 {len(bad_dyn)} 行动态订阅无法解析（已忽略）: {preview}\n"
            if not (self._cfg("bilibili_cookie", "") or "").strip() and dyn_subs:
                message += "• ⚠️ 未配 bilibili_cookie，动态接口可能只返回部分内容(code=-636)\n"
            message += f"• 数据目录: {self.data_dir}"
            yield event.plain_result(message)
        except Exception as e:
            logger.error(f"获取插件状态失败: {e}")
            yield event.plain_result("❌ 获取插件状态失败")

    # ---------- 通知开关（按群生效，仅管理员） ----------
    _ADMIN_TIP = (
        "❌ 仅管理员可以开关通知。\n"
        "请在插件配置 admin_ids 里填管理员QQ号，"
        "或把使用者设为 AstrBot 全局管理员。"
    )

    @filter.command("开启通知")
    async def enable_notify_cmd(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(self._ADMIN_TIP)
            return
        try:
            self._set_group_notify(event.unified_msg_origin, "notify", True)
            yield event.plain_result("✅ 已开启本群的开播/关播通知")
        except Exception as e:
            logger.error(f"开启通知失败: {e}")
            yield event.plain_result("❌ 开启通知失败")

    @filter.command("关闭通知")
    async def disable_notify_cmd(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(self._ADMIN_TIP)
            return
        try:
            self._set_group_notify(event.unified_msg_origin, "notify", False)
            yield event.plain_result("✅ 已关闭本群的所有通知（其他群不受影响）")
        except Exception as e:
            logger.error(f"关闭通知失败: {e}")
            yield event.plain_result("❌ 关闭通知失败")

    @filter.command("开启关播通知")
    async def enable_end_notify_cmd(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(self._ADMIN_TIP)
            return
        try:
            self._set_group_notify(event.unified_msg_origin, "notify_end", True)
            yield event.plain_result("✅ 已开启本群的关播通知")
        except Exception as e:
            logger.error(f"开启关播通知失败: {e}")
            yield event.plain_result("❌ 开启关播通知失败")

    @filter.command("关闭关播通知")
    async def disable_end_notify_cmd(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(self._ADMIN_TIP)
            return
        try:
            self._set_group_notify(event.unified_msg_origin, "notify_end", False)
            yield event.plain_result("✅ 已关闭本群的关播通知（其他群不受影响）")
        except Exception as e:
            logger.error(f"关闭关播通知失败: {e}")
            yield event.plain_result("❌ 关闭关播通知失败")

    @filter.command("开启动态通知")
    async def enable_dyn_notify_cmd(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(self._ADMIN_TIP)
            return
        try:
            self._set_group_notify(event.unified_msg_origin, "notify_dyn", True)
            yield event.plain_result("✅ 已开启本群的动态推送")
        except Exception as e:
            logger.error(f"开启动态通知失败: {e}")
            yield event.plain_result("❌ 开启动态通知失败")

    @filter.command("关闭动态通知")
    async def disable_dyn_notify_cmd(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(self._ADMIN_TIP)
            return
        try:
            self._set_group_notify(event.unified_msg_origin, "notify_dyn", False)
            yield event.plain_result("✅ 已关闭本群的动态推送（开播/关播通知不受影响）")
        except Exception as e:
            logger.error(f"关闭动态通知失败: {e}")
            yield event.plain_result("❌ 关闭动态通知失败")
