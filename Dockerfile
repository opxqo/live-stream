FROM python:3.12-slim

# 安装 FFmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg fonts-wqy-zenhei && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用 Docker 缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY main.py streamer.py playlist.py web.py auth.py ./
COPY sources/ ./sources/

# Web 面板端口
EXPOSE 8088

# 运行时挂载: -v /path/to/config.yaml:/app/config.yaml
CMD ["python", "main.py"]
