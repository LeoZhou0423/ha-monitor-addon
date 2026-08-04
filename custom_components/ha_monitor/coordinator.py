"""DataUpdateCoordinator for HA Monitor.

将原 /config/ha_monitor.py 脚本的全部能力移植为 HA 集成：
- 多 zone 在家判断（支持多套房子，动态读取 HA zone 配置）
- 出门/到家/心情/体温/心率告警
- webhook 直连推送（HMAC 签名，米糊实时收到）
- 数据新鲜度 watchdog（断报只推一次，数据恢复前不重复）
- 晚霞预测提醒（日落前20分钟检测，阈值触发推送）
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    BAD_MOOD_STATES,
    CONF_ALERTS_FILE,
    CONF_COOLDOWN,
    CONF_ENTITIES,
    CONF_HR_HIGH,
    CONF_HR_LOW,
    CONF_STALE_MIN,
    CONF_TEMP_HIGH,
    CONF_TEMP_LOW,
    CONF_WATCHDOG_ENABLED,
    CONF_WEBHOOK_SECRET,
    CONF_WEBHOOK_URL,
    DEFAULT_ALERTS_FILE,
    DEFAULT_COOLDOWN,
    DEFAULT_HR_HIGH,
    DEFAULT_HR_LOW,
    DEFAULT_STALE_MIN,
    DEFAULT_TEMP_HIGH,
    DEFAULT_TEMP_LOW,
    DEFAULT_WATCHDOG_ENABLED,
    DEFAULT_WEBHOOK_SECRET,
    DEFAULT_WEBHOOK_URL,
    DOMAIN,
    GPS_DATA_FILE,
    HEALTH_DATA_FILE,
    RELEVANT_KEYWORDS,
    ZONE_STORAGE_FILE,
)

_LOGGER = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=8))


class HaMonitorCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to manage HA Monitor data."""

    def __init__(self, hass: HomeAssistant, config_entry: Any) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=30),
        )
        self.config_entry = config_entry
        self.alerts: list[dict[str, Any]] = []
        self._last_alert_at: dict[str, float] = {}
        self._is_home: bool = False
        # 断报只推一次的标记：stale_{label} 推送过之后，数据恢复前不再重复推
        self._stale_alerted: set[str] = set()
        self._watchdog_task: Any = None
        self._sunset_check_task: Any = None  # 晚霞检查定时任务
        self._last_sunset_alert_date: str = ""  # 防止同一天重复推送

        # Get config values
        data = config_entry.data
        self._temp_low = data.get(CONF_TEMP_LOW, DEFAULT_TEMP_LOW)
        self._temp_high = data.get(CONF_TEMP_HIGH, DEFAULT_TEMP_HIGH)
        self._hr_high = data.get(CONF_HR_HIGH, DEFAULT_HR_HIGH)
        self._hr_low = data.get(CONF_HR_LOW, DEFAULT_HR_LOW)
        self._alerts_file = data.get(CONF_ALERTS_FILE, DEFAULT_ALERTS_FILE)
        self._cooldown = data.get(CONF_COOLDOWN, DEFAULT_COOLDOWN)
        self._webhook_url = data.get(CONF_WEBHOOK_URL, DEFAULT_WEBHOOK_URL)
        self._webhook_secret = data.get(CONF_WEBHOOK_SECRET, DEFAULT_WEBHOOK_SECRET)
        self._watchdog_enabled = data.get(CONF_WATCHDOG_ENABLED, DEFAULT_WATCHDOG_ENABLED)
        self._stale_min = data.get(CONF_STALE_MIN, DEFAULT_STALE_MIN)
        self._monitored_entities = set(data.get(CONF_ENTITIES, []))

        # 家的状态集合：默认 "home" + 所有非 passive zone 名（动态读取）
        self._home_states: set[str] = self._load_home_zones()

    # ------------------------------------------------------------------
    # 多 zone 在家判断
    # ------------------------------------------------------------------
    def _load_home_zones(self) -> set[str]:
        """读取 HA zone 配置，返回所有非 passive zone 的名称集合。"""
        home_states = {"home"}
        try:
            with open(ZONE_STORAGE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("data", {}).get("items", []):
                if not item.get("passive", False) and item.get("name"):
                    home_states.add(item["name"])
        except Exception as e:
            _LOGGER.warning("Load zones error: %s", e)
        return home_states

    def _is_home_state(self, state: str | None) -> bool:
        """state 是否表示在家（默认 home 或任一非 passive zone）。"""
        if not state:
            return False
        s = state.strip().lower()
        return s == "home" or s in {z.lower() for z in self._home_states}

    # ------------------------------------------------------------------
    # Webhook 直连推送
    # ------------------------------------------------------------------
    def _post_webhook(self, msg: str, level: str = "warning") -> bool:
        """POST 告警到米糊 webhook（HMAC 签名）。成功返回 True。"""
        try:
            body = json.dumps({
                "type": "alert",
                "alert": {"msg": msg, "level": level, "ts": datetime.now(TZ).isoformat()}
            }, ensure_ascii=False).encode("utf-8")
            sig = hmac.new(self._webhook_secret.encode(), body, hashlib.sha256).hexdigest()
            req = urllib.request.Request(
                self._webhook_url, data=body, method="POST",
                headers={"Content-Type": "application/json", "X-HA-Signature": sig},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception as e:
            _LOGGER.error("Webhook post error: %s", e)
            return False

    def _write_alert(self, msg: str) -> None:
        """推送到 webhook；失败时兜底写入本地 JSON 文件（防丢）。"""
        # 直连 webhook 优先
        if self._post_webhook(msg):
            _LOGGER.info("Alert sent via webhook: %s", msg[:60])
            return

        # webhook 失败 → 写入文件兜底
        try:
            self.alerts.append({"msg": msg, "ts": datetime.now(TZ).isoformat()})
            self.alerts = self.alerts[-10:]
            alerts_path = self.hass.config.path(self._alerts_file)
            with open(alerts_path, "w", encoding="utf-8") as f:
                json.dump(self.alerts, f, ensure_ascii=False)
            _LOGGER.info("Alert queued to file: %s", msg[:60])
        except Exception as e:
            _LOGGER.error("Write alert error: %s", e)

    # ------------------------------------------------------------------
    # 冷却判断
    # ------------------------------------------------------------------
    def _should_alert(self, key: str) -> bool:
        """Check if alert should be sent based on cooldown."""
        now = datetime.now(TZ).timestamp()
        if self._last_alert_at.get(key, 0) + self._cooldown > now:
            return False
        self._last_alert_at[key] = now
        return True

    # ------------------------------------------------------------------
    # 晚霞预测提醒（日落前20分钟检测）
    # ------------------------------------------------------------------
    def _get_sunset_quality_value(self) -> float | None:
        """从 sensor.wan_xia_zhi_liang 提取晚霞质量数值。

        返回值：
            float | None: 晚霞质量数值（如 0.208），解析失败返回 None

        传感器状态示例："0.208（小烧到中烧）"
        """
        try:
            state = self.hass.states.get("sensor.wan_xia_zhi_liang")
            if not state or not state.state:
                return None

            # 提取数字部分（支持 "0.208（小烧到中烧）" 格式）
            match = re.search(r"([\d.]+)", state.state)
            if match:
                return float(match.group(1))
        except Exception as e:
            _LOGGER.error("Get sunset quality error: %s", e)
        return None

    def _get_sunset_time(self) -> datetime | None:
        """从 sensor.ri_luo_shi_jian 获取日落时间。

        返回值：
            datetime | None: 日落时间，解析失败返回 None

        传感器状态示例："2026-08-04 18:49:31"
        """
        try:
            state = self.hass.states.get("sensor.ri_luo_shi_jian")
            if not state or not state.state:
                return None

            # 解析时间格式 "2026-08-04 18:49:31"
            return datetime.strptime(state.state, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        except Exception as e:
            _LOGGER.error("Get sunset time error: %s", e)
        return None

    async def _check_sunset_quality(self) -> None:
        """在日落前20分钟检查晚霞质量，达到阈值则推送提醒。

        阈值判断：
            - 大烧：>= 0.6
            - 中烧：>= 0.4
            - 小烧：>= 0.2
            - 无烧：< 0.2

        推送条件：>= 0.2（小烧及以上）
        """
        import asyncio

        _LOGGER.warning("🌅 Sunset check task started")  # 启动时输出WARNING级别日志

        while True:
            try:
                now = datetime.now(TZ)
                today_str = now.strftime("%Y-%m-%d")

                # 防止同一天重复推送
                if self._last_sunset_alert_date == today_str:
                    await asyncio.sleep(60)
                    continue

                # 获取日落时间
                sunset_time = self._get_sunset_time()
                if not sunset_time:
                    _LOGGER.warning("⚠️ Sunset time not available yet, waiting...")
                    await asyncio.sleep(60)
                    continue

                # 计算检查时间：日落前20分钟
                check_time = sunset_time - timedelta(minutes=20)

                # 每分钟输出一次状态（WARNING级别，便于调试）
                time_diff = (check_time - now.replace(tzinfo=None)).total_seconds()
                if now.second == 0:  # 只在整分时输出
                    _LOGGER.warning("⏰ Sunset check: now=%s, check=%s, diff=%.0fs",
                                   now.strftime("%H:%M:%S"),
                                   check_time.strftime("%H:%M:%S"),
                                   time_diff)

                # 判断是否到达检查时间（±60秒容差）
                if abs((now - check_time).total_seconds()) <= 60:
                    _LOGGER.info("Sunset check triggered at %s (sunset at %s)",
                                now.strftime("%H:%M:%S"), sunset_time.strftime("%H:%M:%S"))

                    # 获取晚霞质量数值
                    quality_value = self._get_sunset_quality_value()
                    if quality_value is None:
                        _LOGGER.warning("Sunset quality value not available")
                        await asyncio.sleep(60)
                        continue

                    _LOGGER.info("Sunset quality value: %.3f", quality_value)

                    # 判断阈值并推送
                    if quality_value >= 0.6:
                        level = "大烧"
                        quality_desc = "极佳"
                    elif quality_value >= 0.4:
                        level = "中烧"
                        quality_desc = "不错"
                    elif quality_value >= 0.2:
                        level = "小烧到中烧"
                        quality_desc = "可以"
                    else:
                        level = "无烧"
                        quality_desc = "一般"
                        await asyncio.sleep(60)
                        continue  # 无烧不推送

                    # 推送提醒（含晚霞云图链接）
                    msg = (f"【系统提示】今天晚霞质量{level}，记得提醒辣堡看晚霞。"
                           f"日落{sunset_time.strftime('%H:%M')}，"
                           f"云图：https://sunsetbot.top/?query_id=6441616&intend=select_city&query_city=%E6%9D%AD%E5%B7%9E&event=set_1&model=EC")
                    self._write_alert(msg)
                    _LOGGER.info("Sunset alert sent: %s", msg[:80])

                    # 标记今天已推送
                    self._last_sunset_alert_date = today_str

            except Exception as e:
                _LOGGER.error("Sunset check error: %s", e)

            await asyncio.sleep(60)  # 每分钟检查一次

    async def async_start_sunset_check(self) -> None:
        """启动晚霞检查定时任务（幂等）。"""
        if self._sunset_check_task is None:
            self._sunset_check_task = self.hass.asyncio.create_task(self._check_sunset_quality())
            _LOGGER.info("Sunset check task started")

    async def async_stop_sunset_check(self) -> None:
        """停止晚霞检查定时任务。"""
        if self._sunset_check_task is not None:
            self._sunset_check_task.cancel()
            self._sunset_check_task = None
            _LOGGER.info("Sunset check task stopped")

    # ------------------------------------------------------------------
    # 数据新鲜度 watchdog（断报只推一次）
    # ------------------------------------------------------------------
    async def _check_data_freshness(self) -> None:
        """周期检查手表数据文件 mtime，超过阈值未更新则告警（只推一次）。"""
        import asyncio

        while True:
            try:
                now = datetime.now(TZ).timestamp()
                for label, fpath in (("定位", GPS_DATA_FILE), ("健康", HEALTH_DATA_FILE)):
                    if not os.path.exists(fpath):
                        continue  # 文件不存在不告警（可能是首次部署）
                    mtime = os.path.getmtime(fpath)
                    age_min = (now - mtime) / 60.0
                    key = f"stale_{label}"
                    if age_min > self._stale_min:
                        # 断报只推一次：该 key 未告警过才推，推过之后数据恢复前不再重复
                        if key not in self._stale_alerted:
                            self._stale_alerted.add(key)
                            msg = (f"【系统提示】手表{label}数据已 {age_min:.0f} 分钟未更新，"
                                   f"可能已断报，请检查手表与网络连接。")
                            _LOGGER.info("Watchdog alert: %s", msg)
                            self._write_alert(msg)
                    else:
                        self._stale_alerted.discard(key)  # 数据恢复，重置告警状态
            except Exception as e:
                _LOGGER.error("Watchdog error: %s", e)
            await asyncio.sleep(60)  # 每分钟检查一次

    async def async_start_watchdog(self) -> None:
        """启动数据新鲜度 watchdog（幂等）。"""
        if not self._watchdog_enabled:
            _LOGGER.info("Watchdog disabled by config")
            return
        if self._watchdog_task is None:
            self._watchdog_task = self.hass.asyncio.create_task(self._check_data_freshness())
            _LOGGER.info("Watchdog started")

    async def async_stop_watchdog(self) -> None:
        """停止 watchdog。"""
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None
            _LOGGER.info("Watchdog stopped")

    # ------------------------------------------------------------------
    # DataUpdateCoordinator
    # ------------------------------------------------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        """Update data from entity states."""
        return {
            "is_home": self._is_home,
            "home_states": sorted(self._home_states),
            "alerts_count": len(self.alerts),
            "last_alert": self.alerts[-1] if self.alerts else None,
            "watchdog_enabled": self._watchdog_enabled,
        }

    def _is_relevant(self, entity_id: str) -> bool:
        """Check if entity is relevant for monitoring."""
        eid = entity_id.lower()
        return any(k in eid for k in RELEVANT_KEYWORDS)

    @callback
    def handle_state_change(self, entity_id: str, new_state: State, old_state: State) -> None:
        """Handle a state change event."""
        if not self._is_relevant(entity_id):
            return

        new_val = new_state.state
        old_val = old_state.state

        if new_val == old_val:
            return

        now = datetime.now(TZ).timestamp()
        ts = datetime.now(TZ).strftime("%H:%M:%S")
        _LOGGER.info("[%s] %s: %s → %s", ts, entity_id, old_val, new_val)

        alerts = []

        # Temperature check
        if "temperature" in entity_id.lower() or "体温" in entity_id:
            try:
                t = float(new_val)
                if t < self._temp_low and self._should_alert("temp_low"):
                    alerts.append(f"【系统提示】辣堡体温{t}°C偏低，请关心他的身体状况，但不要提到任何技术细节")
                elif t > self._temp_high and self._should_alert("temp_high"):
                    alerts.append(f"【系统提示】辣堡发烧了体温{t}°C，请提醒他注意休息，但不要提到任何技术细节")
            except (ValueError, TypeError):
                pass

        # Heart rate check
        if "heart" in entity_id.lower() or "心率" in entity_id:
            try:
                hr = float(new_val)
                if hr > self._hr_high and self._should_alert("hr_high"):
                    alerts.append(f"【系统提示】辣堡心率{int(hr)}过快，请关心他是否运动过度或紧张，但不要提到任何技术细节")
                elif 0 < hr < self._hr_low and self._should_alert("hr_low"):
                    alerts.append(f"【系统提示】辣堡心率{int(hr)}偏低，请关心他的身体状况，但不要提到任何技术细节")
            except (ValueError, TypeError):
                pass

        # Mood check
        if "mood" in entity_id.lower() or "心情" in entity_id:
            if new_val in BAD_MOOD_STATES:
                if self._is_home and self._should_alert(f"mood_music_{entity_id}"):
                    alerts.append("【系统提示】辣堡心情不好而且在家，请调用mijiaAPI放一首轻快的歌安慰他，但不要提到任何技术细节")
                elif self._should_alert(f"mood_{entity_id}"):
                    alerts.append("【系统提示】辣堡心情不好，请关心他、陪他聊聊，让他分享心事，但不要提到任何技术细节")

        # Location check (出门/到家) — 支持多套房子
        if "device_tracker" in entity_id.lower() or "person" in entity_id.lower():
            was_home = self._is_home_state(old_val)
            now_home = self._is_home_state(new_val)
            if old_val != new_val and was_home != now_home:
                self._is_home = now_home
                if not now_home and self._should_alert(f"loc_out_{entity_id}"):
                    alerts.append("【系统提示】辣堡出门了，请查询杭州天气并关心他的安全，问他去哪。但不要提到有关\"系统\"、\"HA\"等字眼")
                    _LOGGER.info("Location change: 出门 (from %s to %s)", old_val, new_val)
                elif now_home and self._should_alert(f"loc_home_{entity_id}"):
                    home_name = new_val if self._is_home_state(new_val) else "家"
                    alerts.append(f"【系统提示】辣堡到{home_name}了，欢迎他回家，问他累不累。但不要提到有关\"系统\"、\"HA\"等字眼")
                    _LOGGER.info("Location change: 到家 (%s)", new_val)

        # Write alerts
        for alert in alerts:
            self._write_alert(alert)