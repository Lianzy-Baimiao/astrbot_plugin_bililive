"""B站Cookie扫码登录模块。

管理员私聊Bot发送『更新cookie』/『b站登录』等命令，Bot返回二维码，
扫码后自动把Cookie写回插件配置。
"""
import asyncio
import aiohttp
import os
import tempfile
import time
import qrcode
from typing import Dict, Optional
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Plain
from astrbot.api import logger

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


class BilibiliLoginManager:
    """B站登录管理器"""

    QRCODE_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    QRCODE_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
    NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
    QR_CODE_TTL_SECONDS = 180

    def __init__(self, context, config_save_callback,
                 admin_ids_provider=None, admin_targets_provider=None):
        """初始化登录管理器

        Args:
            context: AstrBot Context 对象
            config_save_callback: 保存Cookie的回调，签名 async def(cookie: str)
            admin_ids_provider: 返回管理员QQ号列表的回调（用于命令鉴权），可为 None
            admin_targets_provider: 返回管理员私聊 umo 列表的回调（用于失效提醒），可为 None
        """
        self.context = context
        self.config_save_callback = config_save_callback
        self.admin_ids_provider = admin_ids_provider
        self.admin_targets_provider = admin_targets_provider
        self._login_in_progress = False
        self._lock = asyncio.Lock()
        self._admin_id = None  # 管理员ID，首次私聊时自动记录（admin_ids 未配置时的兜底）
        self._last_invalid_cookie = None  # 已提醒过失效的 Cookie，避免重试期间刷屏

    async def validate_cookie(self, cookie: str) -> Optional[bool]:
        """验证Cookie是否有效

        Returns:
            True: 有效; False: 无效(-101); None: 网络错误或其他异常
        """
        if not cookie or not cookie.strip():
            return False

        headers = {
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com",
            "Origin": "https://www.bilibili.com",
            "Cookie": cookie.strip(),
        }

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self.NAV_URL, headers=headers) as resp:
                    if resp.content_type != "application/json":
                        return None
                    data = await resp.json()

                    if data.get("code") != 0:
                        error_code = int(data.get("code", 0))
                        if error_code == -101:
                            return False
                        return None

                    nav_data = data.get("data") or {}
                    if "isLogin" not in nav_data:
                        return None
                    return bool(nav_data.get("isLogin"))
        except Exception as e:
            logger.error(f"验证Cookie失败: {e}")
            return None

    def _admin_ids(self) -> list:
        """从插件回调读取管理员QQ号列表；未提供回调时返回空。"""
        if not self.admin_ids_provider:
            return []
        try:
            return [str(x).strip() for x in (self.admin_ids_provider() or []) if str(x).strip()]
        except Exception as e:
            logger.error(f"读取管理员列表失败: {e}")
            return []

    async def handle_admin_command(self, event: AstrMessageEvent) -> bool:
        """处理管理员更新Cookie命令

        Returns:
            True: 命令已处理; False: 不是此命令
        """
        # 只处理私聊消息
        if not self._is_private_message(event):
            return False

        message_text = (event.message_str or "").strip()
        if message_text.lower() not in ["更新cookie", "更新b站cookie", "b站登录", "bilibili登录"]:
            return False

        # 配置了 admin_ids 时做鉴权
        admin_ids = self._admin_ids()
        if admin_ids:
            sender = ""
            try:
                sender = str(event.get_sender_id() or "")
            except Exception:
                pass
            if sender not in admin_ids:
                logger.warning(f"非管理员({sender or '未知'})尝试B站登录命令，已拒绝")
                await event.send(event.plain_result(
                    "❌ 仅插件管理员可以B站登录（在插件配置 admin_ids 中填写QQ号）"))
                return True

        # 记录管理员ID（admin_ids 未配置时的兜底提醒目标）
        if not self._admin_id:
            self._admin_id = event.unified_msg_origin
            logger.info(f"已记录管理员ID: {self._admin_id}")

        await self._start_login_flow(event)
        return True

    def _resolve_admin_targets(self) -> list:
        """Cookie 失效提醒目标：优先插件配置的管理员，退回本次运行记录的私聊。"""
        if self.admin_targets_provider:
            try:
                targets = [str(t).strip() for t in (self.admin_targets_provider() or []) if str(t).strip()]
                if targets:
                    return targets
            except Exception as e:
                logger.error(f"生成管理员提醒目标失败: {e}")
        return [self._admin_id] if self._admin_id else []

    async def check_and_notify_cookie_invalid(self, cookie: str, reason: str = ""):
        """检查Cookie是否失效，失效则通知管理员（同一 Cookie 只提醒一次）"""
        targets = self._resolve_admin_targets()
        if not targets:
            logger.warning("未配置 admin_ids 且本次运行未记录管理员私聊，无法发送Cookie失效通知")
            return

        if cookie and cookie == self._last_invalid_cookie:
            return

        is_valid = await self.validate_cookie(cookie)
        if is_valid is False:
            self._last_invalid_cookie = cookie
            reason_text = reason or "Cookie已失效"
            message = (
                f"⚠️ 检测到B站Cookie不可用\n"
                f"原因: {reason_text}\n\n"
                f"请回复以下任一命令进行扫码登录续期：\n"
                f"• 更新cookie\n"
                f"• b站登录\n"
                f"或手动在插件配置中更新 bilibili_cookie"
            )
            for target in targets:
                try:
                    await self.context.send_message(target, message)
                    logger.info(f"已向管理员发送Cookie失效通知: {target}")
                except Exception as e:
                    logger.error(f"发送Cookie失效通知失败({target}): {e}")

    def _is_private_message(self, event: AstrMessageEvent) -> bool:
        """判断是否为私聊消息（按消息类型判断，跨平台通用）。"""
        try:
            return bool(event.is_private_chat())
        except Exception:
            pass
        # 旧版 AstrBot 兜底：unified_msg_origin 第二段是消息类型
        parts = (event.unified_msg_origin or "").split(":")
        return len(parts) >= 2 and parts[1].lower() in ("friendmessage", "privatemessage")

    async def _start_login_flow(self, event: AstrMessageEvent):
        """启动扫码登录流程"""
        async with self._lock:
            if self._login_in_progress:
                await event.send(event.plain_result("⚠️ 已有一轮扫码登录正在进行，请等待完成"))
                return
            self._login_in_progress = True

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                payload = await self._generate_login_payload(session)

            await self._send_qrcode(event, payload["login_url"])

            asyncio.create_task(
                self._poll_login_and_notify(
                    qrcode_key=payload["qrcode_key"],
                    unified_msg_origin=event.unified_msg_origin,
                )
            )

        except Exception as e:
            self._login_in_progress = False
            logger.error(f"启动扫码登录失败: {e}")
            await event.send(event.plain_result(f"❌ 启动扫码登录失败: {e}"))

    async def _generate_login_payload(self, session: aiohttp.ClientSession) -> Dict[str, str]:
        """生成登录二维码数据"""
        headers = {
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com",
            "Origin": "https://www.bilibili.com",
        }

        async with session.get(self.QRCODE_GENERATE_URL, headers=headers) as resp:
            data = await resp.json()

        if data.get("code") != 0:
            raise RuntimeError(f"生成二维码失败: {data.get('message', '未知错误')}")

        payload = data.get("data") or {}
        login_url = str(payload.get("url", "")).strip()
        qrcode_key = str(payload.get("qrcode_key", "")).strip()

        if not login_url or not qrcode_key:
            raise RuntimeError("生成二维码失败: 响应数据不完整")

        return {"login_url": login_url, "qrcode_key": qrcode_key}

    async def _send_qrcode(self, event: AstrMessageEvent, login_url: str):
        """发送二维码图片"""
        qr_path = None
        try:
            qr_path = await asyncio.to_thread(self._create_qrcode_image, login_url)

            chain = [
                Plain(
                    "📱 请使用哔哩哔哩客户端扫描下方二维码完成登录\n"
                    f"⏱ 二维码有效期: {self.QR_CODE_TTL_SECONDS // 60} 分钟\n\n"
                    "若无法显示图片，可在手机浏览器打开：\n"
                    f"{login_url}"
                ),
                Image.fromFileSystem(qr_path),
            ]

            await event.send(event.chain_result(chain))
            logger.info("二维码已发送")

        except Exception as e:
            logger.warning(f"发送二维码图片失败: {e}，回退为纯文本")
            await event.send(
                event.plain_result(
                    f"📱 请在手机浏览器打开以下链接完成登录：\n{login_url}\n"
                    f"⏱ 链接有效期: {self.QR_CODE_TTL_SECONDS // 60} 分钟"
                )
            )

        finally:
            if qr_path and os.path.exists(qr_path):
                try:
                    os.remove(qr_path)
                except Exception:
                    pass

    @staticmethod
    def _create_qrcode_image(login_url: str) -> str:
        """创建二维码图片文件"""
        fd, qr_path = tempfile.mkstemp(prefix="bili_qr_", suffix=".png")
        os.close(fd)

        try:
            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=8,
                border=4,
            )
            qr.add_data(login_url)
            qr.make(fit=True)
            qr.make_image(fill_color="black", back_color="white").save(qr_path)
            return qr_path
        except Exception:
            try:
                os.remove(qr_path)
            except Exception:
                pass
            raise

    async def _poll_login_and_notify(self, qrcode_key: str, unified_msg_origin: str):
        """轮询登录状态并通知结果"""
        try:
            deadline = time.monotonic() + self.QR_CODE_TTL_SECONDS
            headers = {
                "User-Agent": UA,
                "Referer": "https://www.bilibili.com",
                "Origin": "https://www.bilibili.com",
            }

            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                while time.monotonic() < deadline:
                    await asyncio.sleep(2)

                    try:
                        async with session.get(
                            self.QRCODE_POLL_URL,
                            params={"qrcode_key": qrcode_key},
                            headers=headers,
                        ) as resp:
                            poll_data = await resp.json()
                            poll_result = poll_data.get("data", {}) or {}
                            code = poll_result.get("code")

                            if code == 0:
                                cookie = self._extract_cookie(resp)
                                if cookie:
                                    self._last_invalid_cookie = None
                                    await self.config_save_callback(cookie)
                                    await self.context.send_message(
                                        unified_msg_origin,
                                        "✅ B站扫码登录成功，Cookie已自动更新！"
                                    )
                                    logger.info("B站登录成功，Cookie已更新")
                                else:
                                    await self.context.send_message(
                                        unified_msg_origin,
                                        "❌ 登录成功但未获取到有效Cookie，请手动配置"
                                    )
                                    logger.error("登录成功但Cookie提取失败")
                                return

                            elif code == 86038:
                                await self.context.send_message(
                                    unified_msg_origin,
                                    "⏰ 二维码已过期，请重新发起登录"
                                )
                                logger.info("二维码已过期")
                                return

                            elif code in (86090, 86101):
                                # 86090: 未扫描, 86101: 已扫描未确认
                                continue

                    except Exception as e:
                        logger.error(f"轮询登录状态失败: {e}")
                        await asyncio.sleep(2)
                        continue

                await self.context.send_message(
                    unified_msg_origin,
                    "⏰ 扫码登录超时，请重新发起"
                )
                logger.info("扫码登录超时")

        except Exception as e:
            logger.error(f"扫码登录流程异常: {e}")
            try:
                await self.context.send_message(
                    unified_msg_origin,
                    f"❌ 扫码登录失败: {e}"
                )
            except Exception:
                pass

        finally:
            self._login_in_progress = False

    @staticmethod
    def _extract_cookie(resp: aiohttp.ClientResponse) -> str:
        """从登录响应中提取Cookie"""
        cookies = {}

        set_cookie_headers = resp.headers.getall("Set-Cookie", [])
        for set_cookie in set_cookie_headers:
            parts = set_cookie.split(";")
            if parts:
                kv = parts[0].split("=", 1)
                if len(kv) == 2:
                    cookies[kv[0].strip()] = kv[1].strip()

        required_keys = ["SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5"]
        cookie_parts = []
        for key in required_keys:
            if key in cookies:
                cookie_parts.append(f"{key}={cookies[key]}")

        if not cookie_parts:
            return ""

        return "; ".join(cookie_parts)
