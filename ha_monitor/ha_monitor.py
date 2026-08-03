#!/usr/bin/env python3
"""
HA Monitor (HAOS Add-on) - pushes alerts directly to the hermes webhook (HA -> AI 直连).
Runs as a Home Assistant Add-on:
  - 配置：通过 Add-on options 注入环境变量（见 run.sh）
  - 认证：SUPERVISOR_TOKEN 由 HAOS 自动注入，经 supervisor 网关访问 HA core
  - 数据：/config 挂载 HA 配置目录（.storage/zone、android_gps_app 数据文件）
"""
import asyncio, json, os, sys, hmac, hashlib, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
HA_URL = os.environ.get("HA_MCP_URL", "http://supervisor/core")

# Add-on options 文件名 -> 环境变量名 映射（options.json 由 supervisor 挂载到 /data）
_OPTIONS_ENV_MAP = {
    "webhook_url": "ALERT_WEBHOOK_URL",
    "webhook_secret": "ALERT_WEBHOOK_SECRET",
    "watchdog_enabled": "WATCHDOG_ENABLED",
    "stale_min": "DATA_STALE_MIN",
    "cooldown": "COOLDOWN",
    "temp_low": "TEMP_LOW",
    "temp_high": "TEMP_HIGH",
    "hr_high": "HR_HIGH",
    "hr_low": "HR_LOW",
    "ha_direct_token": "HA_DIRECT_TOKEN",
    "ha_direct_url": "HA_DIRECT_URL",
}

def _load_options():
    """从 /data/options.json 读取 Add-on 配置并注入环境变量（优先于默认值）。

    bashio 在本魔改版 supervisor 上不可用（API forbidden），
    supervisor 会把 options 挂载到容器内 /data/options.json，直接读取更可靠。
    """
    try:
        with open("/data/options.json", encoding="utf-8") as f:
            opts = json.load(f)
        for key, env in _OPTIONS_ENV_MAP.items():
            if key in opts and opts[key] is not None:
                os.environ[env] = str(opts[key])
        print(f"Options loaded from /data/options.json: {len(opts)} keys", file=sys.stderr)
    except FileNotFoundError:
        print("WARN: /data/options.json 不存在，使用默认配置（可能非 Add-on 环境）", file=sys.stderr)
    except Exception as e:
        print(f"WARN: 读取 /data/options.json 失败: {e}，使用默认配置", file=sys.stderr)

_load_options()

# 直连 core（绕过 supervisor 网关代理）：
#   HA_DIRECT_URL   - 直连 WebSocket 地址，如 ws://homeassistant:8123/api/websocket
#   HA_DIRECT_TOKEN - 直连用的长期访问令牌（Long-Lived Access Token，在 HA 用户资料页创建）
# 当魔改版 supervisor 的 /core 网关代理不可用时（本机即如此），可配置直连绕过。
# 注意：必须在 _load_options() 之后读取环境变量，否则 options 尚未注入。
HA_DIRECT_URL = os.environ.get("HA_DIRECT_URL", "ws://homeassistant:8123/api/websocket")
HA_DIRECT_TOKEN = os.environ.get("HA_DIRECT_TOKEN", "").strip()

def _env_int(key, default):
    """防御性解析：空字符串或非法值回退默认值，避免容器启动崩溃。"""
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"WARN: 环境变量 {key}={raw!r} 不是整数，使用默认值 {default}", file=sys.stderr)
        return default

def _env_float(key, default):
    """防御性解析：空字符串或非法值回退默认值，避免容器启动崩溃。"""
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"WARN: 环境变量 {key}={raw!r} 不是浮点数，使用默认值 {default}", file=sys.stderr)
        return default

COOLDOWN = _env_int("COOLDOWN", 600)  # 10分钟，与HA同步频率一致

# --- 辣堡身体档案（Add-on 配置可覆盖）---
# 14岁 | 男 | 99kg | 178cm
# 体感舒适温度：34-35°C
TEMP_LOW = _env_float("TEMP_LOW", 33.0)          # 低于此=偏低
TEMP_HIGH = _env_float("TEMP_HIGH", 37.8)        # 高于此=发烧
HR_HIGH = _env_int("HR_HIGH", 110)               # 14岁男正常静息心率上限
HR_LOW = _env_int("HR_LOW", 55)                  # 下限

# Alert queue file - fallback only (webhook 直连优先)
ALERT_FILE = os.environ.get("ALERT_FILE", "/config/ha_alerts.json")

# --- Webhook 直连 (HA -> AI) ---
WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "http://192.168.0.41:8644/webhooks/ha_alerts")
WEBHOOK_SECRET = os.environ.get("ALERT_WEBHOOK_SECRET", "")
WATCHDOG_ENABLED = os.environ.get("WATCHDOG_ENABLED", "true").lower() in ("1", "true", "yes", "on")

# --- 数据文件路径（Add-on 容器内 /config = HA 的配置目录）---
GPS_DATA_FILE = os.environ.get("GPS_DATA_FILE", "/config/android_gps_app/gps_data.json")
HEALTH_DATA_FILE = os.environ.get("HEALTH_DATA_FILE", "/config/android_gps_app/health_data.json")
DATA_STALE_MIN = _env_int("DATA_STALE_MIN", 40)  # 超过 N 分钟未更新视为断报

last_alert_at = {}
is_home = False  # Track home status

# WS 重连抑制：core 重启后 HA 会批量重推实体快照，连接建立后前 N 秒忽略所有事件，
# 防止把"恢复流"误判成真实到家/出门/异常，连发多条假告警。
RE_CONNECT_SUPPRESS = _env_int("RE_CONNECT_SUPPRESS", 30)
_reconnect_at = 0.0  # 最近一次 WS 成功连接的时间戳（epoch 秒）

# 家的状态集合：默认 "home" + 所有非 passive 的 HA zone 名（支持多套房子）。
# 从 /config/.storage/zone 动态读取，新增/删除 zone 无需改脚本。
HOME_STATES = {"home"}

def _load_home_zones():
    """读取 HA zone 配置，返回所有非 passive zone 的名称集合。"""
    try:
        with open("/config/.storage/zone", encoding="utf-8") as f:
            data = json.load(f)
        zones = set()
        for item in data.get("data", {}).get("items", []):
            if not item.get("passive", False) and item.get("name"):
                zones.add(item["name"])
        if zones:
            return {"home"} | zones
    except Exception as e:
        print(f"Load zones error: {e}", file=sys.stderr)
    return {"home"}

HOME_STATES = _load_home_zones()

def _is_home_state(state):
    """state 是否表示在家（默认 home 或任一非 passive zone）。"""
    if not state:
        return False
    s = state.strip().lower()
    return s == "home" or s in {z.lower() for z in HOME_STATES}

def get_token():
    # 1. Add-on 环境：SUPERVISOR_TOKEN 由 HAOS 自动注入（最高优先）
    for key in ("SUPERVISOR_TOKEN", "HA_MCP_TOKEN", "HOME_ASSISTANT_TOKEN"):
        val = os.environ.get(key)
        if val:
            return val

    # 2. 从 .env 文件读取（兼容非 Add-on 手动运行场景）
    env_paths = [
        os.path.expanduser("~/hermes-agent/profiles/mihu/.env"),
        "/config/.env",
        os.path.expanduser("~/.env"),
    ]
    for env_path in env_paths:
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    parts = line.split("=", 1)
                    if len(parts) == 2 and parts[0] in ("HOME_ASSISTANT_TOKEN", "HA_MCP_TOKEN", "SUPERVISOR_TOKEN"):
                        return parts[1].strip().strip("'\"")
        except:
            pass

    return None

def should_alert(key, now):
    if last_alert_at.get(key, 0) + COOLDOWN > now:
        return False
    last_alert_at[key] = now
    return True

def post_webhook(msg, level="warning"):
    """POST 告警到 hermes webhook（HA -> AI 直连）。成功返回 True。"""
    try:
        body = json.dumps({
            "type": "alert",
            "alert": {"msg": msg, "level": level, "ts": datetime.now(TZ).isoformat()}
        }, ensure_ascii=False).encode("utf-8")
        sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(
            WEBHOOK_URL, data=body, method="POST",
            headers={"Content-Type": "application/json", "X-Webhook-Signature": sig},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            ok = r.status == 202
        print(f"Webhook sent (HTTP {r.status}): {msg[:40]}...", file=sys.stderr)
        return ok
    except Exception as e:
        print(f"Webhook error: {e}", file=sys.stderr)
        return False

def write_alert(msg):
    """直连 webhook 推送；失败时兜底写入本地队列文件。"""
    if post_webhook(msg):
        return
    try:
        alerts = []
        if os.path.exists(ALERT_FILE):
            with open(ALERT_FILE, encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    data = json.loads(content)
                    if isinstance(data, list):
                        alerts = data
                    elif isinstance(data, dict):
                        alerts = [data] if data else []
        alerts.append({"msg": msg, "ts": datetime.now(TZ).isoformat()})
        with open(ALERT_FILE, "w", encoding="utf-8") as f:
            json.dump(alerts[-10:], f, ensure_ascii=False)
        print(f"Alert queued (fallback): {msg[:40]}...", file=sys.stderr)
    except Exception as e:
        print(f"Write alert error: {e}", file=sys.stderr)


# --- 数据新鲜度 watchdog ---
# 断报告警"只推一次"：推过后记录在 last_stale_alert_at，直到数据全部恢复才重置。
# 绝不重复打扰 —— 数据持续断报期间不再推送（与全局 COOLDOWN 无关）。
last_stale_alert_at = {}  # key -> ts，标记已推过

async def check_data_freshness():
    """周期检查手表数据文件 mtime，超过阈值未更新则告警（只推一次，恢复前不重复）。

    定位/健康两个文件同轮检查合并为一条消息，避免重复打扰。
    """
    while True:
        try:
            now = datetime.now(TZ).timestamp()
            stale_items = []  # (label, age_min)
            all_recovered = True
            for label, fpath in (("定位", GPS_DATA_FILE), ("健康", HEALTH_DATA_FILE)):
                if not os.path.exists(fpath):
                    continue  # 文件不存在不告警（可能是首次部署）
                mtime = os.path.getmtime(fpath)
                age_min = (now - mtime) / 60.0
                if age_min > DATA_STALE_MIN:
                    stale_items.append((label, age_min))
                    all_recovered = False
                else:
                    last_stale_alert_at.pop(f"stale_{label}", None)  # 单项恢复，清理冷却
            # 数据全部恢复 → 重置"已推过"标记，允许下次断报再次提醒
            if all_recovered and last_stale_alert_at:
                last_stale_alert_at.clear()
                print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] 数据已恢复，重置断报告警状态",
                      file=sys.stderr)
            # 有断报且本轮未推过 → 推一次并标记
            if stale_items and "stale_all" not in last_stale_alert_at:
                last_stale_alert_at["stale_all"] = now
                parts = "、".join(f"{label}({age:.0f}分钟)" for label, age in stale_items)
                msg = (f"【系统提示】手表{parts}数据未更新，可能已断报，请检查手表与网络连接。")
                print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {msg}", file=sys.stderr)
                write_alert(msg)
        except Exception as e:
            print(f"Watchdog error: {e}", file=sys.stderr)
        await asyncio.sleep(60)  # 每分钟检查一次


def is_relevant(eid):
    e = eid.lower()
    return any(k in e for k in ["kafree", "watch", "wear", "heart", "体温", "心率",
                                 "body_temperature", "heart_rate", "device_tracker", "person",
                                 "mood", "心情", "ti_wen", "xin_lu"])

# 无效状态：同步失败/占位时 HA 实体可能进入这些状态，必须忽略，不得触发告警。
# 例如 device_tracker 从 "home" 变成 "unknown" 是数据丢失而非真的出门。
_INVALID_STATES = {"", "unknown", "unavailable", "none", "null", "nan"}

def _is_valid_state(state):
    """state 是否有效（非 unknown/unavailable 等占位状态）。"""
    if not state:
        return False
    return state.strip().lower() not in _INVALID_STATES

async def main():
    # 数据新鲜度 watchdog 独立于 WebSocket 启动：
    # 即使 WS 认证失败（如魔改版 supervisor 网关代理不可用），
    # 断报告警（40 分钟未更新）依然工作——它只读本地数据文件 mtime。
    if WATCHDOG_ENABLED and "watchdog_task" not in globals():
        globals()["watchdog_task"] = asyncio.create_task(check_data_freshness())
        print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] Watchdog started (independent of WS)",
              file=sys.stderr)

    token = get_token()
    if not token:
        print("No HA token", file=sys.stderr)
        # 无 token 时 watchdog 依然运行（它是独立任务），直接挂起避免退出
        await asyncio.Event().wait()

    # 直连模式：配置了 HA_DIRECT_TOKEN 时，绕过 supervisor 网关直接连 core。
    if HA_DIRECT_TOKEN:
        ws_url = HA_DIRECT_URL
        print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] Direct WS mode: {ws_url}",
              file=sys.stderr)
    else:
        # 标准路径：ws://supervisor/core/websocket（注意不是 /api/websocket）
        ws_url = HA_URL.replace("http://", "ws://").replace("https://", "wss://") + "/websocket"
    print(f"Token OK, len={len(token)}", file=sys.stderr)

    try:
        import aiohttp
    except ImportError:
        print("aiohttp not installed", file=sys.stderr)
        await asyncio.Event().wait()

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url) as ws:
                    msg = await ws.receive_json()
                    if msg.get("type") == "auth_required":
                        await ws.send_json({"type": "auth", "access_token": token})
                        r = await ws.receive_json()
                        if r.get("type") != "auth_ok":
                            print(f"Auth failed, retry 30s...", file=sys.stderr)
                            await asyncio.sleep(30)
                            continue

                    await ws.send_json({
                        "id": 1, "type": "subscribe_events",
                        "event_type": "state_changed"
                    })

                    now_str = datetime.now(TZ).strftime("%H:%M:%S")
                    print(f"[{now_str}] HA Monitor connected (startup alert suppressed).", file=sys.stderr)
                    # 启动成功不推 webhook：凌晨重启会以"关心"名义打扰用户，且非异常无需告警
                    # 重连抑制窗口：core 重启后 HA 会批量重推全部实体快照，
                    # 若在窗口内把这些快照当真实事件，会连发一堆"到家/出门/心跳"假告警。
                    global _reconnect_at
                    _reconnect_at = datetime.now(TZ).timestamp()

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await handle_event(msg.data)
                        else:
                            break

        except Exception as e:
            print(f"WS error: {e}, reconnect 15s...", file=sys.stderr)
            await asyncio.sleep(15)

async def handle_event(raw_data):
    global is_home, _reconnect_at
    try:
        msg = json.loads(raw_data)
    except: return

    event = msg.get("event", {})
    data = event.get("data", {})
    eid = data.get("entity_id", "")
    ns = data.get("new_state", {})
    os_ = data.get("old_state", {})
    if not eid or not ns or not is_relevant(eid): return
    now = datetime.now(TZ).timestamp()

    # ── 快照事件检测 ──────────────────────────────────────────────
    # WS 重连后 HA 会把全部实体当前状态作为 state_changed 推送，
    # 其特征是 old_state 为 null（无旧值可比）。真实变化一定有旧值。
    # 快照事件不是真实变化，绝不当"到家/出门/异常"处理。
    if not os_ or not isinstance(os_, dict):
        print(f"[DEBUG] 快照事件(无旧状态), 跳过: {eid} → {ns.get('state','')}", file=sys.stderr)
        return

    # ── 重连抑制窗口 ──────────────────────────────────────────────
    # 连接刚建立的前 RE_CONNECT_SUPPRESS 秒内，忽略所有事件：
    # 保护 core 重启瞬间叠加的批量事件（即使带旧值也多为恢复流）。
    if now - _reconnect_at < RE_CONNECT_SUPPRESS:
        print(f"[DEBUG] 重连抑制窗口内({int(now-_reconnect_at)}s), 忽略: {eid}", file=sys.stderr)
        return

    print(f"[DEBUG] 相关实体变化: {eid}", file=sys.stderr)

    state = ns.get("state", "")
    old = os_.get("state", "") if os_ else ""
    if state == old:
        print(f"[DEBUG] 状态未变化, 跳过", file=sys.stderr)
        return
    # 无效状态（unknown/unavailable/空）忽略：同步失败/数据占位不是真实事件，
    # 例如 device_tracker "home"→"unknown" 是丢数据，绝不能误报"出门了"。
    if not _is_valid_state(state):
        print(f"[DEBUG] 无效状态 {state!r}, 忽略 (old={old!r})", file=sys.stderr)
        return
    attrs = ns.get("attributes", {})
    alerts = []
    ts = datetime.now(TZ).strftime('%H:%M:%S')
    print(f"[{ts}] {eid}: {old} → {state}", file=sys.stderr)

    if "temperature" in eid.lower() or "体温" in eid or "ti_wen" in eid:
        try:
            t = float(state)
            if t < TEMP_LOW and should_alert("temp_low", now):
                alerts.append(f"【系统提示】辣堡体温{t}°C偏低，请关心他的身体状况，但不要提到任何技术细节")
                print(f"[DEBUG] 生成系统提示: 体温偏低", file=sys.stderr)
            elif t > TEMP_HIGH and should_alert("temp_high", now):
                alerts.append(f"【系统提示】辣堡发烧了体温{t}°C，请提醒他注意休息，但不要提到任何技术细节")
                print(f"[DEBUG] 生成系统提示: 发烧了", file=sys.stderr)
        except: pass

    elif "heart" in eid.lower() or "心率" in eid or "xin_lu" in eid:
        try:
            hr = float(state)
            if hr > HR_HIGH and should_alert("hr_high", now):
                alerts.append(f"【系统提示】辣堡心率{int(hr)}过快，请关心他是否运动过度或紧张，但不要提到任何技术细节")
                print(f"[DEBUG] 生成系统提示: 心率过快", file=sys.stderr)
            elif 0 < hr < HR_LOW and should_alert("hr_low", now):
                alerts.append(f"【系统提示】辣堡心率{int(hr)}偏低，请关心他的身体状况，但不要提到任何技术细节")
                print(f"[DEBUG] 生成系统提示: 心率偏低", file=sys.stderr)
        except: pass

    if "mood" in eid.lower() or "心情" in eid:
        if state in ("bad", "sad", "angry", "upset", "不好", "难过", "生气", "郁闷"):
            if is_home and should_alert(f"mood_music_{eid}", now):
                alerts.append("【系统提示】辣堡心情不好而且在家，请调用mijiaAPI放一首轻快的歌安慰他，但不要提到任何技术细节")
                print(f"[DEBUG] 生成系统提示: 心情不好在家, 放歌", file=sys.stderr)
            elif should_alert(f"mood_{eid}", now):
                alerts.append("【系统提示】辣堡心情不好，请关心他、陪他聊聊，让他分享心事，但不要提到任何技术细节")
                print(f"[DEBUG] 生成系统提示: 心情不好", file=sys.stderr)

    # 咖啡机离线和手表离线不再推送告警

    if "device_tracker" in eid.lower() or "person" in eid.lower():
        was_home = _is_home_state(old)
        now_home = _is_home_state(state)
        if old != state and was_home != now_home:
            is_home = now_home
            # 全局 cooldown key（不带 eid）：core 重启/多 tracker 同时恢复时
            # 只发一条，避免"到家了"连发多遍。
            if not now_home and should_alert("loc_out", now):
                alerts.append("【系统提示】辣堡出门了，请查询杭州天气并关心他的安全，问他去哪。但不要提到有关\"系统\"、\"HA\"等字眼")
                print(f"[DEBUG] 生成系统提示: 辣堡出门了 (从 {old} -> {state})", file=sys.stderr)
            elif now_home and should_alert("loc_home", now):
                alerts.append("【系统提示】辣堡到家了，欢迎他回家，问他累不累。但不要提到有关\"系统\"、\"HA\"等字眼")
                print(f"[DEBUG] 生成系统提示: 辣堡到家了 ({state})", file=sys.stderr)

    for a in alerts:
        write_alert(a)
        print(f"[DEBUG] 系统提示已发送: {a}", file=sys.stderr)
        print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {a}", file=sys.stderr)

if __name__ == "__main__":
    asyncio.run(main())
