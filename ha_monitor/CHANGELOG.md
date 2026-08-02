# Changelog

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
