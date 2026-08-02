# Changelog

## 2.0.8

- **修复致命 bug：断报 watchdog 从未运行**。此前 `check_data_freshness()` 任务被埋在 WebSocket 连接成功之后才启动，而本机魔改版 supervisor 的 `/core` 网关代理不可用（WS 无限 `Auth failed`）→ 40 分钟断报告警从未触发。现已将 watchdog 提到 WS 循环**之外独立启动**——它只读本地数据文件 mtime，与 HA 连接完全解耦，WS 连不上也照常告警
- **修复 WS URL 路径**：`ws://supervisor/core/api/websocket` → `ws://supervisor/core/websocket`（官方标准路径，去掉多余的 `/api/`）
- **新增直连模式**：配置 `ha_direct_token`（HA 长期访问令牌）+ `ha_direct_url`（如 `ws://homeassistant:8123/api/websocket`）可绕过 supervisor 网关直连 core，魔改版 supervisor 下事件告警（体温/心率/心情/出门）也能恢复
- **修复版本号**：此前 v2.0.7 忘记在 config.yaml bump version（仍为 2.0.6），导致附加组件商店识别不到更新（`update_available=false`）。本次 version → 2.0.8

## 2.0.7

- 断报容错升级：数据超过阈值未更新只推送**一次**，数据恢复前**绝不重复打扰**（修复此前每 10 分钟重复推送的 bug）
- 断报阈值默认从 15 分钟提高到 **40 分钟**（`stale_min` 配置项同步更新，可改）
- 数据全部恢复后自动重置告警状态，下次再断报才会再次提醒

## 2.0.6

- 修复重复告警：watchdog 同一轮检查把「定位」「健康」各推一条 → 合并为一条消息（如「手表定位(2546分钟)、健康(2546分钟)数据未更新」）

## 2.0.5

- 修复连接失败：config.yaml 增加 `homeassistant_api: true`，允许经 supervisor 网关访问 HA core（此前 WebSocket 认证 `Auth failed`）
- 加固配置读取：绕开 bashio（本魔改版 supervisor 的 `bashio::config` 访问 API 恒被 forbidden），改为脚本直接读取 supervisor 挂载的 `/data/options.json`

## 2.0.4

- 修复启动崩溃：config.yaml 增加 `auth_api: true`，允许 `SUPERVISOR_TOKEN` 访问 supervisor API（此前 `bashio::config` 报 `Unable to access the API, forbidden`，全部配置读成空字符串）
- 加固：环境变量解析改为防御式（空字符串/非法值回退默认值），单个配置项异常不再导致容器崩溃重启

## 2.0.3

- 修复构建失败：base 镜像 3.23 的 Python 是 PEP 668 externally-managed，`pip3 install aiohttp` 被拒（`externally-managed-environment` 错误）。Dockerfile 增加 `--break-system-packages` 绕过

## 2.0.2

- 修复构建失败（最终版）：Dockerfile 中 `BUILD_FROM` 写死默认值 `ghcr.nju.edu.cn/home-assistant/amd64-base:3.23`，不再依赖 supervisor 注入 build-arg（本魔改版 supervisor 构建命令从不传 `BUILD_FROM`）

## 2.0.1

- 修复构建失败：config.yaml 补充 `build_from`（base 镜像用 ghcr.nju.edu.cn 代理源），supervisor 构建时正确注入 `BUILD_FROM`

## 2.0.0

- 重构为 HAOS Add-on（替代原 core-ssh 独立脚本）
- Supervisor 网关认证（`SUPERVISOR_TOKEN`），无需手工配置 HA token
- 配置项全部移到附加组件配置界面（webhook、阈值、断报开关）
- 保留：webhook 直连推送、多 zone 在家判断（世纪新筑）、断报只推一次、启动不推告警

## 1.0.0

- 初版（core-ssh 独立脚本）：写文件告警 + cron 轮询
