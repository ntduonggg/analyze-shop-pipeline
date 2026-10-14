FROM python:3.12-slim

# tzdata: để giờ trong log/tên thư mục output theo TZ (đặt trong docker-compose)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/app/data \
    CONFIG_DIR=/app/config

# Cài dependency trước (cache layer khi chỉ sửa code)
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
COPY config ./config
RUN uv sync --locked --no-dev

VOLUME ["/app/data"]
CMD ["analyzer", "--host", "0.0.0.0", "--no-browser"]
