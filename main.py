"""B 站 24 小时推流服务入口"""

import atexit
import logging
import signal
import subprocess
import sys
from pathlib import Path

import uvicorn
import yaml

from playlist import Playlist
from sources.local import LocalSource
from sources.webdav import WebDAVSource
from streamer import Streamer
import auth
import web

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def load_config(path: str = "config.yaml") -> dict:
    """加载配置文件"""
    config_path = Path(path)
    if not config_path.exists():
        log.error("配置文件不存在: %s", config_path.absolute())
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    log.info("配置已加载: %s", config_path)
    return config


def build_sources(config: dict) -> list:
    """根据配置创建视频源"""
    sources = []
    extensions = config.get("playlist", {}).get("extensions", [".mp4", ".mkv", ".flv"])

    for src_cfg in config.get("sources", []):
        src_type = src_cfg.get("type")

        if src_type == "local":
            sources.append(LocalSource(
                path=src_cfg["path"],
                extensions=extensions,
            ))
        elif src_type == "webdav":
            sources.append(WebDAVSource(
                url=src_cfg["url"],
                username=src_cfg["username"],
                password=src_cfg["password"],
                path=src_cfg.get("path", "/"),
                extensions=extensions,
            ))
        else:
            log.warning("未知视频源类型: %s", src_type)

    if not sources:
        log.error("未配置任何视频来源，请检查 config.yaml")
        sys.exit(1)

    return sources


def start_mediamtx(config: dict) -> subprocess.Popen | None:
    """当 webcam 启用且本地有 mediamtx 时，自动启动 MediaMTX"""
    webcam = config.get("webcam", {})
    if not webcam.get("enabled"):
        return None

    exe = Path("mediamtx_bin/mediamtx.exe")
    yml = Path("mediamtx.yml")
    if not exe.exists():
        log.warning("未找到 %s，跳过 MediaMTX 启动（webcam 可能不可用）", exe)
        return None

    args = [str(exe)]
    if yml.exists():
        args.append(str(yml))

    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("MediaMTX 已启动 (PID %d)", proc.pid)

    def cleanup():
        if proc.poll() is None:
            proc.terminate()
            log.info("MediaMTX 已停止")
    atexit.register(cleanup)

    return proc


def main():
    config = load_config()

    # 检查推流配置
    stream = config.get("stream", {})
    if "xxx" in stream.get("stream_key", "xxx"):
        log.error("推流码未配置！请在 config.yaml 中填写 B 站推流地址和推流码")
        log.info("获取地址: https://link.bilibili.com/p/center/index#/my-room/start-live")
        sys.exit(1)

    # 启动 MediaMTX（webcam RTSP 中转）
    mtx_proc = start_mediamtx(config)

    sources = build_sources(config)
    playlist = Playlist(
        sources=sources,
        mode=config.get("playlist", {}).get("mode", "sequential"),
    )
    streamer = Streamer(playlist=playlist, config=config)

    # 初始化认证
    auth.init_admin(config)

    # 注入到 Web 模块
    web.init_app(streamer, config=config)

    # 信号处理
    def handle_signal(signum, frame):
        log.info("收到信号 %d，正在停止...", signum)
        streamer.stop()
        if mtx_proc and mtx_proc.poll() is None:
            mtx_proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # 后台线程启动推流
    streamer.run_in_thread()

    # 主线程启动 Web 面板
    web_cfg = config.get("web", {})
    host = web_cfg.get("host", "0.0.0.0")
    port = web_cfg.get("port", 8088)
    log.info("Web 管理面板: http://localhost:%d", port)
    uvicorn.run(web.app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
