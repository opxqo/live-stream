# B 站 24 小时推流工具

Python + FFmpeg 实现的 B 站 7×24 小时无人直播推流工具，支持本地和 WebDAV 视频源。

## 功能

- ✅ 本地文件夹 / WebDAV 视频源（支持 123 盘等）
- ✅ 顺序 / 随机播放，自动循环
- ✅ 断线自动重连
- ✅ 统一分辨率 + 编码输出
- ✅ 优雅的信号处理

## 前置条件

- Python 3.10+
- FFmpeg（需在 PATH 中）

## 安装

```bash
# 安装 Python 依赖
pip install -r requirements.txt

# 确认 FFmpeg 可用
ffmpeg -version
```

### Ubuntu 安装 FFmpeg

```bash
sudo apt update
sudo apt install ffmpeg
```

## 使用

### 1. 配置

编辑 `config.yaml`，填写：

- **推流地址**：从 [B 站直播设置](https://link.bilibili.com/p/center/index#/my-room/start-live) 获取
- **视频来源**：配置本地路径或 WebDAV 信息

### 2. 运行

```bash
python main.py
```

### 3. 后台运行（Ubuntu 服务器）

```bash
# 使用 nohup
nohup python main.py > stream.log 2>&1 &

# 或使用 systemd（推荐）
sudo cp bilibili-stream.service /etc/systemd/system/
sudo systemctl enable bilibili-stream
sudo systemctl start bilibili-stream

# 查看日志
sudo journalctl -u bilibili-stream -f
```

## systemd 服务配置

创建 `/etc/systemd/system/bilibili-stream.service`：

```ini
[Unit]
Description=Bilibili 24h Live Stream
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/直播推流
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## 项目结构

```
├── config.yaml       # 配置文件
├── main.py           # 入口
├── streamer.py       # 推流核心（FFmpeg 进程管理）
├── playlist.py       # 播放列表管理
├── sources/
│   ├── __init__.py   # 视频源基类
│   ├── local.py      # 本地视频源
│   └── webdav.py     # WebDAV 视频源
└── requirements.txt  # 依赖
```
