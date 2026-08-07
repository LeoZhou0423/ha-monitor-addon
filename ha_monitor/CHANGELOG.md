# Changelog

## 2.1.7

- **告警策略改为状态锁**：体温/心率/断连/位置/心情每类异常只提醒一次，状态恢复正常后自动解锁；彻底取消 2.1.5 的 50 分钟/COOLDOWN 时间冷却，避免持续异常时反复打扰
- **去机械化文案**：新增文案池，同类告警会随机使用 2~3 条不同措辞；上下文衔接改为 burst 模式，仅在 5 分钟内连续多条消息时才加“另外，”/“还有，”，超过间隔自动重置为新会话
- **新增配置项 `burst_gap_min`**：burst 上下文窗口（分钟，默认 5），可在 Add-on 配置界面调整

## 2.1.6

- **修复日落时间解析崩溃**：`sensor.ri_luo_shi_jian` 在 sunsetbot API 无当日数据时返回占位符 `-`，直接 `datetime.fromisoformat('-')` 抛错并每分钟刷屏。`_parse_sunset_time` 现将纯占位符（`-`/`--` 等）视为无效静默跳过，不触发晚霞提醒
- 版本号同步至 2.1.6（此前 2.1.5 已含直连认证与拟真策略）

## 2.1.5

- **拟真策略**：体温/心率告警触发后 50 分钟智能冷却（状态恢复时清零）；多条告警间隔 45-120 秒随机发送（`alert_interval_min` / `alert_interval_max` 可配）；第二条消息加"另外，"，第三条加"还有，"避免机械感
- **告警消息风格重写**：全部消息改为吐槽式/建议式口语，去掉【系统提示】前缀和"请关心他/但不要提到技术细节"等指令式废话；措辞铁律：home/世纪新筑统一"到家了"

## 2.1.4

- **直连认证修复 401**：优先使用 `HA_DIRECT_TOKEN`（长期访问令牌）直连 HA Core（`ws://homeassistant:8123/api/websocket`），绕过 supervisor 网关的 `Auth failed`；未配置时回退 SUPERVISOR_TOKEN

## 2.1.2

- **修复"到家了"连发多遍**（core 重启/WS 重连风暴导致）：三层防护
  - **快照事件过滤**：WS 重连后 HA 会把全部实体当前状态作为 `state_changed` 推送（`old_state` 为 null）。此类快照事件现在直接跳过，不再被误判为真实"到家/出门"
  - **重连抑制窗口**：每次 WS 连接建立后前 30 秒忽略所有事件（`RE_CONNECT_SUPPRESS`，可配），保护 core 重启瞬间的批量恢复流
  - **全局 cooldown key**：到家/出门告警的 cooldown key 不再带实体后缀（`loc_home`/`loc_out` 全局共享），多个 tracker 同一时刻变化只推一条
- 本次修改本地单元验证通过：快照跳过 / 抑制窗口跳过 / 窗口外正常 / 全局 cooldown 拦截重复

## 2.1.1

- **修复"到家"通知文案**：不再拼接 zone 名（此前状态为"世纪新筑"时推送"辣堡到世纪新筑了"，会误导米糊把它当成"去了别处"）。统一推送"辣堡到家了"，home 和世纪新筑都算家

## 2.1.0

- **修复 unknown 状态误报**：当手表同步失败上报空值/unknown 时，不再触发"出门了"等虚假告警
  - 事件层：`state` 为 `unknown`/`unavailable`/`none`/空 等无效状态时直接忽略，不进入任何告警逻辑（此前 `device_tracker` 从 `home` → `unknown` 会被误判为"辣堡出门了"）
  - 配套写入端修复见 custom_components（`api.py` 对无效值保留旧数据，见仓库 AndroidGpsApp）

## 2.0.9

- 修复直连模式失效：`HA_DIRECT_TOKEN`/`HA_DIRECT_URL` 需在 `_load_options()`（读取 `/data/options.json`）**之后**读取环境变量，否则 options 尚未注入、token 恒为空 → 直连 WS 仍走魔改网关（Auth failed）

## 2.0.8

- **修复致命 bug：断报 watchdog 从未运行**。此前 `check_data_freshness()` 任务被埋在 WebSocket 连接成功之后才启动，而本机魔改版 supervisor 的 `/core` 网关代理不可用（WS 无限 `Auth failed`）→ 40 分钟断报告警从未触发。现已将 watchdog 提到 WS 循环**之外独立启动**——它只读本地数据文件 mtime，与 HA 连接完全解耦，WS 连不上也照常告警
- **修复 WS URL 路径**：`ws://supervisor/core/api/websocket` → `ws://supervisor/core/websocket`（官方标准路径，去掉多余的 `/api/`）
- **新增直连模式**：配置 `ha_direct_token`（HA 长期访问令牌）+ `ha_direct_url`（如 `ws://homeassistant:8123/api/websocket`）可绕过 supervisor 网关直连 core，魔改版 supervisor 下事件告警（体温/心率/心情/出门）也能恢复
- **修复版本号**：此前 v2.0.7 忘记在 config.yaml bump version（仍为 2.0.6），导致附加组件商店识别不到更新（`update_available=false`）。本次 version → 2.0.8

## 2.0.7

- 断报容错升级：数据超过阈值未更新只推送**一次**，数据恢复前**绝不重复打扰**（修复此前每 10 分钟重复推送的 bug）
- 断报阈值默认从 15 分钟提高到 **40 分钟**（`stale_min` 配置项同步更新，可改）
- 数据全部恢复后自动重置告警状态，下次再断报才会再次提醒

## 2.0.6

- 修复重复告警：watchdog 同一轮检查把「定位」「健康」各推一条 → 合并为一条消息（如「手表定位(2546分钟)、健康(2546分钟)数据未更新」）

## 2.0.5

- 修复连接失败：config.yaml 增加 `homeassistant_api: true`，允许经 supervisor 网关访问 HA core（此前 WebSocket 认证 `Auth failed`）
- 加固配置读取：绕开 bashio（本魔改版 supervisor 的 `bashio::config` 访问 API 恒被 forbidden），改为脚本直接读取 supervisor 挂载的 `/data/options.json`

## 2.0.4

- 修复启动崩溃：config.yaml 增加 `auth_api: true`，允许 `SUPERVISOR_TOKEN` 访问 supervisor API（此前 `bashio::config` 报 `Unable to access the API, forbidden`，全部配置读成空字符串）
- 加固：环境变量解析改为防御式（空字符串/非法值回退默认值），单个配置项异常不再导致容器崩溃重启

## 2.0.3

- 修复构建失败：base 镜像 3.23 的 Python 是 PEP 668 externally-managed，`pip3 install aiohttp` 被拒（`externally-managed-environment` 错误）。Dockerfile 增加 `--break-system-packages` 绕过

## 2.0.2

- 修复构建失败（最终版）：Dockerfile 中 `BUILD_FROM` 写死默认值 `ghcr.nju.edu.cn/home-assistant/amd64-base:3.23`，不再依赖 supervisor 注入 build-arg（本魔改版 supervisor 构建命令从不传 `BUILD_FROM`）

## 2.0.1

- 修复构建失败：config.yaml 补充 `build_from`（base 镜像用 ghcr.nju.edu.cn 代理源），supervisor 构建时正确注入 `BUILD_FROM`

## 2.0.0

- 重构为 HAOS Add-on（替代原 core-ssh 独立脚本）
- Supervisor 网关认证（`SUPERVISOR_TOKEN`），无需手工配置 HA token
- 配置项全部移到附加组件配置界面（webhook、阈值、断报开关）
- 保留：webhook 直连推送、多 zone 在家判断（世纪新筑）、断报只推一次、启动不推告警

## 1.0.0

- 初版（core-ssh 独立脚本）：写文件告警 + cron 轮询
