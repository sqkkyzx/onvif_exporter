FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1

# 安装 ffmpeg 和相关解码器
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libavcodec-extra && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip uv
WORKDIR /app

COPY pyproject.toml uv.lock* ./

RUN uv pip install --system --no-cache-dir --no-verify-hashes .

COPY main.py ./

EXPOSE 9121

CMD ["python", "main.py"]
