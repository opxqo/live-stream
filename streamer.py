"""FFmpeg 推流核心"""

import logging
import os
import re
import smtplib
import socket
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from email.mime.text import MIMEText
from urllib.parse import urlparse

import psutil
import requests
import yaml

from playlist import Playlist

log = logging.getLogger(__name__)


class Streamer:
    def __init__(self, playlist: Playlist, config: dict):
        self.playlist = playlist
        self.stream_cfg = config["stream"]
        self.video_cfg = config["video"]
        self.audio_cfg = config["audio"]
        self.overlay_cfg = config.get("overlay", [])
        self.logo_cfg = config.get("logo", {})
        self.images_cfg = config.get("images", [])
        self.clock_cfg = config.get("clock", {})
        self.resilience = config.get("resilience", {})
        self.email_cfg = config.get("email", {})

        self._process: subprocess.Popen | None = None
        self._running = False
        self._skip_requested = False  # 用户主动跳过/点播标志
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # 缓存字体路径（只查找一次）
        self._font_path: str | None = self._find_font()

        # 状态信息（供 Web 面板读取）
        self.current_video: str = ""
        self.start_time: datetime | None = None
        self.videos_played: int = 0
        self._total_failures: int = 0    # 累计失败次数
        self._last_diagnosis_time: float = 0  # 上次自检时间戳

        # FFmpeg 实时指标
        self.duration: float = 0.0       # 视频总时长（秒）
        self.current_time: float = 0.0   # 当前播放位置（秒，绝对时间）
        self.progress: float = 0.0       # 播放进度 0~100
        self.bitrate: str = ""           # 推流码率
        self.speed: str = ""             # 编码速度
        self._seek_offset: float = 0.0   # -ss 跳转偏移量
        
        # B站API相关配置缓存
        self._bili_cfg = config.get("bilibili", {})

    # ── 公共 API ──────────────────────────────────

    def start(self):
        """启动推流循环"""
        if self._running:
            return

        if not self._font_path:
            self._font_path = self._find_font()
            if self._font_path:
                log.info("Using font: %s", self._font_path)
            else:
                log.warning("No font found! OSD might fail.")

        self._running = True
        self.start_time = datetime.now()
        self._total_failures = 0

        base_delay = self.resilience.get("retry_delay", 5)
        max_delay = self.resilience.get("max_retry_delay", 60)
        max_retries = self.resilience.get("max_retries", 0)

        log.info("=" * 50)
        log.info("B 站 24 小时推流服务已启动")
        log.info("视频总数: %d", self.playlist.total)
        log.info("=" * 50)

        self._notify_email("🟢 推流服务已启动", f"视频总数: {self.playlist.total}")

        while self._running:
            video = self.playlist.next()
            if not video:
                log.warning("无可用视频，%d 秒后重试...", base_delay)
                time.sleep(base_delay)
                continue

            video_retry = 0
            # 首次播放该视频时检查是否有断点续播位置
            resume_pos = self.playlist.consume_resume_position()
            while self._running:
                with self._lock:
                    self.current_video = video.name
                    self.duration = 0.0
                    self.current_time = 0.0
                    self.progress = 0.0
                    self.bitrate = ""
                    self.speed = ""
                if resume_pos > 0:
                    log.info("▶ 正在播放: %s (从 %.1f 秒续播)", video.name, resume_pos)
                else:
                    log.info("▶ 正在播放: %s", video.name)
                with self._lock:
                    self._seek_offset = resume_pos
                cmd = self._build_ffmpeg_cmd(video.ffmpeg_input, video.headers, video.name, seek_position=resume_pos)

                try:
                    self._process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    )
                    self._read_output()
                    returncode = self._process.wait()

                    # 用户主动跳过/点播，直接跳出重试
                    if self._skip_requested:
                        self._skip_requested = False
                        log.info("⏭ 用户操作，切换视频")
                        break

                    if returncode == 0:
                        log.info("✓ %s 播放完毕", video.name)
                        with self._lock:
                            self.videos_played += 1
                        self._total_failures = 0
                        break

                    video_retry += 1
                    self._total_failures += 1

                    # 立即将当前秒数写入 progress.json，确保断点精确
                    with self._lock:
                        resume_pos = self.current_time
                    self.playlist.save_progress_with_position(resume_pos)
                    log.warning("✗ FFmpeg 异常退出 (code=%d)，第 %d 次重试，将从 %.1f 秒续播",
                                returncode, video_retry, resume_pos)

                    if max_retries > 0 and self._total_failures >= max_retries:
                        log.error("达到最大重试次数 (%d)，运行自检后停止推流", max_retries)
                        self._trigger_diagnosis(video.name, f"累计失败 {self._total_failures} 次，已达上限")
                        self._running = False
                        break

                    # 累计 5 次失败 → 自检（不停止，继续重试当前视频）
                    if self._total_failures >= 5 and self._total_failures % 5 == 0:
                        should_stop = self._trigger_diagnosis(video.name, f"累计失败 {self._total_failures} 次")
                        if should_stop:
                            self._running = False
                            break

                    # 指数退避：base * 2^(retry-1)，上限 max_delay
                    delay = min(base_delay * (2 ** (video_retry - 1)), max_delay)
                    log.info("等待 %d 秒后重试...", delay)
                    time.sleep(delay)

                except Exception as e:
                    log.exception("推流异常: %s", e)
                    self._total_failures += 1
                    with self._lock:
                        resume_pos = self.current_time
                    self.playlist.save_progress_with_position(resume_pos)
                    time.sleep(base_delay)

        with self._lock:
            self.current_video = ""
        self._cleanup()
        log.info("推流服务已停止")

    def run_in_thread(self):
        """在后台线程中启动推流"""
        self._thread = threading.Thread(target=self.start, daemon=True)
        self._thread.start()

    def skip(self):
        """跳过当前视频"""
        if self._process and self._process.poll() is None:
            self._skip_requested = True
            log.info("⏭ 跳过当前视频")
            self._process.terminate()

    def play(self, index: int) -> bool:
        """跳转到指定索引的视频"""
        video = self.playlist.jump_to(index)
        if not video:
            return False
        log.info("⏯ 跳转到: %s", video.name)
        # 终止当前进程，主循环会自动取 jump_to 设置的视频
        if self._process and self._process.poll() is None:
            self._skip_requested = True
            self._process.terminate()
        return True

    def stop(self):
        """停止推流"""
        self._running = False
        self._cleanup()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def status(self) -> dict:
        """返回当前推流状态"""
        uptime = ""
        if self.start_time:
            delta = datetime.now() - self.start_time
            hours, remainder = divmod(int(delta.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime = f"{hours}h {minutes}m {seconds}s"

        # 系统监控
        cpu = psutil.cpu_percent(interval=0)
        mem = psutil.virtual_memory().percent

        with self._lock:
            return {
                "running": self._running,
                "current_video": self.current_video,
                "videos_played": self.videos_played,
                "uptime": uptime,
                "playlist_total": self.playlist.total,
                "progress": round(self.progress, 1),
                "duration": round(self.duration, 1),
                "current_time": round(self.current_time, 1),
                "bitrate": self.bitrate,
                "speed": self.speed,
                "cpu_percent": cpu,
                "memory_percent": mem,
            }

    # ── FFmpeg 命令构建 ───────────────────────────

    def _build_ffmpeg_cmd(self, input_path: str, headers: dict | None = None, video_name: str = "", seek_position: float = 0.0) -> list[str]:
        """构建 FFmpeg 推流命令"""
        rtmp_url = self.stream_cfg["rtmp_url"] + self.stream_cfg["stream_key"]
        v = self.video_cfg
        a = self.audio_cfg
        w, h = v.get("width", 1920), v.get("height", 1080)
        
        cmd = ["ffmpeg", "-y"]

        # 1. 主视频输入（如有断点续播位置，在 -i 前加 -ss）
        if headers and "Authorization" in headers:
            cmd += ["-headers", f"Authorization: {headers['Authorization']}\r\n"]
        if seek_position > 0:
            cmd += ["-ss", f"{seek_position:.1f}"]
        cmd += ["-re", "-i", input_path]

        # 2. 收集图片输入 (Logo + Images)
        image_inputs = []
        if self.logo_cfg and self.logo_cfg.get("path"):
             image_inputs.append(self.logo_cfg)
        if self.images_cfg:
             image_inputs.extend(self.images_cfg)

        valid_images = []
        for img in image_inputs:
            path = img.get("path", "")
            if not path:
                continue
            if path.startswith("http"):
                # 预检远程图片是否可达
                try:
                    resp = requests.head(path, timeout=3, allow_redirects=True)
                    if resp.status_code < 400:
                        cmd += ["-i", path]
                        valid_images.append(img)
                    else:
                        log.warning("远程图片不可用 (HTTP %d): %s", resp.status_code, path)
                except Exception as e:
                    log.warning("远程图片不可达，已跳过: %s (%s)", path, e)
            elif os.path.exists(path):
                cmd += ["-i", path]
                valid_images.append(img)
            else:
                log.warning("图片文件不存在: %s", path)

        # 3. 滤镜链
        # [0:v] 缩放并填充黑边 -> [base]
        fc = f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2[base];"
        
        current_stream = "[base]"
        
        # 叠加图片
        for i, img in enumerate(valid_images):
            idx = i + 1
            iw = img.get("height", 80)
            ix = img.get("x", 20)
            iy = img.get("y", 20)
            opacity = img.get("opacity", 1.0)
            
            # 缩放图片
            fc += f"[{idx}:v]scale=-1:{iw},format=rgba"
            if opacity < 1.0:
                 fc += f",colorchannelmixer=aa={opacity}"
            fc += f"[img{i}];"
            
            # 叠加
            next_stream = f"[v{i}]"
            fc += f"{current_stream}[img{i}]overlay=x={ix}:y={iy}{next_stream};"
            current_stream = next_stream

        # 叠加文字
        text_filters = self._collect_text_filters(video_name)
        if text_filters:
            fc += f"{current_stream}{text_filters}[out]"
        else:
            fc += f"{current_stream}null[out]"

        cmd += ["-filter_complex", fc, "-map", "[out]", "-map", "0:a"]

        # 编码参数
        bitrate = v.get("bitrate", "3000k")
        fps = v.get("fps", 30)
        cmd += [
            "-c:v", "libx264", "-preset", v.get("preset", "veryfast"), "-profile:v", "baseline",
            "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", f"{int(bitrate.replace('k', '')) * 2}k",
            "-r", str(fps), "-g", str(fps * 2), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", a.get("bitrate", "192k"), "-ar", str(a.get("sample_rate", 44100)), "-ac", str(a.get("channels", 2)),
            "-flvflags", "no_duration_filesize", "-f", "flv", rtmp_url,
        ]
        
        log.info("FFmpeg CMD: %s", ' '.join(cmd)[:500])
        return cmd

    # ── 滤镜构建 ─────────────────────────────────

    def _collect_text_filters(self, video_name: str) -> str:
        """收集所有文字滤镜，返回逗号分隔的滤镜链"""
        parts: list[str] = []

        # 配置文件中的文字叠加
        overlays = self.overlay_cfg if isinstance(self.overlay_cfg, list) else (
            [self.overlay_cfg] if self.overlay_cfg else []
        )
        for item in overlays:
            if not item or not item.get("text"):
                continue
            parts.append(self._drawtext(
                text=item["text"],
                fontsize=item.get("fontsize", 36),
                fontcolor=item.get("fontcolor", "white"),
                x=item.get("x", 20),
                y=item.get("y", 20),
                borderw=item.get("borderw", 2),
            ))

        # 右下角集数
        episode = self._extract_episode(video_name)
        if episode:
            parts.append(self._drawtext(
                text=episode, fontsize=28, fontcolor="white@0.8",
                x="w-tw-30", y="h-th-30", borderw=1,
            ))

        # 右上角实时时钟
        clock = self._build_clock_filter()
        if clock:
            parts.append(clock)

        return ",".join(parts)

    def _drawtext(self, text: str, fontsize: int, fontcolor: str,
                  x, y, borderw: int = 2) -> str:
        """构建单个 drawtext 滤镜"""
        safe_text = text.replace("'", "\\'").replace(":", "\\:")
        dt = f"drawtext=text='{safe_text}':fontsize={fontsize}:fontcolor={fontcolor}:x={x}:y={y}:borderw={borderw}"
        if self._font_path:
            safe_path = self._font_path.replace("\\", "/").replace(":", "\\:")
            dt += f":fontfile='{safe_path}'"
        return dt

    def _extract_episode(self, video_name: str) -> str:
        """从文件名提取集数信息"""
        if not video_name:
            return ""
        m = re.search(r'S(\d+)E(\d+)', video_name, re.IGNORECASE)
        if m:
            return f"第{int(m.group(1))}季 第{int(m.group(2))}集"
        m = re.search(r'EP?(\d+)', video_name, re.IGNORECASE)
        if m:
            return f"第{int(m.group(1))}集"
        return os.path.splitext(video_name)[0]

    def _build_clock_filter(self) -> str:
        """构建右上角实时时钟 drawtext 滤镜"""
        if not self.clock_cfg.get("enabled", True):
            return ""
        fontsize = self.clock_cfg.get("fontsize", 24)
        fontcolor = self.clock_cfg.get("fontcolor", "white@0.8")
        x = self.clock_cfg.get("x", "w-tw-30")
        y = self.clock_cfg.get("y", 30)
        fmt = self.clock_cfg.get("format", "%H\\:%M\\:%S")

        dt = f"drawtext=text='%{{localtime\\:{fmt}}}':fontsize={fontsize}:fontcolor={fontcolor}:x={x}:y={y}:borderw=1"
        if self._font_path:
            safe_path = self._font_path.replace("\\", "/").replace(":", "\\:")
            dt += f":fontfile='{safe_path}'"
        return dt

    # ── 内部方法 ──────────────────────────────────

    @staticmethod
    def _find_font() -> str | None:
        """查找支持中文的字体文件"""
        candidates = [
            # Linux / Docker (Debian: fonts-wqy-zenhei)
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy-zenhei/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            # Windows
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simsun.ttc",
            "C:/Windows/Fonts/simhei.ttf",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path.replace("\\", "/")
        return None

    # 正则：匹配 FFmpeg 输出中的 Duration 和实时状态行
    _RE_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)")
    _RE_PROGRESS = re.compile(r"time=(\d+):(\d+):(\d+)\.(\d+)")
    _RE_BITRATE = re.compile(r"bitrate=\s*([\d.]+\s*kbits/s)")
    _RE_SPEED = re.compile(r"speed=\s*([\d.]+x)")

    def _read_output(self):
        """读取 FFmpeg 输出，解析进度和码率"""
        if not self._process or not self._process.stdout:
            return
        last_progress_save = time.time()
        for line in self._process.stdout:
            if not self._running:
                break
            text = line.decode("utf-8", errors="replace").strip()

            # 解析视频总时长
            m = self._RE_DURATION.search(text)
            if m:
                with self._lock:
                    self.duration = (
                        int(m.group(1)) * 3600 + int(m.group(2)) * 60
                        + int(m.group(3)) + int(m.group(4)) / 100
                    )

            # 解析当前播放位置
            m = self._RE_PROGRESS.search(text)
            if m:
                relative_time = (
                    int(m.group(1)) * 3600 + int(m.group(2)) * 60
                    + int(m.group(3)) + int(m.group(4)) / 100
                )
                with self._lock:
                    # 加上 seek 偏移量，得到视频绝对时间
                    current = self._seek_offset + relative_time
                    self.current_time = current
                    if self.duration > 0:
                        self.progress = min(current / self.duration * 100, 100)

                # 每 10 秒保存一次秒级进度
                now = time.time()
                if now - last_progress_save >= 10:
                    self.playlist.save_progress_with_position(current)
                    last_progress_save = now

            # 解析推流码率
            m = self._RE_BITRATE.search(text)
            if m:
                with self._lock:
                    self.bitrate = m.group(1)

            # 解析编码速度
            m = self._RE_SPEED.search(text)
            if m:
                with self._lock:
                    self.speed = m.group(1)

            # 关键日志输出
            if any(kw in text for kw in ("Error", "error", "Warning", "Opening", "Output", "Stream")):
                log.warning("[ffmpeg] %s", text)

    def _cleanup(self):
        """清理 FFmpeg 进程"""
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            log.info("FFmpeg 进程已终止")

    # ── 自检程序 ──────────────────────────────────

    def _trigger_diagnosis(self, video_name: str, reason: str) -> bool:
        """触发自检，发送报告邮件。返回 True 表示建议停止推流。"""
        # 冷却：60 秒内不重复自检
        now = time.time()
        if now - self._last_diagnosis_time < 60:
            log.info("自检冷却中，跳过本次自检")
            return False
        self._last_diagnosis_time = now

        log.info("═" * 40)
        log.info("🔍 开始推流自检... 原因: %s", reason)
        log.info("═" * 40)

        report = self._run_diagnosis()
        report["video"] = video_name
        report["reason"] = reason
        report["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        report["total_failures"] = self._total_failures

        # 输出到日志
        for item in report["checks"]:
            icon = "✅" if item["ok"] else "❌"
            log.info("%s %s: %s", icon, item["name"], item["detail"])

        # 生成 HTML 并发送邮件
        html = self._format_report_html(report)
        self._notify_email("🔍 推流自检报告", html, is_html=True)

        # 如果 RTMP 连接和推流码都失败，或直播间未开播，尝试自动重新获取推流码并开播
        rtmp_ok = next((c["ok"] for c in report["checks"] if c["id"] == "rtmp"), True)
        key_ok = next((c["ok"] for c in report["checks"] if c["id"] == "stream_key"), True)
        live_ok = next((c["ok"] for c in report["checks"] if c["id"] == "live_status"), True)
        
        if not rtmp_ok or not key_ok or not live_ok:
            reason_parts = []
            if not rtmp_ok: reason_parts.append("RTMP不可达")
            if not key_ok: reason_parts.append("推流码异常")
            if not live_ok: reason_parts.append("直播间未开播")
            log.error("⛔ %s，准备尝试依靠后台自动重新开播...", "、".join(reason_parts))
            if self._bili_cfg and self._bili_cfg.get("cookie"):
                reconnect_success = self._attempt_auto_restart()
                if reconnect_success:
                    log.info("✅ 后台自动重新开播成功！推流程序将继续。")
                    return False # 重新开播成功，不停止主循环推流
                else:
                    log.error("⛔ 自动重新开播失败，建议彻底停止推流")
                    return True
            else:
                log.error("⛔ 自动开播失败：未在 config.yaml 发现有效的 bilibili 配置")
                return True
                
        return False

    def _attempt_auto_restart(self) -> bool:
        """尝试使用 B 站 API 获取新的推流码进行恢复"""
        room_id = self._bili_cfg.get("room_id")
        area_id = self._bili_cfg.get("area_id")
        cookie_str = self._bili_cfg.get("cookie", "")
        
        match = re.search(r"bili_jct=([^;]+)", cookie_str)
        if not match:
            log.error("Cookie 中未找到 bili_jct (csrf_token)")
            return False
        csrf = match.group(1)
        
        url = "https://api.live.bilibili.com/room/v1/Room/startLive"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": cookie_str,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
            "Origin": "https://link.bilibili.com",
            "Referer": "https://link.bilibili.com/p/center/index"
        }
        
        data = {
            "room_id": room_id,
            "platform": "pc_link",
            "area_v2": area_id,
            "backup_stream": "0",
            "csrf_token": csrf,
            "csrf": csrf
        }
        
        log.info("🔄 正在请求 B 站开启直播...")
        try:
            resp = requests.post(url, headers=headers, data=data, timeout=10)
            resp_json = resp.json()
            
            # code 0 直接成功
            if resp_json.get("code") == 0:
                return self._apply_new_stream_config(resp_json)
                
            # code 60024 需要人脸认证
            elif resp_json.get("code") == 60024 or (resp_json.get("data") and resp_json["data"].get("qr")):
                qr_url = resp_json["data"].get("qr", "")
                log.warning("⚠️ 目标分区需要人脸认证，请查收邮件并扫码")
                
                # 发送邮件通知附带二维码地址
                qr_html = f'''
                <div style="background:#fff;padding:20px;border-radius:8px;text-align:center">
                    <h2 style="color:#fb7299">系统触发了重新开播，但需要进行人脸认证</h2>
                    <p style="color:#666">请用手机浏览器的扫一扫或者 B 站 APP 扫描并在手机端完成认证：</p>
                    <div style="margin:20px 0;">
                        <img src="https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={urllib.parse.quote(qr_url)}" alt="二维码" />
                    </div>
                    <p style="color:#999;font-size:12px;">如果无法显示图片，请直接复制这串链接去浏览器打开获取最新认证二维码：<br>{qr_url}</p>
                </div>
                '''
                self._notify_email("⚠️ 直播推流断线: 待人脸认证恢复", qr_html, is_html=True)
                
                # 轮询人脸认证状态
                face_auth_url = "https://api.live.bilibili.com/xlive/app-blink/v1/preLive/IsUserIdentifiedByFaceAuth"
                face_auth_data = {
                    "room_id": room_id,
                    "face_auth_code": "60024",
                    "csrf_token": csrf,
                    "csrf": csrf,
                    "visit_id": ""
                }
                
                is_verified = False
                max_attempts = 120 # 允许最多等待 120 秒，给你留出看邮件的时间
                attempts = 0
                
                log.info("⏳ 开始轮询人脸认证结果 (超时时间 120 秒)...")
                while not is_verified and attempts < max_attempts:
                    if not self._running:
                        return False # 如果用户手动点击了关闭就提前终止
                        
                    try:
                        auth_resp = requests.post(face_auth_url, headers=headers, data=face_auth_data, timeout=5)
                        auth_json = auth_resp.json()
                        if auth_json.get("code") == 0 and auth_json.get("data", {}).get("is_identified"):
                            log.info("✅ 检测到扫码人脸验证成功！继续开播流程")
                            is_verified = True
                            break
                    except Exception as e:
                        pass
                    
                    time.sleep(1)
                    attempts += 1
                    
                if is_verified:
                    # 重新调用 startLive
                    log.info("🔄 再次请求 B 站开启直播...")
                    resp2 = requests.post(url, headers=headers, data=data, timeout=10)
                    resp_json2 = resp2.json()
                    
                    if resp_json2.get("code") == 0:
                        self._notify_email("✅ 直播推流断线并重新开播成功", "系统检测到人脸验证通过，已成功获取到新的推流码，进程将自动恢复推流！", is_html=False)
                        return self._apply_new_stream_config(resp_json2)
                    else:
                        log.error(f"❌ 二次开播依然失败: {resp_json2}")
                        return False
                else:
                    log.error("❌ 人脸扫描验证超时，重连宣告失败")
                    return False
            else:
                log.error(f"❌ 开播请求失败: {resp_json}")
                return False
                
        except Exception as e:
            log.error(f"自动重连过程遇到错误: {e}")
            return False
            
    def _apply_new_stream_config(self, resp_json: dict) -> bool:
        """解析新的推流地址并热更到配置"""
        data = resp_json.get("data", {})
        rtmp_url = data.get("rtmp", {}).get("addr", "")
        rtmp_code = data.get("rtmp", {}).get("code", "")
        
        if not rtmp_url or not rtmp_code:
            return False
            
        with self._lock:
            # 内部更新缓存的 stream 项属性
            self.stream_cfg["rtmp_url"] = rtmp_url
            self.stream_cfg["stream_key"] = rtmp_code
            
        # 并将其写入 config.yaml 文件持久化
        try:
            with open("config.yaml", "r", encoding="utf-8") as f:
                full_config = yaml.safe_load(f)
                
            full_config["stream"]["rtmp_url"] = rtmp_url
            full_config["stream"]["stream_key"] = rtmp_code
            
            with open("config.yaml", "w", encoding="utf-8") as f:
                yaml.dump(full_config, f, allow_unicode=True, sort_keys=False)
                
            log.info(f"✅ 新的推流地址已更新并保存在 config.yaml。")
            return True
        except Exception as e:
            log.error(f"❌ 写入 config.yaml 数据失败，但已暂时在内存中更新: {e}")
            return True

    def _run_diagnosis(self) -> dict:
        """执行全部自检项目"""
        checks = []
        checks.append(self._check_network())
        checks.append(self._check_dns())
        checks.append(self._check_rtmp())
        checks.append(self._check_stream_key())
        checks.append(self._check_live_status())
        checks.append(self._check_webdav())
        checks.append(self._check_system())
        return {"checks": checks}

    def _check_network(self) -> dict:
        """检查网络连通性"""
        name = "网络连通性"
        try:
            sock = socket.create_connection(("114.114.114.114", 53), timeout=5)
            sock.close()
            return {"id": "network", "name": name, "ok": True, "detail": "公网可达"}
        except Exception as e:
            return {"id": "network", "name": name, "ok": False, "detail": f"公网不可达: {e}"}

    def _check_dns(self) -> dict:
        """检查 RTMP 域名 DNS 解析"""
        name = "DNS 解析"
        rtmp_url = self.stream_cfg.get("rtmp_url", "")
        host = urlparse(rtmp_url).hostname or "live-push.bilivideo.com"
        try:
            ip = socket.gethostbyname(host)
            return {"id": "dns", "name": name, "ok": True, "detail": f"{host} → {ip}"}
        except Exception as e:
            return {"id": "dns", "name": name, "ok": False, "detail": f"{host} 解析失败: {e}"}

    def _check_rtmp(self) -> dict:
        """检查 RTMP 服务器端口可达性"""
        name = "RTMP 连接"
        rtmp_url = self.stream_cfg.get("rtmp_url", "")
        host = urlparse(rtmp_url).hostname or "live-push.bilivideo.com"
        port = urlparse(rtmp_url).port or 1935
        try:
            sock = socket.create_connection((host, port), timeout=10)
            sock.close()
            return {"id": "rtmp", "name": name, "ok": True, "detail": f"{host}:{port} 可达"}
        except Exception as e:
            return {"id": "rtmp", "name": name, "ok": False, "detail": f"{host}:{port} 不可达: {e}"}

    def _check_stream_key(self) -> dict:
        """检查推流码是否有效（FFmpeg 推送 3 秒空流）"""
        name = "推流码验证"
        rtmp_url = self.stream_cfg["rtmp_url"] + self.stream_cfg["stream_key"]
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=3",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", "3",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac",
            "-f", "flv", rtmp_url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=15)
            if result.returncode == 0:
                return {"id": "stream_key", "name": name, "ok": True, "detail": "推流码有效，空流推送成功"}
            stderr = result.stderr.decode("utf-8", errors="replace")[-200:]
            return {"id": "stream_key", "name": name, "ok": False, "detail": f"推流失败: {stderr.strip()}"}
        except subprocess.TimeoutExpired:
            return {"id": "stream_key", "name": name, "ok": False, "detail": "推送超时（15 秒）"}
        except Exception as e:
            return {"id": "stream_key", "name": name, "ok": False, "detail": f"异常: {e}"}

    def _check_live_status(self) -> dict:
        """通过 B 站 API 检查直播间是否正在直播"""
        name = "直播间状态"
        room_id = self._bili_cfg.get("room_id") if self._bili_cfg else None
        if not room_id:
            return {"id": "live_status", "name": name, "ok": True, "detail": "未配置 room_id，跳过"}
        try:
            url = f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            if data.get("code") != 0:
                return {"id": "live_status", "name": name, "ok": False, "detail": f"API 返回错误: {data.get('message', '未知')}"}
            live_status = data.get("data", {}).get("live_status", 0)
            if live_status == 1:
                return {"id": "live_status", "name": name, "ok": True, "detail": f"房间 {room_id} 正在直播中"}
            else:
                return {"id": "live_status", "name": name, "ok": False, "detail": f"房间 {room_id} 未在直播 (status={live_status})"}
        except Exception as e:
            return {"id": "live_status", "name": name, "ok": False, "detail": f"查询失败: {e}"}

    def _check_webdav(self) -> dict:
        """检查 WebDAV 视频源可用性"""
        name = "WebDAV 连接"
        from sources.webdav import WebDAVSource
        for source in self.playlist.sources:
            if isinstance(source, WebDAVSource):
                try:
                    req = urllib.request.Request(source.url, method="HEAD")
                    resp = urllib.request.urlopen(req, timeout=10)
                    return {"id": "webdav", "name": name, "ok": True, "detail": f"{source.url} 可达 ({resp.status})"}
                except Exception as e:
                    return {"id": "webdav", "name": name, "ok": False, "detail": f"{source.url} 不可达: {e}"}
        return {"id": "webdav", "name": name, "ok": True, "detail": "未配置 WebDAV 源"}

    def _check_system(self) -> dict:
        """检查系统资源"""
        name = "系统资源"
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        issues = []
        if cpu > 90:
            issues.append(f"CPU 过高: {cpu}%")
        if mem.percent > 90:
            issues.append(f"内存过高: {mem.percent}%")
        if disk.percent > 95:
            issues.append(f"磁盘过满: {disk.percent}%")
        detail = f"CPU {cpu}% | 内存 {mem.percent}% | 磁盘 {disk.percent}%"
        if issues:
            detail += f" ⚠️ {', '.join(issues)}"
        return {"id": "system", "name": name, "ok": len(issues) == 0, "detail": detail}

    @staticmethod
    def _format_report_html(report: dict) -> str:
        """生成 HTML 格式自检报告"""
        rows = ""
        for c in report["checks"]:
            icon = "✅" if c["ok"] else "❌"
            color = "#22c55e" if c["ok"] else "#ef4444"
            status = "正常" if c["ok"] else "异常"
            rows += f"""
            <tr>
                <td style="padding:10px 14px;border-bottom:1px solid #333">{icon} {c['name']}</td>
                <td style="padding:10px 14px;border-bottom:1px solid #333;color:{color};font-weight:600">{status}</td>
                <td style="padding:10px 14px;border-bottom:1px solid #333;color:#999;font-size:13px">{c['detail']}</td>
            </tr>"""

        passed = sum(1 for c in report["checks"] if c["ok"])
        total = len(report["checks"])
        summary_color = "#22c55e" if passed == total else ("#f59e0b" if passed >= total - 1 else "#ef4444")

        return f"""
        <div style="font-family:'Inter',sans-serif;background:#0a0a0a;color:#fafafa;padding:30px;border-radius:8px">
            <h2 style="margin:0 0 6px;font-size:20px">🔍 推流自检报告</h2>
            <p style="color:#888;margin:0 0 20px;font-size:14px">{report['time']}</p>

            <div style="background:#111;border:1px solid #333;border-radius:8px;padding:16px;margin-bottom:20px">
                <p style="margin:0 0 6px"><strong>触发原因:</strong> {report['reason']}</p>
                <p style="margin:0 0 6px"><strong>当前视频:</strong> {report['video']}</p>
                <p style="margin:0"><strong>累计失败:</strong> {report['total_failures']} 次</p>
            </div>

            <table style="width:100%;border-collapse:collapse;background:#111;border:1px solid #333;border-radius:8px">
                <thead>
                    <tr style="border-bottom:1px solid #333">
                        <th style="padding:10px 14px;text-align:left;color:#888;font-size:12px;text-transform:uppercase">检查项</th>
                        <th style="padding:10px 14px;text-align:left;color:#888;font-size:12px;text-transform:uppercase">状态</th>
                        <th style="padding:10px 14px;text-align:left;color:#888;font-size:12px;text-transform:uppercase">详情</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>

            <div style="margin-top:20px;padding:14px;background:#111;border:1px solid #333;border-radius:8px;text-align:center">
                <span style="font-size:24px;font-weight:700;color:{summary_color}">{passed}/{total}</span>
                <span style="color:#888;margin-left:8px">项检查通过</span>
            </div>
        </div>
        """

    # ── 邮件通知 ──────────────────────────────────

    def _notify_email(self, title: str, content: str, is_html: bool = False):
        """发送邮件通知（异步，不阻塞推流）"""
        if not self.email_cfg.get("enabled", False):
            return
        threading.Thread(
            target=self._do_email, args=(self.email_cfg, title, content, is_html), daemon=True
        ).start()

    @staticmethod
    def _do_email(cfg: dict, title: str, content: str, is_html: bool = False):
        """实际发送邮件"""
        try:
            if is_html:
                msg = MIMEText(content, "html", "utf-8")
            else:
                body = f"{title}\n\n{content}\n\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = f"[直播推流] {title}"
            msg["From"] = cfg["from_addr"]
            msg["To"] = cfg["to_addr"]

            if cfg.get("ssl", True):
                smtp = smtplib.SMTP_SSL(cfg["host"], cfg.get("port", 465), timeout=30)
            else:
                smtp = smtplib.SMTP(cfg["host"], cfg.get("port", 25), timeout=30)
                smtp.starttls()
            smtp.login(cfg["from_addr"], cfg["password"])
            smtp.sendmail(cfg["from_addr"], cfg["to_addr"], msg.as_string())
            smtp.quit()
            log.info("邮件通知已发送至 %s", cfg["to_addr"])
        except Exception as e:
            log.warning("邮件发送失败: %s", e)
