"""astrbot_plugin_lark_cli_platform 插件入口。

导入 platform_adapter 触发 @register_platform_adapter 注册(平台适配器模式)。
"""

from __future__ import annotations

from astrbot.api.star import Context, Star, register

from . import platform_adapter  # noqa: F401  # 导入即注册 lark_cli 平台适配器

PLUGIN_NAME = "astrbot_plugin_lark_cli_platform"


@register(PLUGIN_NAME, "NDsans", "lark-cli 平台适配器插件", "v0.2.0")
class LarkCliPlatformPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
