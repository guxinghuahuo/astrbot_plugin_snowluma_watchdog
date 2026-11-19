# SnowLuma 存活监控 v0.1.5

运行在 AstrBot 中的独立插件。它通过 SnowLuma OneBot HTTP 检查 QQ 账号是否在线，并通过 SMTP 向备用邮箱发送QQ 离线、OneBot HTTP 不可达和可选的恢复通知。需要 AstrBot 4.10.4 或更新版本。

## 安装和配置

1. 在 AstrBot WebUI 的「插件」页面选择从链接安装，填写本仓库的 HTTPS 地址。也可以上传本地构建的 `astrbot_plugin_snowluma_watchdog.zip`，保留安装包文件名。AstrBot 4.10.4 按文件名确定插件目录和配置文件名。
2. 打开「SnowLuma 存活监控」的配置，填写 AstrBot 可访问的 OneBot HTTP 基础地址。两个服务共用网络命名空间且 OneBot 监听本地 3000 端口时，可用 `http://127.0.0.1:3000`。其他部署可填写服务名或主机名，例如 `http://onebot.example.test:3000`。如果服务设置了 `/onebot` 等自定义路径，在地址后加上该路径。
3. 填写 SnowLuma 中该账号的 OneBot **HTTP** Access Token。存在多个 `onebot*.json` 时，以 SnowLuma WebUI 当前账号配置的 HTTP 服务为准。可以填写「预期 QQ 号」，插件用 `get_login_info` 核对在线账号。一个插件实例监控一个 HTTP 地址及其对应账号。
4. 按发件邮箱服务商的配置填写 SMTP 主机、端口和加密方式，常见组合为 `465 / ssl` 或 `587 / starttls`。发件邮箱也是 SMTP 登录账号，密码填授权码或应用密码，收件邮箱填接收通知的地址。
5. 打开「启用 SnowLuma 监控」，保存后查看 AstrBot 日志。需要手动生效时，在插件页面重载该插件。默认等待 60 秒后，日志会显示 `[SnowLuma Watchdog] 首次探测` 及结果。

Access Token 和 SMTP 授权码由 AstrBot 保存到插件配置中。日常日志和告警邮件只包含诊断摘要。

## SMTP 测试

填写 SMTP 配置并保存后，用 AstrBot 管理员账号向机器人发送 `/snowluma_smtp_test`。这会使用告警邮件的 SMTP 设置完成加密连接、登录和发信，可在关闭自动监控时使用。已有安装可在插件页面上传同名新 ZIP 升级，沿用原来的配置。

成功时机器人会回复「SMTP 服务器已接收测试邮件」。到配置的收件邮箱可查看主题为 `[SnowLuma Watchdog] SMTP 测试` 的邮件。

失败时机器人和 AstrBot 日志会给出原因。授权码被服务器拒绝时会报告「SMTP 鉴权失败，请检查发件账号和授权码」，并附上服务器提供的 SMTP 状态码（如 `535`）。连接超时、TLS 失败或收件人被拒绝也会分别提示。自动故障和恢复邮件使用相同的错误摘要。

## 探测与告警

插件使用带 Bearer 鉴权的 `POST /get_status`，只依据 `data.online` 判断 QQ 在线或离线，忽略收发链路的 `data.good`。配置了预期 QQ 号时，在线结果还要求 `get_login_info` 返回相同账号。连接失败、响应传输失败和探测超时均记为 OneBot HTTP 不可达，日志和邮件正文会注明「QQ 在线状态未知」。鉴权失败、其他 HTTP 状态错误或响应无效只记日志，并打断连续探测计数。 [SnowLuma 状态 API](https://snowluma.github.io/zh/docs/api/system/get_status) · [SnowLuma 账号 API](https://snowluma.github.io/en/docs/api/system/get_login_info)

默认每 30 秒探测一次，整轮 HTTP 探测超时 5 秒。连续两次 `online=false` 后发送「QQ 离线」邮件。连续两次 HTTP 不可达后发送「OneBot HTTP 不可达」邮件。两类故障分别累计连续次数，使用同一个告警阈值。连续两次 `online=true` 后确认恢复，可关闭恢复邮件。故障邮件送达 SMTP 后才会启用该次故障的恢复邮件。发送失败时，若通知仍适用，每隔至少 5 分钟重试。SMTP 接收成功会写入 AstrBot 日志。

一次故障期间只发送一封告警邮件，即使探测结果在离线和 HTTP 不可达之间切换。每次启动或重载都会在宽限期后重新累计探测结果。如果再次达到故障阈值，会重新告警。如果重启后先确认在线，插件会依据已保存的告警记录发送可选恢复邮件。v0.1.3 升级时开始一份新的离线记录，避免为旧版的链路异常告警发送恢复邮件。

## 验证运行

配置正确且 QQ 已登录时，首次探测应显示 `QQ 在线（online=true）`。如果日志显示 `QQ 在线状态未知`，可根据具体原因检查 HTTP Access Token、服务地址或网络。

使用 `/snowluma_smtp_test` 验证邮件链路。

## 开发

克隆仓库后，在本仓库根目录执行以下命令。

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
python build_package.py
```

打包结果和 SHA-256 校验文件位于 `dist/v0.1.5/`。

## 更新记录

- v0.1.5 增加 OneBot HTTP 连接失败、响应传输失败和探测超时的邮件告警。
- v0.1.4 通用化部署示例、默认地址与邮件主题，通知正文仅显示探测状态。
- v0.1.3 只根据 QQ 在线状态告警，探测失败时记录状态未知。
- v0.1.2 重启后重新确认故障，修复历史告警抑制新故障邮件的问题。
- v0.1.1 增加管理员 SMTP 测试命令和发信失败原因。
- v0.1.0 支持 OneBot HTTP 存活监控、故障邮件及可选恢复邮件。
