"""LarkGateway — 面向其他插件的飞书能力网关。

认证、登录态、TAT 刷新与限速全部收口在 lark_cli 平台适配器内;消费插件通过
``context.platform_manager.get_insts()`` 拿到适配器实例后调用本网关方法,
不接触任何凭据、登录态目录或 lark-cli 子进程细节。

错误统一抛 :class:`LarkGatewayError`(业务调用方宽捕获即可)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:  # 优先使用插件内 vendored 副本(与打包版本严格一致),缺失再退回安装版
    from .astrbot_lark_kit import (
        CliNotFoundError,
        LarkKitError,
        LarkMessenger,
        RateLimiter,
        run_lark_cli,
        run_lark_cli_json,
    )
except ImportError:  # 开发环境:工作区源码或 pip 安装版
    from astrbot_lark_kit import (
        CliNotFoundError,
        LarkKitError,
        LarkMessenger,
        RateLimiter,
        run_lark_cli,
        run_lark_cli_json,
    )

__all__ = ["LarkGateway", "LarkGatewayError"]

_MEDIA_TIMEOUT_S = 60.0
_AUTH_TIMEOUT_S = 660.0


class LarkGatewayError(Exception):
    """网关操作失败(含底层 CLI/鉴权/限速错误),消息为中文可读描述。"""


def _wrap(exc: Exception) -> LarkGatewayError:
    if isinstance(exc, LarkGatewayError):
        return exc
    return LarkGatewayError(str(exc))


class LarkGateway:
    """飞书能力网关。由平台适配器在启动后构造并持有。"""

    def __init__(self, messenger: LarkMessenger) -> None:
        self._messenger = messenger
        # 单点限速:所有经网关的调用(含转发 messenger 的发送)都先过这道闸,
        # 与 messenger 内部限速取交集,整体吞吐不高于 5 req/s。
        self.limiter = RateLimiter(rate=5.0)

    # ── 消息 ──

    async def send_text(self, target: str, text: str) -> None:
        """以 bot 身份发文本。target 为 oc_(群)/ou_(用户)前缀 ID。"""
        try:
            await self.limiter.acquire()
            await self._messenger.send_text(target, text)
        except LarkKitError as exc:
            raise _wrap(exc) from exc

    async def send_card(self, target: str, card: dict) -> None:
        """以 bot 身份向 target 发送飞书卡片(msg_type=interactive)。"""
        try:
            await self.limiter.acquire()
            await self._messenger.send_card(target, card)
        except LarkKitError as exc:
            raise _wrap(exc) from exc

    async def send_image(self, target: str, image_path) -> None:
        """以 bot 身份发本地图片。"""
        try:
            await self.limiter.acquire()
            await self._messenger.send_image(target, image_path)
        except LarkKitError as exc:
            raise _wrap(exc) from exc

    # ── 原始 API 透传 ──

    async def api(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """任意 open-apis 端点透传(lark-cli ``api`` 逃逸通道)。

        成功返回响应 JSON 的 data 部分;非 2xx 或鉴权失败抛 LarkGatewayError。
        """
        args = ["api", method.upper(), path]
        if params is not None:
            args += ["--params", json.dumps(params, ensure_ascii=False)]
        if data is not None:
            args += ["--data", json.dumps(data, ensure_ascii=False)]
        envelope = await self._run(args)
        doc = envelope.document
        if isinstance(doc, dict) and doc:
            return doc
        payload = envelope.data
        return payload if isinstance(payload, dict) else {}

    # ── 文档 ──

    async def fetch_doc(self, doc_ref: str, fmt: str = "markdown") -> str:
        """拉取 wiki/docx 文档正文(user 身份)。"""
        envelope = await self._run(
            [
                "docs",
                "+fetch",
                "--as",
                "user",
                "--doc",
                doc_ref,
                "--doc-format",
                fmt,
                "--detail",
                "simple",
            ]
        )
        content = envelope.document.get("content") if isinstance(envelope.document, dict) else None
        if not isinstance(content, str):
            raise LarkGatewayError("docs +fetch 响应缺少 document.content")
        return content

    async def append_doc(self, doc_ref: str, content_markdown: str) -> int:
        """向文档末尾追加 markdown(单次完整提交),返回新 revision(-1 表示未知)。"""
        import tempfile

        content_markdown = content_markdown.rstrip("\n") + "\n"
        with tempfile.TemporaryDirectory(prefix="lark_gw_wb_") as td:
            payload = Path(td) / "append_block.md"
            payload.write_text(content_markdown, encoding="utf-8")
            envelope = await self._run(
                [
                    "docs",
                    "+update",
                    "--as",
                    "user",
                    "--doc",
                    doc_ref,
                    "--command",
                    "append",
                    "--content",
                    "@append_block.md",
                    "--doc-format",
                    "markdown",
                ],
                timeout_s=_MEDIA_TIMEOUT_S,
                cwd=td,
            )
        doc = envelope.document
        try:
            return int(doc.get("revision_id", -1)) if isinstance(doc, dict) else -1
        except (TypeError, ValueError):
            return -1

    async def download_media(
        self, file_token: str, dest_dir: Path, *, overwrite: bool = False
    ) -> Path | None:
        """下载文档媒体到 dest_dir,返回本地路径;失败返回 None(不抛异常)。

        先走 drive +preview(source_file)(文档禁下时仍可取图),失败退回 +download。
        """
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        # lark-cli 要求 --output 为 cwd 内相对路径
        out_name = f"./{file_token.replace('/', '_')}"
        commands = (
            [
                "drive",
                "+preview",
                "--as",
                "user",
                "--file-token",
                file_token,
                "--type",
                "source_file",
                "--output",
                out_name,
                *(["--overwrite"] if overwrite else []),
            ],
            [
                "drive",
                "+download",
                "--as",
                "user",
                "--file-token",
                file_token,
                "--output",
                out_name,
                *(["--overwrite"] if overwrite else []),
            ],
        )
        envelope: Any = None
        last_exc: Exception | None = None
        for args in commands:
            try:
                await self.limiter.acquire()
                envelope = await run_lark_cli(
                    list(args),
                    extra_env=self._messenger_env(),
                    timeout_s=_MEDIA_TIMEOUT_S,
                    cwd=str(dest_dir),
                )
                break
            except LarkKitError as exc:
                last_exc = exc
                continue
        if envelope is None:
            if isinstance(last_exc, CliNotFoundError):
                raise _wrap(last_exc)
            return None
        saved = envelope.data.get("saved_path") if isinstance(envelope.data, dict) else None
        path = Path(str(saved)) if saved else dest_dir / Path(out_name).name
        return path if path.is_file() and path.stat().st_size > 0 else None

    # ── 登录态 ──

    async def auth_status(self) -> dict[str, Any]:
        """lark-cli auth status 的原始 JSON(未配置/过期时抛 LarkGatewayError)。"""
        return await self._run_json(["auth", "status"])

    async def auth_login_start(self, domain: str = "feishu") -> dict[str, Any]:
        """发起设备授权,返回含 verification_url/device_code 的 JSON。"""
        obj = await self._run_json(["auth", "login", "--domain", domain, "--no-wait", "--json"])
        if not isinstance(obj, dict) or not obj.get("device_code"):
            raise LarkGatewayError(f"设备授权响应缺字段:{obj}")
        return obj

    async def auth_login_finish(self, device_code: str, timeout_s: float = _AUTH_TIMEOUT_S) -> None:
        """完成设备授权轮询(阻塞直到用户确认或超时)。"""
        try:
            await self._run_json(
                ["auth", "login", "--device-code", device_code, "--json"],
                timeout_s=timeout_s,
            )
        except LarkKitError as exc:
            raise _wrap(exc) from exc

    # ── 内部 ──

    def _messenger_env(self) -> dict[str, str]:
        """messenger 持有的子进程环境(HOME 登录态重定向等)。"""
        return getattr(self._messenger, "_env", None) or {}

    async def _run(self, args: list[str], *, timeout_s: float = 30.0, cwd: str | None = None):
        try:
            await self.limiter.acquire()
            return await run_lark_cli(
                args,
                extra_env=self._messenger_env(),
                timeout_s=timeout_s,
                cwd=cwd,
            )
        except LarkKitError as exc:
            raise _wrap(exc) from exc

    async def _run_json(
        self, args: list[str], *, timeout_s: float = 30.0
    ) -> dict[str, Any]:
        try:
            await self.limiter.acquire()
            return await run_lark_cli_json(
                args,
                extra_env=self._messenger_env(),
                timeout_s=timeout_s,
            )
        except LarkKitError as exc:
            raise _wrap(exc) from exc
