# Changelog

## 2.0.1

- 修复构建失败：config.yaml 补充 `build_from`（base 镜像用 ghcr.nju.edu.cn 代理源），supervisor 构建时正确注入 `BUILD_FROM`

## 2.0.0

- 重构为 HAOS Add-on（替代原 core-ssh 独立脚本）
- Supervisor 网关认证（`SUPERVISOR_TOKEN`），无需手工配置 HA token
- 配置项全部移到附加组件配置界面（webhook、阈值、断报开关）
- 保留：webhook 直连推送、多 zone 在家判断（世纪新筑）、断报只推一次、启动不推告警

## 1.0.0

- 初版（core-ssh 独立脚本）：写文件告警 + cron 轮询
