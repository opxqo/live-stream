"""本地文件夹视频源"""

import logging
from pathlib import Path

from sources import VideoItem, VideoSource

log = logging.getLogger(__name__)


class LocalSource(VideoSource):
    def __init__(self, path: str, extensions: list[str]):
        self.path = Path(path)
        self.extensions = {ext.lower() for ext in extensions}

    def list_videos(self) -> list[VideoItem]:
        if not self.path.exists():
            log.warning("目录不存在: %s", self.path)
            return []

        videos = []
        for file in sorted(self.path.rglob("*")):
            if file.is_file() and file.suffix.lower() in self.extensions:
                videos.append(VideoItem(
                    name=file.name,
                    ffmpeg_input=str(file),
                ))
        log.info("%s -> 找到 %d 个视频", self.path, len(videos))
        return videos
