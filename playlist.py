"""播放列表管理"""

import json
import logging
import random
import threading
from pathlib import Path

from sources import VideoItem, VideoSource

log = logging.getLogger(__name__)

PROGRESS_FILE = Path("progress.json")


class Playlist:
    def __init__(self, sources: list[VideoSource], mode: str = "sequential"):
        self.sources = sources
        self.mode = mode
        self._videos: list[VideoItem] = []
        self._index = 0
        self._lock = threading.Lock()
        self._resume_position: float = 0.0  # 恢复播放的秒数
        self.reload()

    def reload(self):
        """从所有视频源重新加载视频列表"""
        videos: list[VideoItem] = []
        for source in self.sources:
            videos.extend(source.list_videos())

        with self._lock:
            self._videos = videos
            self._index = 0

        if not videos:
            log.warning("未找到任何视频文件")
            return

        if self.mode == "random":
            random.shuffle(self._videos)

        # 尝试从进度文件恢复
        self._restore_progress()

        log.info("已加载 %d 个视频，模式: %s", len(videos), self.mode)

    def next(self) -> VideoItem | None:
        """获取下一个视频，播完自动循环"""
        with self._lock:
            if not self._videos:
                pass  # 释放锁后 reload
            else:
                video = self._videos[self._index]
                self._index += 1
                self._save_progress()
                if self._index >= len(self._videos):
                    log.info("一轮播放完毕，重新加载列表")
                    # 先释放锁再 reload（reload 内部也会加锁）
                    threading.Thread(target=self.reload, daemon=True).start()
                return video

        # 无视频，尝试重新加载
        self.reload()
        with self._lock:
            if not self._videos:
                return None
            self._index = 1
            self._save_progress()
            return self._videos[0]

    def jump_to(self, index: int) -> VideoItem | None:
        """跳转到指定索引的视频"""
        with self._lock:
            if 0 <= index < len(self._videos):
                self._index = index
                self._save_progress()
                return self._videos[index]
            return None

    @property
    def videos(self) -> list[dict]:
        """返回视频列表摘要（供 Web 面板使用）"""
        with self._lock:
            return [
                {"index": i, "name": v.name, "current": i == self._index - 1}
                for i, v in enumerate(self._videos)
            ]

    @property
    def total(self) -> int:
        with self._lock:
            return len(self._videos)

    def switch_path(self, new_path: str):
        """切换 WebDAV 源的播放路径并重新加载"""
        from sources.webdav import WebDAVSource
        for source in self.sources:
            if isinstance(source, WebDAVSource):
                source.path = new_path
                log.info("切换播放目录: %s", new_path)
                break
        self.reload()

    # ── 进度持久化 ─────────────────────────────────

    def consume_resume_position(self) -> float:
        """获取并清除恢复播放位置（秒），只消费一次"""
        with self._lock:
            pos = self._resume_position
            self._resume_position = 0.0
            return pos

    def save_progress_with_position(self, position: float):
        """保存带秒级位置的播放进度（供外部 streamer 调用）"""
        with self._lock:
            self._save_progress(position=position)

    def _save_progress(self, position: float = 0.0):
        """保存当前播放进度（在锁内调用）"""
        if not self._videos:
            return
        try:
            data = {
                "index": self._index,
                "video_name": self._videos[self._index - 1].name if self._index > 0 else "",
                "position": round(position, 1),
            }
            PROGRESS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            log.warning("保存进度失败: %s", e)

    def _restore_progress(self):
        """从进度文件恢复播放位置"""
        if not PROGRESS_FILE.exists():
            return
        try:
            data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
            saved_index = data.get("index", 0)
            saved_name = data.get("video_name", "")
            saved_position = data.get("position", 0.0)

            with self._lock:
                # 优先按视频名匹配（防止列表顺序变化）
                for i, v in enumerate(self._videos):
                    if v.name == saved_name:
                        self._index = i
                        self._resume_position = saved_position
                        log.info("▶ 从进度恢复: %s (第 %d 个, 位置 %.1f 秒)", saved_name, i + 1, saved_position)
                        return

                # 名称未匹配到，尝试用索引恢复
                if 0 <= saved_index < len(self._videos):
                    self._index = saved_index
                    self._resume_position = saved_position
                    log.info("▶ 从进度恢复: 索引 %d (位置 %.1f 秒)", saved_index, saved_position)
        except Exception as e:
            log.warning("恢复进度失败: %s", e)
