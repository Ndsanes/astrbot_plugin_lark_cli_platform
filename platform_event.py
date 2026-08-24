"""LarkCliPlatformEvent — 平台事件:send 走 bot 身份的 kit messenger。

MessageChain 支持范围(第一版):Plain → send_text;Image →
convert_to_file_path() 后 send_image;其余组件 WARNING + 确定性跳过,
不因单个组件崩溃整条链。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.api.platform import AstrBotMessage, PlatformMetadata


async def _deliver(messenger, target: str, message: MessageChain) -> None:
    for comp in message.chain:
        if isinstance(comp, Plain):
            await messenger.send_text(target, comp.text)
        elif isinstance(comp, Image):
            image_path = await comp.convert_to_file_path()
            await messenger.send_image(target, image_path)
        else:
            logger.warning(f"lark_cli 平台暂不支持消息组件 {type(comp).__name__},已跳过")


async def deliver_chain(messenger, target: str, message: MessageChain) -> None:
    """send_by_session 共用的投递逻辑(CLI 失败只记录不抛出)。"""
    try:
        await _deliver(messenger, target, message)
    except Exception as exc:  # noqa: BLE001 — 发送失败不应中断平台循环
        logger.error(f"lark_cli 发送失败(target={target}): {exc}")


class LarkCliPlatformEvent(AstrMessageEvent):
    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        messenger,
        chat_id: str,
    ):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self._messenger = messenger
        self.chat_id = chat_id

    async def send(self, message: MessageChain):
        await _deliver(self._messenger, self.chat_id, message)
        await super().send(message)

    async def send_streaming(self, generator, use_fallback: bool = False) -> None:
        """不支持逐字流式:消费流并聚合全文,整条一次性投递。"""
        parts: list[str] = []
        async for chunk in generator:
            for comp in getattr(chunk, "chain", None) or []:
                text = getattr(comp, "text", None)
                if isinstance(comp, Plain) and text:
                    parts.append(text)
        full_text = "".join(parts).strip()
        if not full_text:
            logger.warning("lark_cli 流式回复聚合后为空,跳过发送")
            return
        await self.send(MessageChain(chain=[Plain(full_text)]))
