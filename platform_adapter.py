"""lark_cli 平台适配器 — 把 lark-cli 的 bot 消息能力接入 AstrBot Platform API。

身份模型:消息面(收发)固定 bot(``--as bot``);user 登录态由本适配器维护
(设备授权 + 周期健康检查),业务能力经 LarkGateway 透传时按需选身份。
事件链:lark-cli NDJSON → kit EventStream(归一化/去重/自消息过滤)→
convert_message → LarkCliPlatformEvent → commit_event。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.star.star_tools import StarTools

try:  # 优先使用插件内 vendored 副本(与打包版本严格一致),缺失再退回安装版
    from .astrbot_lark_kit import (
        DEFAULT_CLI_VERSION,
        CliExecutionError,
        CliNotFoundError,
        EventStream,
        LarkMessenger,
        NormalizedLarkMessage,
        bundled_cli_platform,
        ensure_bot_credentials,
        ensure_bundled_cli,
        ensure_short_home,
        find_bundled_cli,
        resolve_cli_bin,
        resolve_state_home,
    )
except ImportError:  # 开发环境:工作区源码或 pip 安装版
    from astrbot_lark_kit import (
        DEFAULT_CLI_VERSION,
        CliExecutionError,
        CliNotFoundError,
        EventStream,
        LarkMessenger,
        NormalizedLarkMessage,
        bundled_cli_platform,
        ensure_bot_credentials,
        ensure_bundled_cli,
        ensure_short_home,
        find_bundled_cli,
        resolve_cli_bin,
        resolve_state_home,
    )

from .gateway import LarkGateway
from .platform_event import LarkCliPlatformEvent, deliver_chain

PLUGIN_DIR = Path(__file__).resolve().parent
VENDOR_DIR = PLUGIN_DIR / "vendor" / "lark-cli"

# 群聊正文开头的 @提及(飞书渲染形如 "@插件Bot ")
_LEADING_MENTION = re.compile(r"^@\S+\s+")

LARK_CONFIG_METADATA = {
    "app_id": {
        "description": "飞书应用 App ID",
        "type": "string",
        "hint": "必填;cli_xxx 开头,在飞书开放平台应用凭证页面获取",
    },
    "app_secret": {
        "description": "飞书应用 App Secret",
        "type": "string",
        "hint": "必填;与 App ID 配套,bot 身份收发凭据来源",
    },
    "notify_umos": {
        "description": "管理通知目标 UMO 列表",
        "type": "list",
        "hint": "登录态异常/授权链接等卡片推送到这些会话(取末段 oc_/ou_)",
    },
    "user_auth_enabled": {
        "description": "开启用户登录态维护",
        "type": "bool",
        "hint": "周期检查 user 登录态健康,临期/失效自动推授权卡片;"
                 "关闭后仅可经 /lark_auth_login 手动授权",
    },
    "auth_check_hours": {
        "description": "登录态健康检查间隔(小时)",
        "type": "int",
        "hint": "状态恶化时自动发起重新授权并推卡片;仅 user_auth_enabled 开启时生效",
    },
    "auth_warning_hours": {
        "description": "登录态临期阈值(小时)",
        "type": "int",
        "hint": "剩余有效期低于该值即视为临期并提醒;仅 user_auth_enabled 开启时生效",
    },
    "auth_login_domains": {
        "description": "设备授权申请的能力域",
        "type": "string",
        "hint": "逗号分隔(docs,drive,wiki,im,sheets,base,contact 等);"
                "决定 user 令牌可用的能力面",
    },
}


@register_platform_adapter(
    "lark_cli",
    "lark-cli 平台适配器(bot 身份收发)",
    default_config_tmpl={
        "app_id": "",
        "app_secret": "",
        "notify_umos": [],
        "user_auth_enabled": True,
        "auth_check_hours": 6,
        "auth_warning_hours": 48,
        "auth_login_domains": "docs,drive,wiki",
    },
    config_metadata=LARK_CONFIG_METADATA,
    # CLI 每次调用发送完整消息,不支持逐字流式:由核心缓冲整段后一次性下发
    support_streaming_message=False,
)


class LarkCliPlatform(Platform):
    def __init__(self, platform_config: dict, platform_settings: dict, event_queue) -> None:
        # manager 固定传 (config, settings, event_queue);基类收 (config, event_queue)
        super().__init__(platform_config, event_queue)
        self.config = platform_config
        self._stream: EventStream | None = None
        self._messenger: LarkMessenger | None = None
        # 飞书能力网关:其他插件经 platform_manager 拿到本实例后使用
        self.gateway: LarkGateway | None = None
        self._auth_task: asyncio.Task | None = None
        self._reauth_task: asyncio.Task | None = None

    def meta(self) -> PlatformMetadata:
        # AstrBot 4.27+: PlatformMetadata 需要唯一实例 id
        return PlatformMetadata("lark_cli", "lark-cli 平台适配器",
                                id=str(self.config.get("id") or "lark_cli"))
    async def run(self):
        logger.info("[lark_cli] 平台适配器启动中(1/4:数据目录)")
        data_dir = StarTools.get_data_dir("astrbot_plugin_lark_cli_platform")
        # 登录态目录为本插件内部事务:只放在自己的数据目录下,不跨插件共享
        legacy_home = str(self.config.get("lark_cli_home") or "").strip()  # 兼容旧配置
        state_home = Path(legacy_home) if legacy_home else resolve_state_home(data_dir)
        app_id = str(self.config.get("app_id") or "")
        app_secret = str(self.config.get("app_secret") or "")
        if ensure_bot_credentials(state_home, app_id=app_id, app_secret=app_secret):
            logger.info("[lark_cli] bot 凭据已同步(来源:平台配置,app_id=%s)", app_id)
        else:
            logger.error(
                "[lark_cli] 平台配置缺少 app_id/app_secret(必填),事件消费将无法通过鉴权;"
                "请在 WebUI 平台实例配置中填写后重启适配器"
            )
        runtime_home = ensure_short_home(state_home)
        if runtime_home != state_home:
            logger.info(
                "[lark_cli] 登录态路径过深,子进程使用别名 %s -> %s", runtime_home, state_home
            )

        binary = await self._aresolve_binary()
        if binary is None:
            return
        logger.info("[lark_cli] 启动(3/4):二进制就绪 %s", binary)

        self._messenger = LarkMessenger(binary=binary, env={"HOME": str(runtime_home)})
        self._stream = EventStream(binary=binary, state_home=runtime_home,
                                   log_cb=lambda m: logger.info(f"[lark_cli][stream] {m}"))
        logger.info("[lark_cli] 启动(4/4):事件消费进程拉起中...")
        self.gateway = LarkGateway(self._messenger)
        logger.info("[lark_cli] 飞书网关就绪(供其他插件调用 gateway.*)")
        if self._auth_monitor_enabled():
            self._auth_task = asyncio.create_task(self._auth_loop())
        async for msg in self._stream.stream():
            abm = await self.convert_message(msg)
            await self.handle_msg(abm, msg.chat_id)

    def _auth_monitor_enabled(self) -> bool:
        """是否开启用户登录态周期维护。"""
        return bool(self.config.get("user_auth_enabled", True))

    # ── 管理员通知与认证闭环 ──

    def _notify_targets(self) -> list[str]:
        """通知目标:notify_umos 配置的 UMO 列表,取末段会话 ID(oc_/ou_)。"""
        targets = []
        for umo in self.config.get("notify_umos") or []:
            chat_id = str(umo).strip().split(":")[-1]
            if chat_id.startswith(("oc_", "ou_")):
                targets.append(chat_id)
            elif str(umo).strip():
                logger.warning("[lark_cli] notify_umos 条目无法解析会话 ID: %s", umo)
        return targets

    async def send_admin_card(self, card: dict) -> int:
        """向全部 notify_umos 发送飞书卡片;返回成功条数。"""
        if not self.gateway:
            logger.warning("[lark_cli] 网关未就绪,无法发送管理卡片")
            return 0
        sent = 0
        for chat_id in self._notify_targets():
            try:
                await self.gateway.send_card(chat_id, card)
                sent += 1
            except Exception as exc:  # noqa: BLE001 — 单个目标失败不阻断其余
                logger.warning("[lark_cli] 管理卡片发送失败(%s): %s", chat_id, exc)
        return sent

    @staticmethod
    def _build_reauth_card(reason: str, verification_url: str | None) -> dict:
        """登录态异常授权卡片:说明 + 可点击跳转按钮。"""
        elements: list[dict] = [
            {"tag": "div", "text": {"tag": "plain_text", "content": reason}},
        ]
        if verification_url:
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "打开授权页面"},
                            "type": "primary",
                            "url": verification_url,
                        }
                    ],
                }
            )
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "red",
                "title": {"tag": "plain_text", "content": "lark_cli 登录态需要重新授权"},
            },
            "elements": elements,
        }

    def _auth_login_domains(self) -> str:
        """设备授权申请的能力域(逗号分隔),来自 auth_login_domains 配置。"""
        raw = str(self.config.get("auth_login_domains") or "docs,drive,wiki")
        parts = [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]
        return ",".join(parts) or "docs,drive,wiki"

    async def begin_reauth(self, reason: str) -> str:
        """发起设备授权并向管理员推授权卡片;返回结果描述。

        在途防重入:已有未完成的授权轮询时跳过,避免两个设备码并存。
        """
        if not self.gateway:
            return "网关未就绪,无法发起授权"
        if self._reauth_task is not None and not self._reauth_task.done():
            return "已有设备授权在途,跳过重复发起"
        targets = self._notify_targets()
        if not targets:
            return "未配置 notify_umos,无处推送授权卡片;请在平台实例配置中填写"
        initiated = await self.gateway.auth_login_start(self._auth_login_domains())
        url = initiated.get("verification_url")
        sent = await self.send_admin_card(self._build_reauth_card(reason, url))
        # 保存强引用:asyncio 只持弱引用,裸 create_task 可能被 GC 静默取消
        self._reauth_task = asyncio.get_running_loop().create_task(
            self._finish_reauth(initiated["device_code"])
        )
        return f"已发起设备授权并推送卡片({sent}/{len(targets)});链接:{url}"

    async def _finish_reauth(self, device_code: str) -> None:
        try:
            await self.gateway.auth_login_finish(device_code)
            logger.info("[lark_cli] 用户重新授权完成")
            await self.send_admin_card(
                {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "template": "green",
                        "title": {"tag": "plain_text", "content": "lark_cli 登录态已恢复"},
                    },
                    "elements": [],
                }
            )
        except Exception as exc:  # noqa: BLE001 — 超时/取消只记日志
            logger.warning("[lark_cli] 重新授权未完成:%s", exc)

    async def _auth_loop(self) -> None:
        """用户登录态自动维护:启动即首查,之后每 auth_check_hours 检查一次。

        - 判定为非 HEALTHY(含从未授权的冷启动):立即发起设备授权并推卡片;
        - 持续非健康:每隔约 24h 重发一次授权卡片提醒;
        - 单轮检查/授权失败只告警,绝不终止循环。
        """
        from astrbot_lark_kit import Health, auth_status_from_dict, health_of

        interval_h = max(1, int(self.config.get("auth_check_hours") or 6))
        warning_h = float(self.config.get("auth_warning_hours") or 48)
        remind_every = max(1, round(24 / interval_h))
        bad_streak = 0
        logger.info("[lark_cli] 登录态维护已启动(启动即首查,间隔 %sh)", interval_h)
        while True:
            try:
                if self.gateway is not None:
                    try:
                        status = health_of(
                            auth_status_from_dict(await self.gateway.auth_status()),
                            warning_hours=warning_h,
                        )
                    except Exception as exc:  # noqa: BLE001 — 检查失败下轮重试
                        logger.warning("[lark_cli] 登录态检查失败:%s", exc)
                        status = None
                    if status is not None:
                        if status is Health.HEALTHY:
                            bad_streak = 0
                        else:
                            bad_streak += 1
                            if bad_streak == 1 or bad_streak % remind_every == 0:
                                try:
                                    result = await self.begin_reauth(
                                        f"登录态健康状态:{status.value}"
                                    )
                                    logger.warning("[lark_cli] 自动重新授权:%s", result)
                                except Exception as exc:  # noqa: BLE001
                                    logger.warning("[lark_cli] 自动授权发起失败:%s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 循环不因单次异常退出
                logger.warning("[lark_cli] 登录态维护循环异常:%s", exc)
            await asyncio.sleep(interval_h * 3600)


    def _repair_bundled_binary(self) -> None:
        """解压器可能丢失可执行位/版本标记缺失:启动时就地修复(离线安全)。"""
        logger.info(f"[lark_cli][dbg] vendor={VENDOR_DIR} exists={VENDOR_DIR.exists()}")
        plat = bundled_cli_platform()
        logger.info(f"[lark_cli][dbg] plat={plat}")
        if not plat:
            return
        plat_dir = VENDOR_DIR / plat
        binary = plat_dir / "lark-cli"
        logger.info(f"[lark_cli][dbg] probe={binary} exists={binary.is_file()}")
        if not (binary.is_file() and binary.stat().st_size > 0):
            return
        with contextlib.suppress(OSError):
            binary.chmod(binary.stat().st_mode | 0o111)
        marker = plat_dir / ".cli_version"
        if not marker.is_file():
            with contextlib.suppress(OSError):
                marker.write_text(DEFAULT_CLI_VERSION)

    @staticmethod
    def _exec_safe_copy(binary: Path) -> Path:
        """数据卷可能挂载为 noexec:把二进制复制到可执行目录(/tmp)再使用。"""
        import shutil
        import subprocess

        probe = subprocess.run(
            [str(binary), "--version"], capture_output=True, timeout=30,
        )
        if probe.returncode == 0:
            return binary
        target = Path("/tmp/astrbot_lark_cli/lark-cli")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, target)
        target.chmod(0o755)
        check = subprocess.run(
            [str(target), "--version"], capture_output=True, timeout=30,
        )
        if check.returncode != 0:
            raise CliExecutionError(f"/tmp 副本仍不可执行:{check.stderr[:200]}")
        logger.info("[lark_cli] 数据卷 noexec,已改用 /tmp 副本")
        return target

    async def _aresolve_binary(self) -> Path | None:
        self._repair_bundled_binary()
        binary = find_bundled_cli(VENDOR_DIR)
        if binary is not None:
            return binary
        # 自举是内部事务:二进制缺失时总是自动补齐,无用户开关
        try:
            from astrbot_lark_kit import bundled_cli_platform
            plat = bundled_cli_platform()
            plats = (plat,) if plat else ()
            if plats:
                await asyncio.to_thread(ensure_bundled_cli, VENDOR_DIR, platforms=plats)
            binary = find_bundled_cli(VENDOR_DIR)
            if binary is not None:
                return binary
        except Exception as exc:  # noqa: BLE001 — 自举失败降级 PATH 查找
            logger.warning(f"lark_cli 自举下载失败:{exc}")
        try:
            return resolve_cli_bin()
        except CliNotFoundError as exc:
            logger.error(f"lark_cli 平台启动失败:找不到 lark-cli 二进制:{exc}")
            return None

    # ---- 接收 ----------------------------------------------------------

    async def convert_message(self, msg: NormalizedLarkMessage) -> AstrBotMessage:
        abm = AstrBotMessage()
        abm.type = (
            MessageType.GROUP_MESSAGE if msg.chat_type == "group" else MessageType.FRIEND_MESSAGE
        )
        if msg.chat_type == "group":
            abm.group_id = msg.chat_id
        # 群聊文本以"@某名 "开头(飞书把提及渲染进正文):剥掉一次,
        # 否则 wake/命令解析都会被前缀卡住
        text = _LEADING_MENTION.sub("", msg.text, count=1) if msg.chat_type == "group" else msg.text
        abm.message_str = text
        abm.sender = MessageMember(user_id=msg.sender_id, nickname=msg.sender_name or msg.sender_id)
        abm.message = [Plain(text=text)]
        abm.raw_message = msg.raw
        abm.self_id = "lark_cli_bot"
        abm.session_id = msg.chat_id
        abm.message_id = msg.message_id
        abm.timestamp = int(msg.timestamp) if str(msg.timestamp).isdigit() else None
        return abm

    async def handle_msg(self, message: AstrBotMessage, chat_id: str) -> None:
        event = LarkCliPlatformEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            messenger=self._messenger,
            chat_id=chat_id,
        )
        self.commit_event(event)

    # ---- 发送 ----------------------------------------------------------

    async def send_by_session(self, session: MessageSesion, message_chain: MessageChain):
        """按会话发送:session_id 即 chat target(oc_/ou_ 前缀分派)。"""
        if self._messenger is None:
            logger.error("lark_cli send_by_session:messenger 未初始化(平台未 run)")
            return
        await deliver_chain(self._messenger, session.session_id, message_chain)


