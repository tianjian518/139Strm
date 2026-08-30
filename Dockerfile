FROM python:3.11-slim

LABEL org.opencontainers.image.title="139Strm" \
      org.opencontainers.image.description="移动云盘(139) STRM 生成器与 302 直链服务，原生支持 .cas 秒传文件播放" \
      org.opencontainers.image.source="https://github.com/tianjian518/139Strm" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

# 依赖单独一层，改代码时不用重装
# PIP_INDEX_URL 可覆盖：国内构建默认走清华源，境外 CI 传空值即回退官方源
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
COPY requirements.txt ./
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt \
    || pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/config /strm

COPY yun139/ ./yun139/
COPY templates/ ./templates/
COPY app.py ./

ENV PORT=8025 \
    CONFIG_PATH=/app/config/config.json \
    YUN139_OUTPUT_DIR=/strm \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

EXPOSE 8025

HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8025')+'/health')" || exit 1

CMD ["python", "app.py"]
