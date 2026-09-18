import asyncio
import aiohttp
import json
import os
from typing import Dict, List, Optional
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from .bili_login import BilibiliLoginManager
from . import subscription as sub

PLUGIN_NAME = "astrbot_plugin_bililive"


@register("astrbot_plugin_bililive", "BB0813", "B站UP主开播监测插件", "2.0.0",
          "https://github.com/BB0813/astrbot_plugin_bilibiliobs")
class BiliLivePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        # 直播状态缓存 {uid: last_live_status}，每个UP一份（开播是UP的属性，与群无关）
        self.live_status_cache: Dict[str, int] = {}
        self.uid_error_counts: Dict[str, int] = {}
        self.uid_skip_until: Dict[str, float] = {}
        self.current_interval = self._cfg_int("check_interval", 60)
        self._last_rate_limited = False

        self._init_lock = asyncio.Lock()
        self._initialized = False
        self.monitor_task = None
        self.session = None

        # 数据目录（规范路径：data/plugin_data/astrbot_plugin_bililive/）
        self.data_dir = self._get_data_dir()
        self.state_file = os.path.join(self.data_dir, "state.json")

        # 登录管理器
        self.login_manager = BilibiliLoginManager(context, self._save_cookie_to_config)

        asyncio.create_task(self.initialize())

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
        return self._cfg_int("check_interval", 60)

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
        getters = (
            lambda: self.context.platform_manager.platform_insts,
            lambda: self.context.get_platform_insts(),
        )
        for get in getters:
            try:
                insts = get()
            except Exception:
                continue
            ids = []
            for p in insts or []:
                try:
                    ids.append(str(p.meta().id))
                except Exception:
                    continue
            if ids:
                return ids
        return []

    # ---------- 订阅数据：config 是唯一真相源 ----------
    def _load_subs(self) -> Dict[str, Dict]:
        """从 config 读取并解析订阅（每次现读现解析，保证与WebUI改动同步）。"""
        return sub.parse_subscriptions(self._cfg("subscriptions", []) or [], self.default_platform)

    async def _save_subs(self, subs: Dict[str, Dict]):
        """把订阅结构序列化回 config 并落盘（WebUI 与命令共用）。"""
        lines = sub.serialize_subscriptions(subs)
        try:
            self.config["subscriptions"] = lines
        except Exception as e:
            logger.error(f"写入订阅到配置失败: {e}")
            return
        await self._persist_config()

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
            "title": d.get("title", ""),
            "uname": d.get("uname", ""),
            "cover": d.get("cover_from_user", "") or d.get("cover", ""),
        }

    # ---------- 监控循环 ----------
    async def monitor_live_status(self):
        consecutive_errors = 0
        max_consecutive_errors = 5

        while True:
            try:
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
                    at_all = info.get("at_all", False)

                    if cur_status == 1 and prev_status != 1:
                        for origin in groups:
                            await self.send_live_notification(current, origin, at_all)
                    elif prev_status == 1 and cur_status != 1:
                        for origin in groups:
                            await self.send_end_notification(current, origin, at_all)

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
        text = template.format(uname=uname, title=title, room_id=room_id)
        chain = MessageChain()
        if at_all:
            chain.at_all()
        chain.message(text)
        if cover:
            chain.url_image(cover)
        return chain

    async def send_live_notification(self, status_info: Dict, origin: str, at_all: bool):
        try:
            if not self.enable_notifications:
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
    @filter.command("")
    async def handle_all_messages(self, event: AstrMessageEvent):
        """拦截所有消息，优先处理管理员私聊登录命令。"""
        if await self.login_manager.handle_admin_command(event):
            return

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
            if uid in subs and origin in subs[uid].get("groups", []):
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
            elif token.isdigit() and token in subs and origin in subs[token].get("groups", []):
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
                at_tip = " 📢@all" if subs.get(uid, {}).get("at_all") else ""
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
        """查看插件运行状态。"""
        try:
            subs = self._load_subs()
            total_groups = sum(len(v.get("groups", [])) for v in subs.values())
            message = "🔧 B站开播监测状态:\n"
            message += f"• HTTP会话: {'✅ 正常' if (self.session and not self.session.closed) else '❌ 异常'}\n"
            message += f"• 监控任务: {'✅ 运行中' if (self.monitor_task and not self.monitor_task.done()) else '❌ 已停止'}\n"
            message += f"• 监控UP数: {len(subs)}\n"
            message += f"• 群订阅数: {total_groups}\n"
            message += f"• 数据目录: {self.data_dir}"
            yield event.plain_result(message)
        except Exception as e:
            logger.error(f"获取插件状态失败: {e}")
            yield event.plain_result("❌ 获取插件状态失败")

    @filter.command("开启通知")
    async def enable_notify_cmd(self, event: AstrMessageEvent):
        try:
            self.config["enable_notifications"] = True
            await self._persist_config()
            yield event.plain_result("✅ 已开启开播与关播通知")
        except Exception as e:
            logger.error(f"开启通知失败: {e}")
            yield event.plain_result("❌ 开启通知失败")

    @filter.command("关闭通知")
    async def disable_notify_cmd(self, event: AstrMessageEvent):
        try:
            self.config["enable_notifications"] = False
            await self._persist_config()
            yield event.plain_result("✅ 已关闭所有通知")
        except Exception as e:
            logger.error(f"关闭通知失败: {e}")
            yield event.plain_result("❌ 关闭通知失败")

    @filter.command("开启关播通知")
    async def enable_end_notify_cmd(self, event: AstrMessageEvent):
        try:
            self.config["enable_end_notifications"] = True
            await self._persist_config()
            yield event.plain_result("✅ 已开启关播通知")
        except Exception as e:
            logger.error(f"开启关播通知失败: {e}")
            yield event.plain_result("❌ 开启关播通知失败")

    @filter.command("关闭关播通知")
    async def disable_end_notify_cmd(self, event: AstrMessageEvent):
        try:
            self.config["enable_end_notifications"] = False
            await self._persist_config()
            yield event.plain_result("✅ 已关闭关播通知")
        except Exception as e:
            logger.error(f"关闭关播通知失败: {e}")
            yield event.plain_result("❌ 关闭关播通知失败")
