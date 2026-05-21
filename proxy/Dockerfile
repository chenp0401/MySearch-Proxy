FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/chenp0401/MySearch-Proxy" \
      org.opencontainers.image.title="mysearch-proxy" \
      org.opencontainers.image.description="MySearch proxy with Tavily/Firecrawl/Exa/Qwen key pool"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先装依赖以最大化层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 拷贝业务代码
COPY . .

# 准备数据目录并切到非 root
RUN mkdir -p /app/data \
    && groupadd -r app \
    && useradd -r -g app -d /app -s /usr/sbin/nologin app \
    && chown -R app:app /app
USER app

EXPOSE 9874

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:9874/healthz || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "9874"]
