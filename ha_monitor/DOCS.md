# HA Monitor - 辣堡健康位置监控

监控辣堡手表（Wear OS）的健康与位置数据，异常时通过 webhook 直推米糊（Hermes Agent）。

## 安装

1. 打开 Home Assistant → **设置 → 附加组件 → 附加组件商店**
2. 右上角 **⋮ → 仓库**，添加本仓库地址
3. 刷新商店，找到 **HA Monitor - 辣堡健康位置监控**，点击 **安装**
4. 安装完成后点击 **启动**

## 配置

在附加组件 **配置** 标签页可调整（保存后需重启附加组件生效）：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| webhook_url | (空，必填) | 米糊 webhook 地址 |
| webhook_secret | (空，必填) | HMAC 签名密钥 |
| watchdog_enabled | true | 数据断报监控开关 |
| stale_min | 300 | 断报阈值（分钟） |
| cooldown | 600 | 告警冷却（秒） |
| temp_low / temp_high | 33.0 / 37.8 | 体温阈值 |
| hr_low / hr_high | 55 / 110 | 心率阈值 |
| alert_interval_min / max | 45 / 120 | 多条告警的随机发送间隔（秒） |

## 日志

**日志** 标签页可查看运行状态：连接成功、实体变化、告警推送记录。

## 工作原理

- 通过 Supervisor 网关（`SUPERVISOR_TOKEN` 自动认证）连接 HA WebSocket，订阅实体状态变化
- 健康异常（体温/心率）、位置变化（出门/到家，支持多套房子）、心情不佳 → 直推 webhook
- 手表数据文件超过阈值未更新 → 断报告警（**只推一次**，恢复前不重复）
