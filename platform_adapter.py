"""lark_cli 平台适配器 — 把 lark-cli 的 bot 消息能力接入 AstrBot Platform API。

身份固定 bot(收发均为 ``--as bot``),不提供任何身份选择配置。
事件链:lark-cli NDJSON → kit EventStream(归一化/去重/自消息过滤)→
convert_message → LarkCliPlatformEvent → commit_event。
"""

from __future__ import annotations

import asyncio
import contextlib
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

from .platform_event import LarkCliPlatformEvent, deliver_chain

PLUGIN_DIR = Path(__file__).resolve().parent
VENDOR_DIR = PLUGIN_DIR / "vendor" / "lark-cli"


@register_platform_adapter(
    "lark_cli",
    "lark-cli 平台适配器(bot 身份收发)",
    default_config_tmpl={"enabled_chats": [], "app_id": "", "app_secret": ""},
)
class LarkCliPlatform(Platform):
    def __init__(self, platform_config: dict, platform_settings: dict, event_queue) -> None:
        # manager 固定传 (config, settings, event_queue);基类收 (config, event_queue)
        super().__init__(platform_config, event_queue)
        self.config = platform_config
        self._stream: EventStream | None = None
        self._messenger: LarkMessenger | None = None

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
            logger.info("[lark_cli] bot 凭据就绪(app_id=%s)", app_id)
        else:
            logger.warning(
                "[lark_cli] 未配置 app_id/app_secret,事件消费将无法通过鉴权;"
                "请在平台实例配置中填写"
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
        async for msg in self._stream.stream():
            if not self._chat_enabled(msg.chat_id, msg.chat_type):
                continue
            abm = await self.convert_message(msg)
            await self.handle_msg(abm, msg.chat_id)
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
        if True:  # 自举是内部事务:二进制缺失时总是自动补齐,无用户开关
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

    def _chat_enabled(self, chat_id: str, chat_type: str) -> bool:
        """enabled_chats 白名单;空列表 = 不限制。

        条目支持标准 UMO(``lark_cli:GroupMessage:oc_xx``)或裸 chat_id。
        """
        allowed = [str(c).strip() for c in self.config.get("enabled_chats") or []]
        if not allowed:
            return True
        msg_type = (
            MessageType.GROUP_MESSAGE if chat_type == "group" else MessageType.FRIEND_MESSAGE
        )
        umo = f"{self.meta().name}:{msg_type.value}:{chat_id}"
        return chat_id in allowed or umo in allowed

    async def convert_message(self, msg: NormalizedLarkMessage) -> AstrBotMessage:
        abm = AstrBotMessage()
        abm.type = (
            MessageType.GROUP_MESSAGE if msg.chat_type == "group" else MessageType.FRIEND_MESSAGE
        )
        if msg.chat_type == "group":
            abm.group_id = msg.chat_id
        abm.message_str = msg.text
        abm.sender = MessageMember(user_id=msg.sender_id, nickname=msg.sender_name or msg.sender_id)
        abm.message = [Plain(text=msg.text)]
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


