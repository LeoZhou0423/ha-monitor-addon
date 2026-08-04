# 默认值兜底：本魔改版 supervisor 不注入 BUILD_FROM 时也能构建
# （该镜像源 ghcr.nju.edu.cn = 南京大学 ghcr 代理，本机已验证可达）
ARG BUILD_FROM=ghcr.nju.edu.cn/home-assistant/amd64-base:3.23
FROM $BUILD_FROM

# Python3 + aiohttp（WebSocket 连 HA 用）
# --break-system-packages：base 镜像 3.23 的 Python 是 PEP 668 externally-managed，
# 不加会报 externally-managed-environment 导致构建失败
RUN apk add --no-cache python3 py3-pip \
    && pip3 install --no-cache-dir --break-system-packages aiohttp

# 运行脚本
COPY run.sh /run.sh
COPY ha_monitor.py /ha_monitor.py

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
