"""Constants for HA Monitor integration."""

DOMAIN = "ha_monitor"

# Configuration keys
CONF_ENTITIES = "entities"
CONF_TEMP_LOW = "temp_low"
CONF_TEMP_HIGH = "temp_high"
CONF_HR_HIGH = "hr_high"
CONF_HR_LOW = "hr_low"
CONF_ALERTS_FILE = "alerts_file"
CONF_COOLDOWN = "cooldown"
CONF_WEBHOOK_URL = "webhook_url"
CONF_WEBHOOK_SECRET = "webhook_secret"
CONF_WATCHDOG_ENABLED = "watchdog_enabled"
CONF_STALE_MIN = "stale_min"

# Default values
DEFAULT_TEMP_LOW = 33.0
DEFAULT_TEMP_HIGH = 37.8
DEFAULT_HR_HIGH = 110
DEFAULT_HR_LOW = 55
DEFAULT_ALERTS_FILE = "ha_alerts.json"
DEFAULT_COOLDOWN = 600  # 10 minutes
DEFAULT_WEBHOOK_URL = ""
DEFAULT_WEBHOOK_SECRET = ""
DEFAULT_WATCHDOG_ENABLED = True
DEFAULT_STALE_MIN = 300  # 超过 5 小时未更新视为断报

# Watchdog data files (HA 集成 android_gps_app 写入)
GPS_DATA_FILE = "/homeassistant/android_gps_app/gps_data.json"
HEALTH_DATA_FILE = "/homeassistant/android_gps_app/health_data.json"
ZONE_STORAGE_FILE = "/config/.storage/zone"  # HA zone 定义（支持多套房子）

# Relevant entity keywords
RELEVANT_KEYWORDS = [
    "temperature", "heart_rate", "mood",
    "体温", "心率", "心情",
    "ti_wen", "xin_lu",
    "device_tracker", "person"
]

# Bad mood states
BAD_MOOD_STATES = ["bad", "sad", "angry", "upset", "不好", "难过", "生气", "郁闷"]
