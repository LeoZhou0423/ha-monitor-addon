# HA Monitor Add-on

监控辣堡手表（Wear OS）的健康与位置数据，异常时通过 webhook 直推米糊（Hermes Agent）。

## 功能

- **健康监控**：体温过低/过高、心率过快/过慢 → 推送关心告警
- **位置监控**：出门/到家检测，支持**多套房子**（动态读取 HA zone，如"世纪新筑"）
- **心情监控**：心情不好 → 在家时提示放歌安慰
- **数据断报监控**（watchdog）：手表数据超过阈值未更新 → 告警（**只推一次**，数据恢复前不重复）

## 安装

见 [DOCS.md](DOCS.md)。简版：

1. HAOS **设置 → 附加组件 → 附加组件商店 → ⋮ → 仓库** 添加本仓库
2. 商店里安装 **HA Monitor - 辣堡健康位置监控**
3. 启动即可

## 目录结构

```
ha_monitor/
├── config.yaml     # Add-on 定义与配置 schema
├── Dockerfile      # 基于 HA base 镜像 + python3 + aiohttp
├── run.sh          # 入口：bashio 读配置 → 注入环境变量 → 启动 python
├── ha_monitor.py   # 主逻辑（监控 + webhook 直推 + watchdog）
├── DOCS.md         # 附加组件详情文档
└── CHANGELOG.md    # 版本记录
```

## 版本

- 2.0.0：重构为 HAOS Add-on。移植脚本版全部能力：webhook 直连、多 zone 在家判断、断报只推一次、Supervisor 网关认证。
