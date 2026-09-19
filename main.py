import asyncio
import aiohttp
import html
import json
import os
import re
from datetime import datetime
from typing import Dict, List, Optional
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from .bili_login import BilibiliLoginManager
from . import subscription as sub

PLUGIN_NAME = "astrbot_plugin_bililive"


class _SafeFormatDict(dict):
    """format_map 用：未知占位符原样保留而不是抛 KeyError，模板写错不炸推送。"""

    def __missing__(self, key):
        return "{" + key + "}"


@register("astrbot_plugin_bililive", "BB0813", "B站UP主开播监测插件", "2.1.0",
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

        self._init_lock = asyncio.Lock()
        self._initialized = False
        self.monitor_task = None
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
    def max_monitors(self) -> int:
        return self._cfg_int("max_monitors", 50)

    @property
    def default_platform(self) -> str:
        """裸群号补全用的平台『实例id』。

        配置留空时自动探测第一个已加载平台的 id（send_message 按 id 匹配）。
        探测不到才回落 aiocqhttp。
        """
        configured = str(self._cfg("default_platform", "") or "").strip()
        if configured:
            return configured
        ids = self._platform_ids()
        return ids[0] if ids else "aiocqhttp"

    def _platform_ids(self) -> List[str]:
        """当前已加载平台的实例 id 列表；取不到返回空。"""
        try:
            insts = self.context.platform_manager.platform_insts
        except Exception:
            return []
        ids = []
        for p in insts or []:
            try:
                ids.append(str(p.meta().id))
            except Exception:
                continue
        return ids

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

    def _prune_group_settings(self, subs: Dict[str, Dict]):
        """清掉已无任何订阅引用的会话设置，避免 groups.json 无限膨胀。"""
        referenced = {
            g.get("umo")
            for info in subs.values()
            for g in info.get("groups", [])
            if isinstance(g, dict)
        }
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
        for name in ("live_notify_template", "end_notify_template"):
            t = str(self._cfg(name, "") or "")
            if not t:
                continue
            try:
                t.format_map(_SafeFormatDict(uname="x", title="x", room_id=0))
            except Exception as e:
                logger.warning(f"配置项 {name} 存在未配对的花括号，推送时将按原文发送: {e}")

    # ---------- 直播状态缓存持久化 ----------
    def _load_state(self):
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cache = data.get("live_status_cache", {})
                if isinstance(cache, dict):
                    self.live_status_cache = {str(k): int(v) for k, v in cache.items()}
                logger.info(f"已加载直播状态缓存 {len(self.live_status_cache)} 条")
        except Exception as e:
            logger.error(f"加载状态缓存失败: {e}")

    def _save_state(self):
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump({"live_status_cache": self.live_status_cache}, f, ensure_ascii=False, indent=2)
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

                self._initialized = True
                total = sum(len(v.get("groups", [])) for v in subs.values())
                logger.info(f"B站开播监测插件初始化完成，{len(subs)} 个UP、{total} 条群订阅")
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
            if self.monitor_task and not self.monitor_task.done():
                self.monitor_task.cancel()
                try:
                    await self.monitor_task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"取消监控任务时出错: {e}")
                finally:
                    self.monitor_task = None
            if self.session and not self.session.closed:
                await self.session.close()
                self.session = None
        except Exception as e:
            logger.error(f"清理资源时出错: {e}")

    async def terminate(self):
        try:
            logger.info("正在停止B站开播监测插件...")
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
            chain = self._build_message_chain(template, uname, title, room_id, cover, at_all)
            await self.context.send_message(origin, chain)
            logger.info(f"开播通知已发送: {uname} -> {origin}")
        except Exception as e:
            logger.error(f"发送开播通知失败: {e}")

    async def send_end_notification(self, status_info: Dict, origin: str, at_all: bool):
        try:
            if not self.enable_notifications or not self.enable_end_notifications:
                return
            if not self._group_notify_enabled(origin, "notify_end"):
                return
            uname = status_info.get("uname", "未知UP主")
            room_id = status_info.get("room_id", 0)
            cover = status_info.get("cover", "")
            template = self._cfg("end_notify_template", "⚫ {uname} 已结束直播")
            chain = self._build_message_chain(template, uname, "", room_id, cover, False)
            await self.context.send_message(origin, chain)
            logger.info(f"关播通知已发送: {uname} -> {origin}")
        except Exception as e:
            logger.error(f"发送关播通知失败: {e}")

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
                        f" / 关播 {'✅' if self._group_notify_enabled(origin, 'notify_end') else '❌'}\n")
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
