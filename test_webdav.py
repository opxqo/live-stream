"""集成测试：验证 WebDAV 视频源扫描"""
import sys
sys.path.insert(0, ".")

import yaml
from sources.webdav import WebDAVSource

# 加载配置
with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

extensions = config["playlist"]["extensions"]

for src_cfg in config["sources"]:
    if src_cfg["type"] == "webdav":
        source = WebDAVSource(
            url=src_cfg["url"],
            username=src_cfg["username"],
            password=src_cfg["password"],
            path=src_cfg.get("path", "/"),
            extensions=extensions,
        )
        videos = source.list_videos()
        print(f"\n找到 {len(videos)} 个视频：")
        for v in videos[:20]:
            print(f"  ▶ {v.name}")
            print(f"    URL: {v.ffmpeg_input[:80]}...")
