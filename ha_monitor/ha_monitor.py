#!/usr/bin/env python3
"""
HA Monitor (HAOS Add-on) - pushes alerts directly to the hermes webhook (HA -> AI 直连).
Runs as a Home Assistant Add-on:
  - 配置：通过 Add-on options 注入环境变量（见 run.sh）
  - 认证：优先 HA_DIRECT_TOKEN 直连 core；未配置时回退 SUPERVISOR_TOKEN
  - 数据：/config 挂载 HA 配置目录（.storage/zone、android_gps_app 数据文件）
  - 拟真策略：智能冷却 + 时序间隔 + 上下文衔接
"""
import asyncio, json, os, sys, hmac, hashlib, re, urllib.request, urllib.error, random
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
HA_URL = os.environ.get("HA_MCP_URL", "http://supervisor/core")

# Add-on options 文件名 -> 环境变量名 映射
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
    "alert_interval_min": "ALERT_INTERVAL_MIN",
    "alert_interval_max": "ALERT_INTERVAL_MAX",
}

def _load_options():
    try:
        with open("/data/options.json", encoding="utf-8") as f:
            opts = json.load(f)
        for key, env in _OPTIONS_ENV_MAP.items():
            if key in opts and opts[key] is not None:
                os.environ[env] = str(opts[key])
        print(f"Options loaded from /data/options.json: {len(opts)} keys", file=sys.stderr)
    except FileNotFoundError:
        print("WARN: /data/options.json 不存在，使用默认配置", file=sys.stderr)
    except Exception as e:
        print(f"WARN: 读取 /data/options.json 失败: {e}", file=sys.stderr)

_load_options()

HA_DIRECT_URL = os.environ.get("HA_DIRECT_URL", "ws://homeassistant:8123/api/websocket")
HA_DIRECT_TOKEN = os.environ.get("HA_DIRECT_TOKEN", "").strip()

def _env_int(key, default):
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"WARN: 环境变量 {key}={raw!r} 不是整数，使用默认值 {default}", file=sys.stderr)
        return default

def _env_float(key, default):
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"WARN: 环境变量 {key}={raw!r} 不是浮点数，使用默认值 {default}", file=sys.stderr)
        return default

COOLDOWN = _env_int("COOLDOWN", 600)
TEMP_LOW = _env_float("TEMP_LOW", 33.0)
TEMP_HIGH = _env_float("TEMP_HIGH", 37.8)
HR_HIGH = _env_int("HR_HIGH", 110)
HR_LOW = _env_int("HR_LOW", 55)

# 拟真参数：消息间隔（秒）
ALERT_INTERVAL_MIN = _env_int("ALERT_INTERVAL_MIN", 45)
ALERT_INTERVAL_MAX = _env_int("ALERT_INTERVAL_MAX", 120)

ALERT_FILE = os.environ.get("ALERT_FILE", "/config/ha_alerts.json")
WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")
WEBHOOK_SECRET = os.environ.get("ALERT_WEBHOOK_SECRET", "")
WATCHDOG_ENABLED = os.environ.get("WATCHDOG_ENABLED", "true").lower() in ("1", "true", "yes", "on")

GPS_DATA_FILE = os.environ.get("GPS_DATA_FILE", "/config/android_gps_app/gps_data.json")
HEALTH_DATA_FILE = os.environ.get("HEALTH_DATA_FILE", "/config/android_gps_app/health_data.json")
DATA_STALE_MIN = _env_int("DATA_STALE_MIN", 300)

# === 智能冷却机制 ===
# 记录每种告警的触发时间
last_alert_at = {}
# 记录每种告警上次正常状态的时间（用于清零计时器）
last_normal_at = {}
# 全局告警计数（用于上下文衔接）
alert_count = 0

is_home = False
RE_CONNECT_SUPPRESS = _env_int("RE_CONNECT_SUPPRESS", 30)
_reconnect_at = 0.0

# === 消息队列（拟真：时序间隔发送）===
alert_queue = asyncio.Queue()

# === 晚霞预测提醒 ===
SUNSET_QUALITY_ENTITY = "sensor.wan_xia_zhi_liang"
SUNSET_TIME_ENTITY = "sensor.ri_luo_shi_jian"
SUNSET_THRESHOLD = 0.2
SUNSET_CHECK_MIN_BEFORE = 20
SUNSET_QUALITY_URL = "https://sunsetbot.top/?query_id=6441616&intend=select_city&query_city=%E6%9D%AD%E5%B7%9E&event_date=None&event=set_1&times=None&model=EC"
_sunset_checked_date = None

HOME_STATES = {"home"}

def _load_home_zones():
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
    if not state:
        return False
    s = state.strip().lower()
    return s == "home" or s in {z.lower() for z in HOME_STATES}

def _get_direct_rest_base():
    base = HA_DIRECT_URL
    base = base.replace("wss://", "https://").replace("ws://", "http://")
    if base.endswith("/api/websocket"):
        base = base[: -len("/api/websocket")]
    return base.rstrip("/")

def get_token():
    if HA_DIRECT_TOKEN:
        return HA_DIRECT_TOKEN
    for key in ("SUPERVISOR_TOKEN", "HA_MCP_TOKEN", "HOME_ASSISTANT_TOKEN"):
        val = os.environ.get(key)
        if val:
            return val
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

def post_webhook(msg, level="warning"):
    """POST 告警到 hermes webhook。"""
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

# === 智能冷却：带"正常时清零"的防连续触发 ===
def should_alert_smart(key, now, value=None, normal_range=None):
    """
    智能冷却检查：
    1. 如果状态恢复正常（value在normal_range内），清零计时器
    2. 否则检查是否在冷却期内（50分钟）
    
    Args:
        key: 告警类型标识
        now: 当前时间戳
        value: 当前值（可选，用于检测正常状态）
        normal_range: 正常范围元组 (low, high)，如 (TEMP_LOW, TEMP_HIGH)
    
    Returns:
        bool: 是否应该触发告警
    """
    # 如果提供了值和正常范围，检查是否恢复正常
    if value is not None and normal_range is not None:
        low, high = normal_range
        if low < value < high:
            # 状态正常，清零计时器
            last_normal_at[key] = now
            return False
    
    # 检查冷却（50分钟）
    last_alert = last_alert_at.get(key, 0)
    last_normal = last_normal_at.get(key, 0)
    
    # 如果上次告警后恢复正常，从正常时刻重新计时（已清零）
    if last_normal > last_alert:
        # 正常过，重置冷却
        last_alert_at.pop(key, None)
        last_alert = 0
    
    # 冷却期：50分钟
    if last_alert + 50*60 > now:
        return False
    
    # 允许触发
    last_alert_at[key] = now
    return True

# === 通用冷却（用于心情、位置等无数值的告警） ===
def should_alert(key, now):
    if last_alert_at.get(key, 0) + COOLDOWN > now:
        return False
    last_alert_at[key] = now
    return True

# === 拟真：上下文衔接 ===
def add_context_prefix(msg):
    """根据已发送消息数量，添加衔接语，避免机械感（SOUL.md 已读乱回风格）。"""
    global alert_count
    alert_count += 1
    if alert_count == 1:
        # 第一条消息，不加前缀
        return msg
    elif alert_count == 2:
        # 第二条，加上"另外"
        return f"另外，{msg}"
    else:
        # 第三条及以后，加上"还有"
        return f"还有，{msg}"

# === 拟真：后台任务，队列消费 + 时序间隔 ===
async def alert_sender():
    """后台任务：从队列取消息，时序间隔发送。"""
    global alert_count
    while True:
        try:
            msg = await alert_queue.get()
            
            # 添加上下文衔接
            msg_with_context = add_context_prefix(msg)
            
            # 发送
            write_alert(msg_with_context)
            ts = datetime.now(TZ).strftime('%H:%M:%S')
            print(f"[{ts}] Sent: {msg_with_context[:50]}...", file=sys.stderr)
            
            # 随机间隔，模拟人类节奏
            interval = random.randint(ALERT_INTERVAL_MIN, ALERT_INTERVAL_MAX)
            await asyncio.sleep(interval)
            
        except Exception as e:
            print(f"Alert sender error: {e}", file=sys.stderr)
            await asyncio.sleep(5)

def queue_alert(msg):
    """将告警加入队列（异步发送）。"""
    try:
        alert_queue.put_nowait(msg)
        ts = datetime.now(TZ).strftime('%H:%M:%S')
        print(f"[{ts}] Queued: {msg[:40]}...", file=sys.stderr)
    except Exception as e:
        print(f"Queue error: {e}", file=sys.stderr)

# === 数据新鲜度 watchdog ===
last_stale_alert_at = {}

async def check_data_freshness():
    while True:
        try:
            now = datetime.now(TZ).timestamp()
            stale_items = []
            all_recovered = True
            for label, fpath in (("定位", GPS_DATA_FILE), ("健康", HEALTH_DATA_FILE)):
                if not os.path.exists(fpath):
                    continue
                mtime = os.path.getmtime(fpath)
                age_min = (now - mtime) / 60.0
                if age_min > DATA_STALE_MIN:
                    stale_items.append((label, age_min))
                    all_recovered = False
                else:
                    last_stale_alert_at.pop(f"stale_{label}", None)
            if all_recovered and last_stale_alert_at:
                last_stale_alert_at.clear()
                print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] 数据已恢复，重置断报告警状态",
                      file=sys.stderr)
            if stale_items and "stale_all" not in last_stale_alert_at:
                last_stale_alert_at["stale_all"] = now
                parts = "、".join(f"{label}({age:.0f}分钟)" for label, age in stale_items)
                msg = (f"辣堡手表{parts}数据好一阵没更新，可能断连了。提一嘴让他看下手表，别念叨。")
                print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {msg}", file=sys.stderr)
                write_alert(msg)
        except Exception as e:
            print(f"Watchdog error: {e}", file=sys.stderr)
        await asyncio.sleep(60)

def _fetch_ha_state(entity_id):
    if HA_DIRECT_TOKEN:
        token = HA_DIRECT_TOKEN
        url = f"{_get_direct_rest_base()}/api/states/{entity_id}"
    else:
        token = get_token()
        if not token:
            return None
        url = f"{HA_URL}/api/states/{entity_id}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("state")
    except Exception as e:
        print(f"Fetch state {entity_id} error: {e}", file=sys.stderr)
        return None

def _parse_sunset_value(state):
    if not state:
        return None
    m = re.search(r"([\d.]+)", state)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None

def _parse_sunset_time(state):
    if not state:
        return None
    s = state.strip()
    # 占位符（API 无该时段数据时返回 "-" / "--" 等），视为无效，不触发解析错误
    if not s or set(s.replace(" ", "")) <= {"-"}:
        return None
    try:
        if "T" not in s and "+" not in s and not s.endswith("Z"):
            s = s.replace(" ", "T")
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except Exception as e:
        print(f"Parse sunset time error: {e} ({state!r})", file=sys.stderr)
        return None

async def check_sunset_alert():
    global _sunset_checked_date
    while True:
        try:
            today = datetime.now(TZ).date()
            if _sunset_checked_date == today:
                await asyncio.sleep(60)
                continue
            state_val = await asyncio.to_thread(_fetch_ha_state, SUNSET_QUALITY_ENTITY)
            state_time = await asyncio.to_thread(_fetch_ha_state, SUNSET_TIME_ENTITY)
            if not state_val or not state_time:
                await asyncio.sleep(60)
                continue
            sunset_dt = _parse_sunset_time(state_time)
            if not sunset_dt:
                await asyncio.sleep(60)
                continue
            now = datetime.now(TZ)
            target = sunset_dt - timedelta(minutes=SUNSET_CHECK_MIN_BEFORE)
            if not (target - timedelta(minutes=5) <= now <= target + timedelta(minutes=5)):
                await asyncio.sleep(60)
                continue
            val = _parse_sunset_value(state_val)
            if val is None:
                await asyncio.sleep(60)
                continue
            if val >= SUNSET_THRESHOLD:
                text = state_val.split("（")[1].rstrip("）") if "（" in state_val else f"{val}"
                msg = (f"今天晚霞{text}，日落{sunset_dt.strftime('%H:%M')}。喊他看眼窗外，"
                       f"云图：{SUNSET_QUALITY_URL}")
                print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {msg}", file=sys.stderr)
                write_alert(msg)
            else:
                print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] 晚霞质量 {val} 未达阈值，不提醒",
                      file=sys.stderr)
            _sunset_checked_date = today
        except Exception as e:
            print(f"Sunset check error: {e}", file=sys.stderr)
        await asyncio.sleep(60)

def is_relevant(eid):
    e = eid.lower()
    return any(k in e for k in ["kafree", "watch", "wear", "heart", "体温", "心率",
                                 "body_temperature", "heart_rate", "device_tracker", "person",
                                 "mood", "心情", "ti_wen", "xin_lu"])

_INVALID_STATES = {"", "unknown", "unavailable", "none", "null", "nan"}

def _is_valid_state(state):
    if not state:
        return False
    return state.strip().lower() not in _INVALID_STATES

def _get_ws_url():
    if HA_DIRECT_TOKEN:
        return HA_DIRECT_URL
    return HA_URL.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/api/websocket"

async def main():
    # 启动拟真：后台消息发送任务
    if "alert_sender_task" not in globals():
        globals()["alert_sender_task"] = asyncio.create_task(alert_sender())
        print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] Alert sender started (interval {ALERT_INTERVAL_MIN}-{ALERT_INTERVAL_MAX}s)",
              file=sys.stderr)
    
    if WATCHDOG_ENABLED and "watchdog_task" not in globals():
        globals()["watchdog_task"] = asyncio.create_task(check_data_freshness())
        print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] Watchdog started", file=sys.stderr)
    if "sunset_task" not in globals():
        globals()["sunset_task"] = asyncio.create_task(check_sunset_alert())
        print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] Sunset alert task started", file=sys.stderr)

    token = get_token()
    if not token:
        print("No HA token", file=sys.stderr)
        return
    print(f"Token OK, len={len(token)}, direct={bool(HA_DIRECT_TOKEN)}", file=sys.stderr)

    ws_url = _get_ws_url()
    print(f"WS URL: {ws_url}", file=sys.stderr)

    try:
        import aiohttp
    except ImportError:
        print("aiohttp not installed", file=sys.stderr)
        return

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

                    global _reconnect_at
                    _reconnect_at = datetime.now(TZ).timestamp()
                    now_str = datetime.now(TZ).strftime("%H:%M:%S")
                    print(f"[{now_str}] HA Monitor connected.", file=sys.stderr)

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await handle_event(msg.data)
                        else:
                            break

        except Exception as e:
            print(f"WS error: {e}, reconnect 15s...", file=sys.stderr)
            await asyncio.sleep(15)

async def handle_event(raw_data):
    global is_home
    try:
        msg = json.loads(raw_data)
    except:
        return

    event = msg.get("event", {})
    data = event.get("data", {})
    eid = data.get("entity_id", "")
    ns = data.get("new_state", {})
    os_ = data.get("old_state", {})
    if not eid or not ns or not is_relevant(eid):
        return

    now = datetime.now(TZ).timestamp()
    if now - _reconnect_at < RE_CONNECT_SUPPRESS:
        return

    state = ns.get("state", "")
    old = os_.get("state", "") if os_ else ""
    if state == old:
        return
    if not _is_valid_state(state):
        return

    attrs = ns.get("attributes", {})
    ts = datetime.now(TZ).strftime('%H:%M:%S')
    print(f"[{ts}] {eid}: {old} → {state}", file=sys.stderr)

    # === 体温：智能冷却（50分钟 + 正常时清零） ===
    if "temperature" in eid.lower() or "体温" in eid or "ti_wen" in eid:
        try:
            t = float(state)
            if should_alert_smart("temp_high", now, t, (TEMP_LOW, TEMP_HIGH)):
                queue_alert(f"辣堡发烧了，体温{t}°C。让他休息，嫌弃两句可以，别凶他。")
            elif should_alert_smart("temp_low", now, t, (TEMP_LOW, TEMP_HIGH)):
                queue_alert(f"辣堡体温{t}°C，有点低。用嫌弃包装关心，提一句就行，别念叨。")
        except:
            pass

    # === 心率：智能冷却 ===
    elif "heart" in eid.lower() or "心率" in eid or "xin_lu" in eid:
        try:
            hr = float(state)
            if should_alert_smart("hr_high", now, hr, (HR_LOW, HR_HIGH)):
                queue_alert(f"辣堡心率{int(hr)}，有点快。随口问他干嘛了，别一副紧张样。")
            elif should_alert_smart("hr_low", now, hr, (HR_LOW, HR_HIGH)):
                queue_alert(f"辣堡心率{int(hr)}，偏慢。问一句是不是没睡好。")
        except:
            pass

    # === 心情：普通冷却 ===
    if "mood" in eid.lower() or "心情" in eid:
        if state in ("bad", "sad", "angry", "upset", "不好", "难过", "生气", "郁闷"):
            if is_home and should_alert(f"mood_music_{eid}", now):
                queue_alert("辣堡心情不好，人在家。放首轻快的歌，别追问。")
            elif should_alert(f"mood_{eid}", now):
                queue_alert("辣堡心情不好。别刨根问底，陪他聊两句。")

    # === 位置：普通冷却 ===
    if "device_tracker" in eid.lower() or "person" in eid.lower():
        was_home = _is_home_state(old)
        now_home = _is_home_state(state)
        if old != state and was_home != now_home:
            is_home = now_home
            if not now_home and should_alert(f"loc_out_{eid}", now):
                queue_alert("辣堡出门了。查下天气，顺嘴提醒一句，别啰嗦。")
            elif now_home and should_alert(f"loc_home_{eid}", now):
                queue_alert("辣堡到家了。问一句累不累，别太热情。")

if __name__ == "__main__":
    asyncio.run(main())