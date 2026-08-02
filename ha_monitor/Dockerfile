ARG BUILD_FROM
FROM $BUILD_FROM

# Python3 + aiohttp（WebSocket 连 HA 用）
RUN apk add --no-cache python3 py3-pip \
    && pip3 install --no-cache-dir aiohttp

# 运行脚本
COPY run.sh /run.sh
COPY ha_monitor.py /ha_monitor.py

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
