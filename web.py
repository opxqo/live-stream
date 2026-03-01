"""Web 管理面板 — FastAPI + 认证"""

import logging
import os
import shutil
import time

import yaml
from fastapi import FastAPI, Request, UploadFile, File, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.security import APIKeyCookie

from sources.webdav import WebDAVSource
import auth

log = logging.getLogger(__name__)
app = FastAPI(title="直播推流管理")

# 由 init_app() 注入
_streamer = None
_config = None
_config_path = "config.yaml"


def init_app(streamer, config=None, config_path="config.yaml"):
    """注入推流器实例和配置引用"""
    global _streamer, _config, _config_path
    _streamer = streamer
    _config = config
    _config_path = config_path


# ── 认证辅助 (Depends 注入) ──

cookie_scheme = APIKeyCookie(name="token", auto_error=False)

def get_current_user(token: str = Depends(cookie_scheme)) -> dict:
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    user = auth.verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="凭证无效，请重新登录")
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权限：仅管理员可访问")
    return user


# ── 页面路由 ──

@app.get("/login", response_class=FileResponse)
async def login_page():
    return FileResponse("templates/login.html", media_type="text/html")


@app.get("/", response_class=FileResponse)
async def index(request: Request):
    token = request.cookies.get("token")
    if not token or not auth.verify_token(token):
        return RedirectResponse("/login", status_code=302)
    return FileResponse("templates/dashboard.html", media_type="text/html")


# ── 认证 API ──

@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    username = body.get("username", "")
    password = body.get("password", "")
    user = auth.authenticate(username, password)
    if not user:
        return JSONResponse({"ok": False, "msg": "用户名或密码错误"}, status_code=401)
    token = auth.create_token(user["username"], user["role"])
    resp = JSONResponse({"ok": True, "msg": "登录成功", "role": user["role"]})
    resp.set_cookie("token", token, httponly=True, max_age=86400 * 7, samesite="lax")
    log.info("用户登录: %s (%s)", username, user["role"])
    return resp


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True, "msg": "已登出"})
    resp.delete_cookie("token")
    return resp


@app.get("/api/me")
async def api_me(user: dict = Depends(get_current_user)):
    return user


# ── 用户管理 API（仅 admin） ──

@app.get("/api/users")
async def api_list_users(admin: dict = Depends(require_admin)):
    return auth.list_users()


@app.post("/api/users")
async def api_add_user(request: Request, admin: dict = Depends(require_admin)):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    if not username or not password:
        return JSONResponse({"ok": False, "msg": "用户名和密码不能为空"}, status_code=400)
    ok, msg = auth.add_user(username, password)
    return {"ok": ok, "msg": msg}


@app.delete("/api/users/{username}")
async def api_delete_user(username: str, admin: dict = Depends(require_admin)):
    ok, msg = auth.delete_user(username)
    return {"ok": ok, "msg": msg}


# ── 推流 API（需登录） ──

@app.get("/api/status")
async def get_status(user: dict = Depends(get_current_user)):
    if not _streamer:
        raise HTTPException(status_code=503, detail="推流服务未初始化")
    return _streamer.status


@app.get("/api/playlist")
async def get_playlist(user: dict = Depends(get_current_user)):
    if not _streamer:
        raise HTTPException(status_code=503, detail="推流服务未初始化")
    return _streamer.playlist.videos


@app.post("/api/skip")
async def skip_video(user: dict = Depends(get_current_user)):
    if not _streamer or not _streamer.is_running:
        return {"ok": False, "msg": "推流未运行"}
    _streamer.skip()
    log.info("Web 操作: 跳过当前视频")
    return {"ok": True, "msg": "已跳过当前视频"}


@app.post("/api/play/{index}")
async def play_video(index: int, user: dict = Depends(get_current_user)):
    if not _streamer or not _streamer.is_running:
        return {"ok": False, "msg": "推流未运行"}
    if _streamer.play(index):
        log.info("Web 操作: 播放第 %d 集", index + 1)
        return {"ok": True, "msg": f"正在切换到第 {index + 1} 集"}
    return {"ok": False, "msg": "无效的集数索引"}


@app.post("/api/seek")
async def seek_video(request: Request, user: dict = Depends(get_current_user)):
    if not _streamer or not _streamer.is_running:
        return {"ok": False, "msg": "推流未运行"}
    body = await request.json()
    position = float(body.get("position", 0))
    if _streamer.seek(position):
        log.info("Web 操作: 跳转到 %.1f 秒", position)
        return {"ok": True, "msg": f"正在跳转到 {int(position)}s"}
    return {"ok": False, "msg": "跳转失败"}


@app.post("/api/stop")
async def stop_stream(admin: dict = Depends(require_admin)):
    if not _streamer:
        return {"ok": False, "msg": "推流未初始化"}
    _streamer.stop()
    log.info("Web 操作: 停止推流")
    return {"ok": True, "msg": "推流已停止"}


@app.post("/api/start")
async def start_stream(admin: dict = Depends(require_admin)):
    if not _streamer:
        return {"ok": False, "msg": "推流未初始化"}
    if _streamer.is_running:
        return {"ok": False, "msg": "推流已在运行中"}
    _streamer.run_in_thread()
    log.info("Web 操作: 启动推流")
    return {"ok": True, "msg": "推流已启动"}


@app.get("/api/browse")
async def browse_dir(path: str = "/", user: dict = Depends(get_current_user)):
    if not _streamer:
        raise HTTPException(status_code=503, detail="推流服务未初始化")
    for source in _streamer.playlist.sources:
        if isinstance(source, WebDAVSource):
            items = source.list_dirs(path)
            return {"path": path, "items": items}
    return {"path": path, "items": []}


@app.post("/api/switch")
async def switch_dir(path: str = "/", user: dict = Depends(get_current_user)):
    if not _streamer:
        return {"ok": False, "msg": "推流未初始化"}
    _streamer.playlist.switch_path(path)
    _streamer.skip()
    log.info("Web 操作: 切换播放目录到 %s", path)
    return {"ok": True, "msg": f"已切换到 {path}，共 {_streamer.playlist.total} 个视频"}


# ── 画面设置 API（仅 admin） ──

def _save_config():
    """将当前 config 写回 yaml 文件"""
    with open(_config_path, "w", encoding="utf-8") as f:
        yaml.dump(_config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


@app.get("/api/overlay")
async def get_overlay(user: dict = Depends(get_current_user)):
    logo = _config.get("logo", {}) if _config else {}
    overlay = _config.get("overlay", []) if _config else []
    clock = _config.get("clock", {}) if _config else {}
    images = _config.get("images", []) if _config else []
    webcam = _config.get("webcam", {}) if _config else {}
    return {"logo": logo, "overlay": overlay, "clock": clock, "images": images, "webcam": webcam}


@app.post("/api/overlay")
async def save_overlay(request: Request, admin: dict = Depends(require_admin)):
    body = await request.json()

    # 更新 config
    if "logo" in body:
        _config["logo"] = body["logo"]
        if _streamer:
            _streamer.logo_cfg = body["logo"]
    if "overlay" in body:
        _config["overlay"] = body["overlay"]
        if _streamer:
            _streamer.overlay_cfg = body["overlay"]
    if "clock" in body:
        _config["clock"] = body["clock"]
        if _streamer:
            _streamer.clock_cfg = body["clock"]
    if "images" in body:
        _config["images"] = body["images"]
        if _streamer:
            _streamer.images_cfg = body["images"]
    if "webcam" in body:
        _config["webcam"] = body["webcam"]
        if _streamer:
            _streamer.webcam_cfg = body["webcam"]

    _save_config()
    log.info("Web 操作: 更新画面设置")
    
    if _streamer and _streamer.is_running:
        log.info("已触发生效，重载当前帧的视频流...")
        _streamer.seek(_streamer.current_time)
        return {"ok": True, "msg": "画面设置已保存，直播间画面将在几秒内刷新"}

    return {"ok": True, "msg": "画面设置已保存，下一个视频生效"}


@app.get("/api/platform")
async def get_platform(user: dict = Depends(get_current_user)):
    stream = _config.get("stream", {}) if _config else {}
    bili = _config.get("bilibili", {}) if _config else {}
    huya = _config.get("huya", {}) if _config else {}
    custom = _config.get("custom", {}) if _config else {}
    return {"stream": stream, "bilibili": bili, "huya": huya, "custom": custom}


@app.post("/api/platform")
async def save_platform(request: Request, admin: dict = Depends(require_admin)):
    body = await request.json()
    platform = body.get("stream", {}).get("active_platform", "bilibili")

    # 更新内存配置
    if "stream" in body:
        _config["stream"] = body["stream"]
        if _streamer:
            _streamer.stream_cfg = body["stream"]
            _streamer._active_platform = platform
    if "bilibili" in body:
        _config["bilibili"] = body["bilibili"]
        if _streamer:
            _streamer._bili_cfg = body["bilibili"]
    if "huya" in body:
        _config["huya"] = body["huya"]
    if "custom" in body:
        _config["custom"] = body["custom"]

    # 持久化：全量配置写入 + 各平台推流信息独立存档
    _save_config()
    if _streamer:
        rtmp_url = body.get("stream", {}).get("rtmp_url", "")
        stream_key = body.get("stream", {}).get("stream_key", "")
        extra = body.get("bilibili") if platform == "bilibili" else None
        _streamer._persist_platform_config(platform, rtmp_url, stream_key, extra)

    log.info("Web 操作: 更新推流平台设置 [%s]", platform)

    if _streamer and _streamer.is_running:
        log.info("新配置准备生效，正在无缝切换推流器...")
        t0 = time.time()
        _streamer._restart_pusher()
        elapsed = round((time.time() - t0) * 1000)
        return {"ok": True, "msg": f"✅ 平台已无缝切换！耗时 {elapsed}ms", "elapsed_ms": elapsed}

    return {"ok": True, "msg": "推流平台已保存（当前未推流）"}


@app.post("/api/upload-logo")
async def upload_logo(admin: dict = Depends(require_admin), file: UploadFile = File(...)):
    # 确保 images 目录存在
    os.makedirs("images", exist_ok=True)
    
    ext = os.path.splitext(file.filename)[1] or ".png"
    # 保存到 images 目录
    save_name = f"images/台标{ext}"
    with open(save_name, "wb") as f:
        shutil.copyfileobj(file.file, f)
    # 更新 config 中的路径
    if "logo" not in _config:
        _config["logo"] = {}
    _config["logo"]["path"] = save_name
    if _streamer:
        _streamer.logo_cfg = _config["logo"]
    _save_config()
    log.info("Web 操作: 上传台标 %s", save_name)
    return {"ok": True, "msg": f"台标已上传: {save_name}", "path": save_name}

@app.post("/api/upload-image")
async def upload_image(admin: dict = Depends(require_admin), file: UploadFile = File(...)):
    os.makedirs("images", exist_ok=True)
    
    ext = os.path.splitext(file.filename)[1] or ".png"
    save_name = f"images/overlay_{int(time.time()*1000)}{ext}"
    with open(save_name, "wb") as f:
        shutil.copyfileobj(file.file, f)
    
    log.info("Web 操作: 上传图片 %s", save_name)
    return {"ok": True, "msg": "上传成功", "path": save_name}

