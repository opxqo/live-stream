"""WebDAV 视频源（支持 123 盘等）"""

import base64
import logging
import time
from urllib.parse import quote

import httpx
from webdav4.client import Client

from sources import VideoItem, VideoSource

log = logging.getLogger(__name__)


class WebDAVSource(VideoSource):
    def __init__(self, url: str, username: str, password: str,
                 path: str, extensions: list[str]):
        self.url = url.rstrip("/")
        self.path = path
        self.extensions = {ext.lower() for ext in extensions}

        # 自定义 httpx 客户端（兼容 123 盘等 SSL 问题）
        http_client = httpx.Client(
            auth=(username, password),
            verify=False,
            timeout=30,
        )
        self.client = Client(base_url=self.url, http_client=http_client)

        # FFmpeg HTTP Basic Auth 值（不含 "Authorization: " 前缀）
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth_value = f"Basic {credentials}"

    def list_videos(self) -> list[VideoItem]:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                items = self._scan_recursive(self.path)
                log.info("%s%s -> 找到 %d 个视频", self.url, self.path, len(items))
                return items
            except Exception as e:
                log.warning("扫描失败 (第%d次): %s", attempt + 1, e)
                if attempt < max_retries - 1:
                    wait = (attempt + 1) * 3
                    log.info("%d 秒后重试...", wait)
                    time.sleep(wait)
        return []

    def list_dirs(self, dir_path: str = "/") -> list[dict]:
        """列举指定目录下的子目录和视频文件（不递归）"""
        items = []
        try:
            entries = self.client.ls(dir_path, detail=True)
        except Exception as e:
            log.warning("列举 %s 失败: %s", dir_path, e)
            return items

        for entry in entries:
            name = entry.get("name", "")
            entry_type = entry.get("type", "")
            display_name = name.rstrip("/").split("/")[-1]

            if entry_type == "directory":
                items.append({"name": display_name, "path": name, "type": "dir"})
            else:
                dot_idx = name.rfind(".")
                ext = name[dot_idx:].lower() if dot_idx != -1 else ""
                if ext in self.extensions:
                    items.append({"name": display_name, "path": name, "type": "file"})

        # 目录在前，文件在后，各自按名称排序
        dirs = sorted([i for i in items if i["type"] == "dir"], key=lambda x: x["name"])
        files = sorted([i for i in items if i["type"] == "file"], key=lambda x: x["name"])
        return dirs + files

    def _scan_recursive(self, dir_path: str) -> list[VideoItem]:
        """递归扫描 WebDAV 目录"""
        videos: list[VideoItem] = []
        try:
            entries = self.client.ls(dir_path, detail=True)
        except httpx.ReadTimeout:
            log.warning("列举 %s 超时，暂返回空列表", dir_path)
            return videos
        except Exception as e:
            log.warning("列举 %s 失败: %s", dir_path, e)
            return videos

        for entry in entries:
            name = entry.get("name", "")
            entry_type = entry.get("type", "")

            if entry_type == "directory":
                videos.extend(self._scan_recursive(name))
                continue

            # 检查文件扩展名
            dot_idx = name.rfind(".")
            ext = name[dot_idx:].lower() if dot_idx != -1 else ""
            if ext not in self.extensions:
                continue

            # 构建 FFmpeg 可用的 HTTP URL
            encoded_path = quote(name, safe="/")
            ffmpeg_url = f"{self.url}/{encoded_path}".replace("//", "/").replace(":/", "://")

            videos.append(VideoItem(
                name=name.split("/")[-1],
                ffmpeg_input=ffmpeg_url,
                headers={"Authorization": self._auth_value},
            ))

        return videos
