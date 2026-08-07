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

# burst 上下文窗口：超过该时间无消息则重置衔接计数（分钟）
BURST_GAP_MIN = _env_int("BURST_GAP_MIN", 5)

ALERT_FILE = os.environ.get("ALERT_FILE", "/config/ha_alerts.json")
WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "http://192.168.0.41:8644/webhooks/ha_alerts")
WEBHOOK_SECRET = os.environ.get("ALERT_WEBHOOK_SECRET", "")
WATCHDOG_ENABLED = os.environ.get("WATCHDOG_ENABLED", "true").lower() in ("1", "true", "yes", "on")

GPS_DATA_FILE = os.environ.get("GPS_DATA_FILE", "/config/android_gps_app/gps_data.json")
HEALTH_DATA_FILE = os.environ.get("HEALTH_DATA_FILE", "/config/android_gps_app/health_data.json")
DATA_STALE_MIN = _env_int("DATA_STALE_MIN", 300)

# === 状态锁机制 ===
# 同一异常只提醒一次，直到状态恢复正常才解锁
_alert_lock = {}

# burst 上下文：记录最近一次发送时间 + 本次 burst 内已发条数
_last_alert_ts = 0
_burst_count = 0

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

# === 状态锁工具函数 ===
def is_locked(key):
    return _alert_lock.get(key, False)


def lock(key):
    _alert_lock[key] = True


def unlock(key):
    _alert_lock.pop(key, None)


# === 文案池：同类告警用不同措辞，避免机械重复 ===
_MESSAGE_TEMPLATES = {
    "temp_high": [
        "辣堡体温{t}°C，有点烧。让他歇会儿，别一直催他。",
        "体温{t}°C，辣堡怕不是着凉了/捂热了。随口问一句就行。",
    ],
    "temp_low": [
        "辣堡体温{t}°C，有点低。用嫌弃包装关心，提一句就行，别念叨。",
        "体温{t}°C，辣堡是不是穿少了？嫌弃地提醒一下。",
    ],
    "hr_high": [
        "辣堡心率{hr}，有点快。随口问他干嘛了，别一副紧张样。",
        "心率{hr}，辣堡刚运动完还是咋的？顺嘴问一句。",
    ],
    "hr_low": [
        "辣堡心率{hr}，偏慢。问一句是不是没睡好。",
        "心率{hr}，辣堡是不是在睡觉？别大惊小怪，提一句。",
    ],
    "stale": [
        "辣堡手表{parts}数据好久没更新了，可能断连。让他看一眼手表，别反复说。",
        "手表{parts}没动静了，辣堡是不是没开 APP？提醒一次就行。",
    ],
    "mood_bad_home": [
        "辣堡心情不好，人在家。放首轻快的歌，别追问。",
        "他情绪不太对，在家。挑首歌缓缓气氛，别去审他。",
    ],
    "mood_bad": [
        "辣堡心情不好。别刨根问底，陪他聊两句。",
        "他今天不太开心，有空随口问一句，别讲大道理。",
    ],
    "loc_out": [
        "辣堡出门了。查下天气，顺嘴提醒一句，别啰嗦。",
        "他出去了。瞄一眼要不要带伞/添衣，轻点提醒。",
    ],
    "loc_home": [
        "辣堡到家了。问一句累不累，别太热情。",
        "他回来了。随口说句回来了就行，别围着他转。",
    ],
}


def message_for(key, **kwargs):
    """按 key 随机返回一条文案并格式化。"""
    templates = _MESSAGE_TEMPLATES.get(key, [])
    if not templates:
        return ""
    return random.choice(templates).format(**kwargs)


# === 拟真：上下文衔接（burst 模式） ===
def add_context_prefix(msg):
    """
    只有在短时间内连续发送的消息才加衔接语。
    超过 BURST_GAP_MIN 则视为新会话，避免隔很久还来一句"还有"。
    """
    global _last_alert_ts, _burst_count
    now = datetime.now(TZ).timestamp()
    gap = BURST_GAP_MIN * 60
    if now - _last_alert_ts > gap:
        _burst_count = 0
    _burst_count += 1
    _last_alert_ts = now
    if _burst_count == 1:
        return msg
    if _burst_count == 2:
        return f"另外，{msg}"
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

# === 数据新鲜度 watchdog（状态锁模式） ===
async def check_data_freshness():
    while True:
        try:
            now = datetime.now(TZ).timestamp()
            stale_items = []
            any_recovered = False
            for label, fpath in (("定位", GPS_DATA_FILE), ("健康", HEALTH_DATA_FILE)):
                if not os.path.exists(fpath):
                    continue
                mtime = os.path.getmtime(fpath)
                age_min = (now - mtime) / 60.0
                if age_min > DATA_STALE_MIN:
                    stale_items.append((label, age_min))
                else:
                    if is_locked(f"stale_{label}"):
                        any_recovered = True
                        unlock(f"stale_{label}")
            # 只要有一个从 stale 恢复，也重置总锁，允许下次整体断连时再报
            if any_recovered and is_locked("stale_all"):
                unlock("stale_all")
                print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] 手表数据恢复，重置断连提醒锁",
                      file=sys.stderr)
            if stale_items and not is_locked("stale_all"):
                lock("stale_all")
                for label, _ in stale_items:
                    lock(f"stale_{label}")
                parts = "、".join(f"{label}({age:.0f}分钟)" for label, age in stale_items)
                msg = message_for("stale", parts=parts)
                print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {msg}", file=sys.stderr)
                queue_alert(msg)
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

    # === 体温：状态锁（异常一次 + 恢复正常解锁） ===
    if "temperature" in eid.lower() or "体温" in eid or "ti_wen" in eid:
        try:
            t = float(state)
            if TEMP_LOW < t < TEMP_HIGH:
                # 恢复正常，解锁
                unlock("temp_high")
                unlock("temp_low")
            elif t >= TEMP_HIGH and not is_locked("temp_high"):
                lock("temp_high")
                unlock("temp_low")
                queue_alert(message_for("temp_high", t=t))
            elif t <= TEMP_LOW and not is_locked("temp_low"):
                lock("temp_low")
                unlock("temp_high")
                queue_alert(message_for("temp_low", t=t))
        except:
            pass

    # === 心率：状态锁 ===
    elif "heart" in eid.lower() or "心率" in eid or "xin_lu" in eid:
        try:
            hr = float(state)
            if HR_LOW < hr < HR_HIGH:
                unlock("hr_high")
                unlock("hr_low")
            elif hr >= HR_HIGH and not is_locked("hr_high"):
                lock("hr_high")
                unlock("hr_low")
                queue_alert(message_for("hr_high", hr=int(hr)))
            elif hr <= HR_LOW and not is_locked("hr_low"):
                lock("hr_low")
                unlock("hr_high")
                queue_alert(message_for("hr_low", hr=int(hr)))
        except:
            pass

    # === 心情：状态锁 ===
    if "mood" in eid.lower() or "心情" in eid:
        bad_states = {"bad", "sad", "angry", "upset", "不好", "难过", "生气", "郁闷"}
        good_states = {"good", "happy", "fine", "ok", "好", "开心", "不错", "平静"}
        if state in bad_states and not is_locked(f"mood_{eid}"):
            lock(f"mood_{eid}")
            key = "mood_bad_home" if is_home else "mood_bad"
            queue_alert(message_for(key))
        elif state in good_states and is_locked(f"mood_{eid}"):
            unlock(f"mood_{eid}")

    # === 位置：状态锁 ===
    if "device_tracker" in eid.lower() or "person" in eid.lower():
        was_home = _is_home_state(old)
        now_home = _is_home_state(state)
        if old != state and was_home != now_home:
            is_home = now_home
            if not now_home and not is_locked(f"loc_out_{eid}"):
                lock(f"loc_out_{eid}")
                unlock(f"loc_home_{eid}")
                queue_alert(message_for("loc_out"))
            elif now_home and not is_locked(f"loc_home_{eid}"):
                lock(f"loc_home_{eid}")
                unlock(f"loc_out_{eid}")
                queue_alert(message_for("loc_home"))

if __name__ == "__main__":
    asyncio.run(main())