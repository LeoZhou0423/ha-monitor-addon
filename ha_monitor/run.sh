#!/usr/bin/env bashio
set -e

bashio::log.info "HA Monitor starting..."

# 把 Add-on 配置项注入环境变量（脚本通过 os.environ 读取）
export ALERT_WEBHOOK_URL="$(bashio::config 'webhook_url')"
export ALERT_WEBHOOK_SECRET="$(bashio::config 'webhook_secret')"
export WATCHDOG_ENABLED="$(bashio::config 'watchdog_enabled')"
export DATA_STALE_MIN="$(bashio::config 'stale_min')"
export COOLDOWN="$(bashio::config 'cooldown')"
export TEMP_LOW="$(bashio::config 'temp_low')"
export TEMP_HIGH="$(bashio::config 'temp_high')"
export HR_HIGH="$(bashio::config 'hr_high')"
export HR_LOW="$(bashio::config 'hr_low')"

# 通过 Supervisor 网关访问 HA core（SUPERVISOR_TOKEN 由 HAOS 自动注入）
export HA_MCP_URL="http://supervisor/core"

exec python3 /ha_monitor.py
