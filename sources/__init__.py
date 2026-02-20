"""视频源抽象基类"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class VideoItem:
    """视频条目"""
    name: str          # 文件名
    ffmpeg_input: str  # FFmpeg -i 参数（本地路径或 HTTP URL）
    headers: dict | None = None  # HTTP 请求头（WebDAV 认证用）


class VideoSource(ABC):
    """视频来源抽象基类"""

    @abstractmethod
    def list_videos(self) -> list[VideoItem]:
        """列举所有可用视频"""
        ...
