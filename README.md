# astrbot_plugin_lark_cli_platform

lark-cli 平台适配器 + 飞书能力网关:把飞书消息接入 AstrBot Platform API,
并作为本机所有 lark-cli 能力的**唯一认证收口点**(双身份维护)。

## 身份模型

同一份登录态目录里并存两个身份:

| 身份 | 凭据来源 | 维护方 |
| --- | --- | --- |
| `bot` | 平台配置的 `app_id`/`app_secret`(tenant_access_token 由 CLI 自管) | 适配器启动时播种,零人工 |
| `user` | 设备流授权的 user_access_token(带 domain/scope) | `/lark_auth_login` + `_auth_loop` 周期健康检查(可关) |

- **消息面固定 bot**:收(`event consume --as bot`)发(`im +messages-send --as bot`)
  均显式钉死,不受 CLI `defaultAs=auto` 影响(auto 在双身份并存时会静默解析成 user)。
- **能力面按需选身份**:lark-cli 全部 API 方法都声明了 `access_tokens`
  (实测 231 个 typed 方法:42 个仅 user、8 个仅 bot、181 个双身份),
  消费方经网关透传时用 `as_identity="bot"|"user"` 显式选择。

## 网关(LarkGateway)

其他插件经 `context.platform_manager.get_insts()` 找到本适配器实例后调用其
`gateway` 属性,**不接触任何凭据、登录态目录或子进程细节**。错误统一
`LarkGatewayError`,消费方 None/异常一律降级不崩溃。

| 方法 | 身份 | 说明 |
| --- | --- | --- |
| `send_text/send_card/send_image(target, …)` | 固定 bot | 消息平面 |
| `api(method, path, *, params, data, as_identity="bot")` | 可选 | HTTP 路径逃逸通道 |
| `call(cli_args, *, as_identity="bot", timeout_s=30, cwd=None)` | 可选 | **通用命令通配层**,任意一次性 typed/shortcut 命令 |
| `fetch_doc / append_doc / download_media` | 固定 user | 文档读写糖方法(含 cwd/tempfile 编排) |
| `auth_status / auth_login_start / auth_login_finish` | — | 登录态观测与设备授权闭环 |

通配层示例(不逐能力包装,直接用 CLI 原生命令面):

```python
await gw.call(["wiki", "+node-list", "--space-id", "7056…"], as_identity="user")
await gw.call(["sheets", "+cells-get", "--range", "Sheet1!A1:B2", ...])   # 默认 bot
await gw.api("GET", "/open-apis/wiki/v4/spaces", params={"page_size": 20})
```

### 通配层守卫

- 首命令不得为 `auth/config/profile/update/doctor/event/help/__complete`
  (凭据与 CLI 自身管理由平台自管;`event` 是平台独占的长连接);
- `cli_args` 内禁止自带 `--as`/`--profile`(身份单一权威在 `as_identity`);
- high-risk-write 命令需要 `--yes`,调用方自行确认后显式传入。

## 配置(WebUI 表单)

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `app_id` / `app_secret` | 必填 | bot 凭据唯一权威来源,启动时播种进登录态目录 |
| `notify_umos` | `[]` | 授权提醒/告警推送目标(取末段 oc_/ou_ 推卡片) |
| `user_auth_enabled` | `true` | 开启 user 登录态自动维护:启动即首查,非健康自动推授权卡片 |
| `auth_check_hours` / `auth_warning_hours` | `6` / `48` | 健康检查周期/提前告警阈值 |
| `auth_login_domains` | `docs,drive,wiki` | 设备授权申请的能力域(逗号分隔),决定 user 令牌可用能力面 |
| `bootstrap_cli` | `true` | 找不到二进制时自动下载到 `vendor/lark-cli/` |
| `lark_cli_home` | 插件数据目录下 | 登录态 HOME 兼容键 |

## 测试

```bash
python -m pytest astrbot_plugin_lark_cli_platform/tests -q
```

零网络、零真实 CLI(stub + 注入假事件流)。
