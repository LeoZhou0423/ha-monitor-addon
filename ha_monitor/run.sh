#!/usr/bin/env bash
set -e

echo "HA Monitor starting..."

# 配置读取：不再依赖 bashio（本魔改版 supervisor 的 bashio::config 访问 API 被 forbidden）。
# supervisor 会把 options 挂载到 /data/options.json，由 ha_monitor.py 直接读取。
# 仅注入运行环境所需变量，其余由脚本内 _load_options() 处理。
export HA_MCP_URL="http://supervisor/core"

exec python3 /ha_monitor.py
