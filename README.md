# astrbot_plugin_lark_cli_platform

lark-cli 平台适配器:以 **bot 身份**把飞书消息接入 AstrBot Platform API。

## 特性

- 收发身份固定 bot(`--as bot`),无任何身份选择配置
- 事件经 `astrbot_lark_kit.EventStream` 归一化(去重、bot 自消息过滤、bounded 重启退避),不解析原始 NDJSON
- MessageChain 第一版支持 `Plain` / `Image`;其余组件 WARNING + 跳过,不影响整链
- `enabled_chats` 白名单:空列表 = **不限制**;条目支持标准 UMO(`lark_cli:GroupMessage:oc_xx`)或裸 chat_id
- 可选自举:`bootstrap_cli=true` 且 vendored 二进制缺失时自动下载到 `vendor/lark-cli/`

## 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `lark_cli_home` | `""` | lark-cli 登录态 HOME;空 = `<插件数据目录>/lark_cli_home`(与 feishu_qa 共享同一约定) |
| `bootstrap_cli` | `true` | 找不到二进制时是否自动下载 |
| `enabled_chats` | `[]` | 会话白名单 |

WebUI:机器人 → 新增平台 → `lark_cli`。

## 测试

```bash
.venv/bin/python -m pytest astrbot_plugin_lark_cli_platform/tests -q
```

零网络、零真实 CLI(stub + 注入假事件流)。
