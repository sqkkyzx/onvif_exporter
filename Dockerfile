FROM python:3.14-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip uv
WORKDIR /app

COPY pyproject.toml uv.lock* ./

RUN uv sync --locked --no-dev --no-install-project

FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

# 安装 ffmpeg 和相关解码器
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libavcodec-extra && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY pyproject.toml ./
COPY main.py ./

EXPOSE 9121

CMD ["python", "main.py"]
