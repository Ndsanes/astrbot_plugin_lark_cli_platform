"""astrbot_plugin_lark_cli_platform 插件入口。

导入 platform_adapter 触发 @register_platform_adapter 注册(平台适配器模式);
本 Star 另提供 /lark_auth_login 管理命令(用户设备授权闭环入口)。
"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from . import platform_adapter  # noqa: F401  # 导入即注册 lark_cli 平台适配器

PLUGIN_NAME = "astrbot_plugin_lark_cli_platform"


@register(PLUGIN_NAME, "NDsans", "lark-cli 平台适配器插件(飞书网关)", "v0.3.2")
class LarkCliPlatformPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    def _get_platform(self):
        """返回本插件的平台适配器实例;未加载时 None。"""
        try:
            for p in self.context.platform_manager.get_insts():
                if p.meta().name == "lark_cli":
                    return p
        except Exception:
            pass
        return None

    @filter.command("lark_auth_login")
    async def lark_auth_login(self, event: AstrMessageEvent):
        """/lark_auth_login:发起用户设备授权并向 notify_umos 推送授权卡片。"""
        platform = self._get_platform()
        if platform is None or not getattr(platform, "gateway", None):
            yield event.plain_result("lark_cli 平台适配器未运行,无法发起授权。")
            return
        result = await platform.begin_reauth("管理员手动触发重新授权")
        yield event.plain_result(result)
