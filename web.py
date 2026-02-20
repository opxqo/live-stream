"""Web 管理面板 — FastAPI + 认证"""

import logging
import os
import shutil
import time

import yaml
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

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


# ── 认证辅助 ──

def _get_user(request: Request) -> dict | None:
    token = request.cookies.get("token")
    if not token:
        return None
    return auth.verify_token(token)


def _require_admin(request: Request) -> dict | None:
    """返回用户信息，若非 admin 则返回 None"""
    user = _get_user(request)
    if user and user["role"] == "admin":
        return user
    return None


# ── 页面路由 ──

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return LOGIN_HTML


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return DASHBOARD_HTML


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
async def api_me(request: Request):
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    return user


# ── 用户管理 API（仅 admin） ──

@app.get("/api/users")
async def api_list_users(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "无权限"}, status_code=403)
    return auth.list_users()


@app.post("/api/users")
async def api_add_user(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "无权限"}, status_code=403)
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    if not username or not password:
        return JSONResponse({"ok": False, "msg": "用户名和密码不能为空"}, status_code=400)
    ok, msg = auth.add_user(username, password)
    return {"ok": ok, "msg": msg}


@app.delete("/api/users/{username}")
async def api_delete_user(username: str, request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "无权限"}, status_code=403)
    ok, msg = auth.delete_user(username)
    return {"ok": ok, "msg": msg}


# ── 推流 API（需登录） ──

@app.get("/api/status")
async def get_status(request: Request):
    if not _get_user(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not _streamer:
        return JSONResponse({"error": "推流服务未初始化"}, status_code=503)
    return _streamer.status


@app.get("/api/playlist")
async def get_playlist(request: Request):
    if not _get_user(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not _streamer:
        return JSONResponse({"error": "推流服务未初始化"}, status_code=503)
    return _streamer.playlist.videos


@app.post("/api/skip")
async def skip_video(request: Request):
    if not _get_user(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not _streamer or not _streamer.is_running:
        return {"ok": False, "msg": "推流未运行"}
    _streamer.skip()
    log.info("Web 操作: 跳过当前视频")
    return {"ok": True, "msg": "已跳过当前视频"}


@app.post("/api/play/{index}")
async def play_video(index: int, request: Request):
    if not _get_user(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not _streamer or not _streamer.is_running:
        return {"ok": False, "msg": "推流未运行"}
    if _streamer.play(index):
        log.info("Web 操作: 播放第 %d 集", index + 1)
        return {"ok": True, "msg": f"正在切换到第 {index + 1} 集"}
    return {"ok": False, "msg": "无效的集数索引"}


@app.post("/api/stop")
async def stop_stream(request: Request):
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    if user["role"] != "admin":
        return JSONResponse({"ok": False, "msg": "仅管理员可停止推流"}, status_code=403)
    if not _streamer:
        return {"ok": False, "msg": "推流未初始化"}
    _streamer.stop()
    log.info("Web 操作: 停止推流")
    return {"ok": True, "msg": "推流已停止"}


@app.post("/api/start")
async def start_stream(request: Request):
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    if user["role"] != "admin":
        return JSONResponse({"ok": False, "msg": "仅管理员可启动推流"}, status_code=403)
    if not _streamer:
        return {"ok": False, "msg": "推流未初始化"}
    if _streamer.is_running:
        return {"ok": False, "msg": "推流已在运行中"}
    _streamer.run_in_thread()
    log.info("Web 操作: 启动推流")
    return {"ok": True, "msg": "推流已启动"}


@app.get("/api/browse")
async def browse_dir(request: Request, path: str = "/"):
    if not _get_user(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not _streamer:
        return JSONResponse({"error": "推流服务未初始化"}, status_code=503)
    for source in _streamer.playlist.sources:
        if isinstance(source, WebDAVSource):
            items = source.list_dirs(path)
            return {"path": path, "items": items}
    return {"path": path, "items": []}


@app.post("/api/switch")
async def switch_dir(request: Request, path: str = "/"):
    if not _get_user(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
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
async def get_overlay(request: Request):
    if not _get_user(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    logo = _config.get("logo", {}) if _config else {}
    overlay = _config.get("overlay", []) if _config else []
    clock = _config.get("clock", {}) if _config else {}
    images = _config.get("images", []) if _config else []
    return {"logo": logo, "overlay": overlay, "clock": clock, "images": images}


@app.post("/api/overlay")
async def save_overlay(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "无权限"}, status_code=403)
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

    _save_config()
    log.info("Web 操作: 更新画面设置")
    return {"ok": True, "msg": "画面设置已保存，下一个视频生效"}


@app.post("/api/upload-logo")
async def upload_logo(request: Request, file: UploadFile = File(...)):
    if not _require_admin(request):
        return JSONResponse({"error": "无权限"}, status_code=403)
    # 保存到项目目录
    ext = os.path.splitext(file.filename)[1] or ".png"
    save_name = f"台标{ext}"
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
async def upload_image(request: Request, file: UploadFile = File(...)):
    if not _require_admin(request):
        return JSONResponse({"error": "无权限"}, status_code=403)
    # 保存到项目目录
    ext = os.path.splitext(file.filename)[1] or ".png"
    # 使用时间戳作为文件名，避免中文乱码和冲突
    save_name = f"overlay_{int(time.time()*1000)}{ext}"
    with open(save_name, "wb") as f:
        shutil.copyfileobj(file.file, f)
    
    log.info("Web 操作: 上传图片 %s", save_name)
    return {"ok": True, "msg": "上传成功", "path": save_name}


# ──────────────────────────────────────────────
# 登录页面 HTML（shadcn 风格）
# ──────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 — 直播推流管理</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --background: hsl(240, 10%, 3.9%);
    --foreground: hsl(0, 0%, 98%);
    --card: hsl(240, 10%, 3.9%);
    --card-foreground: hsl(0, 0%, 98%);
    --primary: hsl(222, 47%, 50%);
    --primary-foreground: hsl(0, 0%, 98%);
    --muted: hsl(240, 3.7%, 15.9%);
    --muted-foreground: hsl(240, 5%, 64.9%);
    --border: hsl(240, 3.7%, 15.9%);
    --input: hsl(240, 3.7%, 15.9%);
    --ring: hsl(222, 47%, 50%);
    --destructive: hsl(0, 62.8%, 30.6%);
    --radius: 0.5rem;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: 'Inter', -apple-system, sans-serif;
    background: var(--background);
    color: var(--foreground);
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .login-card {
    width: 90%;
    max-width: 380px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 40px 32px;
  }
  @media (max-width: 480px) {
    .login-card { padding: 28px 20px; }
    .login-card h1 { font-size: 20px; }
    .form-group input, .btn-primary { height: 44px; font-size: 15px; }
  }
  .login-card h1 {
    font-size: 24px;
    font-weight: 700;
    text-align: center;
    margin-bottom: 6px;
  }
  .login-card .subtitle {
    text-align: center;
    color: var(--muted-foreground);
    font-size: 14px;
    margin-bottom: 28px;
  }
  .form-group {
    margin-bottom: 18px;
  }
  .form-group label {
    display: block;
    font-size: 14px;
    font-weight: 500;
    margin-bottom: 6px;
  }
  .form-group input {
    width: 100%;
    height: 40px;
    padding: 0 12px;
    background: var(--background);
    border: 1px solid var(--input);
    border-radius: var(--radius);
    color: var(--foreground);
    font-size: 14px;
    font-family: inherit;
    outline: none;
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  .form-group input:focus {
    border-color: var(--ring);
    box-shadow: 0 0 0 2px hsla(222, 47%, 50%, 0.25);
  }
  .form-group input::placeholder { color: var(--muted-foreground); }
  .btn-primary {
    width: 100%;
    height: 40px;
    background: var(--primary);
    color: var(--primary-foreground);
    border: none;
    border-radius: var(--radius);
    font-size: 14px;
    font-weight: 600;
    font-family: inherit;
    cursor: pointer;
    transition: opacity 0.15s;
  }
  .btn-primary:hover { opacity: 0.9; }
  .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
  .error-msg {
    margin-top: 14px;
    padding: 10px 14px;
    background: hsl(0, 62.8%, 30.6%, 0.15);
    border: 1px solid hsl(0, 62.8%, 30.6%, 0.3);
    border-radius: var(--radius);
    color: hsl(0, 86%, 70%);
    font-size: 13px;
    display: none;
  }
</style>
</head>
<body>
<div class="login-card">
  <h1>📡 直播推流</h1>
  <div class="subtitle">登录管理面板</div>
  <div class="form-group">
    <label for="username">用户名</label>
    <input type="text" id="username" placeholder="请输入用户名" autocomplete="username">
  </div>
  <div class="form-group">
    <label for="password">密码</label>
    <input type="password" id="password" placeholder="请输入密码" autocomplete="current-password">
  </div>
  <button class="btn-primary" id="btnLogin" onclick="doLogin()">登 录</button>
  <div class="error-msg" id="errMsg"></div>
</div>
<script>
  document.getElementById('password').addEventListener('keydown', e => {
    if (e.key === 'Enter') doLogin();
  });
  async function doLogin() {
    const btn = document.getElementById('btnLogin');
    const err = document.getElementById('errMsg');
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value;
    if (!username || !password) { err.textContent = '请填写用户名和密码'; err.style.display = 'block'; return; }
    btn.disabled = true;
    btn.textContent = '登录中...';
    err.style.display = 'none';
    try {
      const res = await fetch('/api/login', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ username, password })
      });
      const d = await res.json();
      if (d.ok) { location.href = '/'; }
      else { err.textContent = d.msg; err.style.display = 'block'; }
    } catch(e) { err.textContent = '网络错误'; err.style.display = 'block'; }
    btn.disabled = false;
    btn.textContent = '登 录';
  }
</script>
</body>
</html>
"""

# ──────────────────────────────────────────────
# 管理面板 HTML（shadcn 风格）
# ──────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>直播推流管理面板</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --background: hsl(240, 10%, 3.9%);
    --foreground: hsl(0, 0%, 98%);
    --card: hsl(240, 10%, 3.9%);
    --card-foreground: hsl(0, 0%, 98%);
    --primary: hsl(222, 47%, 50%);
    --primary-foreground: hsl(0, 0%, 98%);
    --secondary: hsl(240, 3.7%, 15.9%);
    --secondary-foreground: hsl(0, 0%, 98%);
    --muted: hsl(240, 3.7%, 15.9%);
    --muted-foreground: hsl(240, 5%, 64.9%);
    --accent: hsl(240, 3.7%, 15.9%);
    --accent-foreground: hsl(0, 0%, 98%);
    --border: hsl(240, 3.7%, 15.9%);
    --input: hsl(240, 3.7%, 15.9%);
    --ring: hsl(222, 47%, 50%);
    --destructive: hsl(0, 62.8%, 30.6%);
    --success: hsl(142, 71%, 45%);
    --warning: hsl(48, 96%, 53%);
    --radius: 0.5rem;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: 'Inter', -apple-system, sans-serif;
    background: var(--background);
    color: var(--foreground);
    min-height: 100vh;
  }

  /* ── 顶栏 ── */
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 24px;
    height: 56px;
    border-bottom: 1px solid var(--border);
    background: var(--card);
    position: sticky; top: 0; z-index: 50;
  }
  .topbar-left {
    display: flex; align-items: center; gap: 10px;
    font-size: 16px; font-weight: 700;
  }
  .topbar-right {
    display: flex; align-items: center; gap: 14px;
  }
  .user-info {
    display: flex; align-items: center; gap: 8px;
    font-size: 13px; color: var(--muted-foreground);
  }
  .user-avatar {
    width: 28px; height: 28px;
    border-radius: 50%;
    background: var(--primary);
    display: flex; align-items: center; justify-content: center;
    font-size: 12px; font-weight: 700; color: #fff;
  }
  .role-badge {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 9999px;
    font-weight: 600;
  }
  .role-admin { background: hsl(262, 83%, 58%, 0.15); color: hsl(262, 83%, 68%); }
  .role-mod   { background: hsl(142, 71%, 45%, 0.15); color: hsl(142, 71%, 55%); }
  .btn-ghost {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--foreground);
    padding: 6px 14px;
    border-radius: var(--radius);
    font-size: 13px;
    font-family: inherit;
    cursor: pointer;
    transition: background 0.15s;
  }
  .btn-ghost:hover { background: var(--accent); }

  /* ── 容器 ── */
  .main-wrap {
    max-width: 720px;
    margin: 0 auto;
    padding: 24px 20px;
  }

  /* ── 卡片 ── */
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
    margin-bottom: 16px;
  }
  .card-title {
    font-size: 14px;
    font-weight: 600;
    margin-bottom: 14px;
    color: var(--foreground);
  }

  /* ── 状态行 ── */
  .status-row {
    display: flex;
    justify-content: space-between;
    padding: 8px 0;
    border-bottom: 1px solid var(--border);
    font-size: 14px;
  }
  .status-row:last-child { border: none; }
  .status-label { color: var(--muted-foreground); }
  .status-value { font-weight: 600; text-align: right; max-width: 320px; word-break: break-all; }
  .badge {
    display: inline-flex;
    align-items: center;
    padding: 2px 10px;
    border-radius: 9999px;
    font-size: 12px;
    font-weight: 600;
  }
  .badge-on  { background: hsl(142,71%,45%,0.15); color: hsl(142,71%,55%); }
  .badge-off { background: hsl(0,62.8%,30.6%,0.15); color: hsl(0,86%,70%); }

  /* ── 进度条 ── */
  .progress-wrap { margin: 14px 0 4px; }
  .progress-header {
    display: flex; justify-content: space-between;
    font-size: 13px; color: var(--muted-foreground);
    margin-bottom: 6px;
  }
  .progress-bg {
    width: 100%; height: 6px;
    background: var(--muted);
    border-radius: 3px; overflow: hidden;
  }
  .progress-fg {
    height: 100%; width: 0%;
    background: var(--primary);
    border-radius: 3px;
    transition: width 0.6s ease;
  }

  /* ── 监控卡片 ── */
  .monitor-grid {
    display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px;
    margin-bottom: 16px;
  }
  .monitor-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px;
    text-align: center;
  }
  .monitor-label { font-size: 12px; color: var(--muted-foreground); margin-bottom: 6px; }
  .monitor-value { font-size: 20px; font-weight: 700; }
  .monitor-value.cpu { color: hsl(142, 71%, 55%); }
  .monitor-value.mem { color: hsl(217, 91%, 65%); }
  .monitor-value.bps { color: hsl(48, 96%, 53%); }

  /* ── 按钮组 ── */
  .actions {
    display: flex; gap: 10px; margin-bottom: 20px;
  }
  .btn {
    flex: 1; height: 40px;
    border: none; border-radius: var(--radius);
    font-size: 14px; font-weight: 600;
    font-family: inherit;
    cursor: pointer;
    transition: opacity 0.15s;
    display: flex; align-items: center; justify-content: center; gap: 6px;
  }
  .btn:hover { opacity: 0.9; }
  .btn:disabled { opacity: 0.35; cursor: not-allowed; }
  .btn-blue   { background: var(--primary); color: #fff; }
  .btn-red    { background: hsl(0, 62.8%, 30.6%); color: hsl(0, 86%, 90%); }
  .btn-green  { background: hsl(142, 71%, 35%); color: hsl(142, 71%, 90%); }

  /* ── Tabs ── */
  .tabs {
    display: flex; gap: 0; margin-bottom: 16px;
    border-bottom: 1px solid var(--border);
  }
  .tab-btn {
    padding: 10px 20px;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--muted-foreground);
    font-size: 14px;
    font-weight: 500;
    font-family: inherit;
    cursor: pointer;
    transition: color 0.15s, border-color 0.15s;
  }
  .tab-btn.active {
    color: var(--foreground);
    border-bottom-color: var(--primary);
  }
  .tab-btn:hover { color: var(--foreground); }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }

  /* ── 目录浏览 ── */
  .breadcrumb {
    display: flex; align-items: center; gap: 4px;
    padding: 10px 14px;
    font-size: 13px; color: var(--muted-foreground);
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius) var(--radius) 0 0;
    border-bottom: none;
    flex-wrap: wrap;
  }
  .breadcrumb a {
    color: var(--primary); text-decoration: none; cursor: pointer;
  }
  .breadcrumb a:hover { text-decoration: underline; }
  .breadcrumb .sep { color: var(--muted-foreground); margin: 0 2px; }
  .dir-list {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 0 0 var(--radius) var(--radius);
    max-height: 380px;
    overflow-y: auto;
  }
  .dir-list::-webkit-scrollbar { width: 6px; }
  .dir-list::-webkit-scrollbar-thumb { background: var(--muted); border-radius: 3px; }
  .dir-item {
    display: flex; align-items: center;
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    transition: background 0.12s;
    font-size: 13px; gap: 10px;
  }
  .dir-item:last-child { border: none; }
  .dir-item:hover { background: var(--accent); }
  .dir-icon { font-size: 16px; flex-shrink: 0; }
  .dir-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dir-action {
    font-size: 11px; padding: 4px 12px;
    border-radius: var(--radius); border: none;
    cursor: pointer; font-weight: 600;
    background: var(--primary); color: #fff;
    transition: opacity 0.15s;
  }
  .dir-action:hover { opacity: 0.85; }

  /* ── 播放列表 ── */
  .playlist {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    max-height: 380px;
    overflow-y: auto;
  }
  .playlist::-webkit-scrollbar { width: 6px; }
  .playlist::-webkit-scrollbar-thumb { background: var(--muted); border-radius: 3px; }
  .ep-item {
    display: flex; align-items: center;
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    cursor: pointer; transition: background 0.12s;
    font-size: 13px; gap: 10px;
  }
  .ep-item:last-child { border: none; }
  .ep-item:hover { background: var(--accent); }
  .ep-item.playing { background: hsl(222, 47%, 50%, 0.1); }
  .ep-num {
    color: var(--muted-foreground); font-size: 12px;
    min-width: 28px; text-align: center; flex-shrink: 0;
  }
  .ep-name {
    flex: 1; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
  }
  .ep-badge {
    font-size: 11px; padding: 2px 8px;
    border-radius: 9999px;
    background: hsl(222, 47%, 50%, 0.15);
    color: hsl(222, 47%, 70%);
    font-weight: 600; flex-shrink: 0;
  }

  /* ── 用户管理 ── */
  .user-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
  }
  .user-table th {
    text-align: left;
    padding: 10px 14px;
    font-weight: 600;
    color: var(--muted-foreground);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    border-bottom: 1px solid var(--border);
  }
  .user-table td {
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
  }
  .user-table tr:last-child td { border: none; }
  .btn-sm {
    padding: 4px 12px;
    border-radius: var(--radius);
    border: 1px solid var(--border);
    background: transparent;
    color: hsl(0, 86%, 70%);
    font-size: 12px;
    font-family: inherit;
    cursor: pointer;
    transition: background 0.12s;
  }
  .btn-sm:hover { background: hsl(0, 62.8%, 30.6%, 0.2); }
  .add-user-form {
    display: flex; gap: 10px; margin-top: 14px;
  }
  .add-user-form input {
    flex: 1; height: 36px;
    padding: 0 12px;
    background: var(--background);
    border: 1px solid var(--input);
    border-radius: var(--radius);
    color: var(--foreground);
    font-size: 13px;
    font-family: inherit;
    outline: none;
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  .add-user-form input:focus {
    border-color: var(--ring);
    box-shadow: 0 0 0 2px hsla(222, 47%, 50%, 0.25);
  }
  .add-user-form input::placeholder { color: var(--muted-foreground); }
  .btn-add {
    height: 36px;
    padding: 0 18px;
    background: var(--primary);
    color: #fff;
    border: none;
    border-radius: var(--radius);
    font-size: 13px;
    font-weight: 600;
    font-family: inherit;
    cursor: pointer;
    transition: opacity 0.15s;
    white-space: nowrap;
  }
  .btn-add:hover { opacity: 0.9; }

  /* ── Toast ── */
  .toast {
    position: fixed; top: 16px; right: 16px;
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--foreground);
    padding: 12px 20px; border-radius: var(--radius);
    font-size: 14px; opacity: 0;
    transition: opacity 0.25s;
    pointer-events: none; z-index: 100;
    box-shadow: 0 4px 20px rgba(0,0,0,0.4);
  }
  .toast.show { opacity: 1; }

  .meta-line {
    text-align: center; margin-top: 16px;
    font-size: 12px; color: var(--muted-foreground);
  }
  .loading   { text-align: center; padding: 30px; color: var(--muted-foreground); font-size: 13px; }
  .empty-hint { text-align: center; padding: 30px; color: var(--muted-foreground); font-size: 13px; }

  /* ── 画面设置 ── */
  .form-section {
    margin-bottom: 20px;
  }
  .form-section-title {
    font-size: 14px; font-weight: 600;
    margin-bottom: 12px;
    display: flex; align-items: center; gap: 8px;
  }
  .form-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
  }
  .form-group-sm {
    display: flex; flex-direction: column; gap: 4px;
  }
  .form-group-sm label {
    font-size: 12px; color: var(--muted-foreground); font-weight: 500;
  }
  .form-group-sm input, .form-group-sm select {
    height: 36px; padding: 0 10px;
    background: var(--background);
    border: 1px solid var(--input);
    border-radius: var(--radius);
    color: var(--foreground);
    font-size: 13px; font-family: inherit;
    outline: none;
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  .form-group-sm input:focus, .form-group-sm select:focus {
    border-color: var(--ring);
    box-shadow: 0 0 0 2px hsla(222, 47%, 50%, 0.25);
  }
  .form-group-full {
    grid-column: 1 / -1;
  }
  .overlay-item {
    background: var(--background);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px;
    margin-bottom: 10px;
    position: relative;
  }
  .overlay-item-header {
    display: flex; justify-content: space-between;
    align-items: center; margin-bottom: 10px;
  }
  .overlay-item-title {
    font-size: 13px; font-weight: 600; color: var(--muted-foreground);
  }
  .btn-icon {
    width: 28px; height: 28px;
    border-radius: var(--radius);
    border: 1px solid var(--border);
    background: transparent; color: hsl(0,86%,70%);
    cursor: pointer; font-size: 14px;
    display: flex; align-items: center; justify-content: center;
    transition: background 0.12s;
  }
  .btn-icon:hover { background: hsl(0,62.8%,30.6%,0.2); }
  .btn-outline {
    height: 36px; padding: 0 16px;
    border: 1px dashed var(--border);
    background: transparent;
    color: var(--muted-foreground);
    border-radius: var(--radius);
    font-size: 13px; font-family: inherit;
    cursor: pointer;
    transition: border-color 0.15s, color 0.15s;
    width: 100%;
  }
  .btn-outline:hover { border-color: var(--primary); color: var(--primary); }
  .save-bar {
    display: flex; gap: 10px; margin-top: 16px;
  }
  .save-bar .btn { flex: none; width: auto; padding: 0 24px; }
  .upload-area {
    border: 1px dashed var(--border);
    border-radius: var(--radius);
    padding: 20px;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.15s;
    color: var(--muted-foreground);
    font-size: 13px;
  }
  .upload-area:hover { border-color: var(--primary); }
  .upload-area input[type=file] { display: none; }
  .switch-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 0;
  }
  .switch {
    width: 40px; height: 22px;
    background: var(--muted); border-radius: 11px;
    position: relative; cursor: pointer;
    transition: background 0.2s;
    border: none;
  }
  .switch::after {
    content: ''; position: absolute;
    width: 18px; height: 18px;
    border-radius: 50%; background: #fff;
    top: 2px; left: 2px;
    transition: transform 0.2s;
  }
  .switch.on { background: var(--primary); }
  .switch.on::after { transform: translateX(18px); }

  /* ── 屏幕预览 ── */
  .preview-wrap {
    margin-bottom: 16px;
  }
  .preview-header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 8px;
  }
  .preview-title {
    font-size: 14px; font-weight: 600;
    display: flex; align-items: center; gap: 8px;
  }
  .preview-hint {
    font-size: 11px; color: var(--muted-foreground);
  }
  .preview-canvas-wrap {
    position: relative;
    background: #111;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    aspect-ratio: 16 / 9;
    width: 100%;
  }
  .preview-canvas-wrap canvas {
    width: 100%; height: 100%; display: block;
  }

  /* ── 颜色预设 ── */
  .color-presets {
    display: flex; gap: 5px; margin-top: 6px; flex-wrap: wrap;
  }
  .color-dot {
    width: 22px; height: 22px;
    border-radius: 50%;
    border: 2px solid var(--border);
    cursor: pointer;
    transition: transform 0.12s, border-color 0.12s;
    position: relative;
  }
  .color-dot:hover {
    transform: scale(1.2);
    border-color: var(--primary);
  }
  .color-dot.active {
    border-color: var(--primary);
    box-shadow: 0 0 0 2px hsla(222, 47%, 50%, 0.4);
  }

  /* ── 位置预设 ── */
  .pos-presets {
    display: flex; gap: 4px; margin-top: 6px; flex-wrap: wrap;
  }
  .pos-btn {
    padding: 3px 8px;
    font-size: 11px;
    border-radius: var(--radius);
    border: 1px solid var(--border);
    background: transparent;
    color: var(--muted-foreground);
    cursor: pointer;
    font-family: inherit;
    transition: all 0.12s;
  }
  .pos-btn:hover {
    border-color: var(--primary);
    color: var(--primary);
    background: hsla(222, 47%, 50%, 0.08);
  }

  /* ── 移动端适配 ── */
  @media (max-width: 640px) {
    /* 顶栏 */
    .topbar { padding: 0 10px; height: 48px; }
    .topbar-left { font-size: 14px; gap: 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
    .topbar-right { gap: 6px; flex-shrink: 0; }
    .user-info { gap: 4px; font-size: 11px; }
    .user-info > #uName { display: none; }
    .user-avatar { width: 24px; height: 24px; font-size: 10px; }
    .role-badge { font-size: 10px; padding: 2px 6px; }
    .btn-ghost { padding: 4px 8px; font-size: 11px; }

    /* 主容器 */
    .main-wrap { padding: 12px 10px; }

    /* 卡片 */
    .card { padding: 14px; margin-bottom: 12px; }
    .card-title { font-size: 13px; margin-bottom: 10px; }

    /* 状态行 */
    .status-row { font-size: 13px; padding: 6px 0; flex-wrap: wrap; gap: 4px; }
    .status-value { max-width: 100%; font-size: 13px; }

    /* 监控卡片 */
    .monitor-grid { grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
    .monitor-card { padding: 10px 4px; overflow: hidden; }
    .monitor-label { font-size: 11px; margin-bottom: 4px; }
    .monitor-value { font-size: 14px; word-break: break-all; }

    /* 操作按钮 */
    .actions { gap: 6px; margin-bottom: 14px; }
    .btn { height: 42px; font-size: 13px; border-radius: 8px; }

    /* Tab 导航 */
    .tabs { overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width: none; }
    .tabs::-webkit-scrollbar { display: none; }
    .tab-btn { padding: 8px 14px; font-size: 13px; white-space: nowrap; flex-shrink: 0; }

    /* 播放列表 / 目录 */
    .ep-item, .dir-item { padding: 12px 10px; font-size: 13px; min-height: 44px; }
    .ep-num { min-width: 24px; font-size: 11px; }
    .dir-action { padding: 6px 10px; font-size: 11px; }
    .playlist, .dir-list { max-height: 320px; }

    /* 画面设置表单 */
    .form-grid { grid-template-columns: 1fr; gap: 10px; }
    .form-section-title { font-size: 13px; }
    .form-group-sm input, .form-group-sm select { height: 40px; font-size: 14px; }
    .preview-header { flex-direction: column; align-items: flex-start; gap: 4px; }
    .preview-hint { font-size: 10px; }
    .save-bar { flex-direction: column; }
    .save-bar .btn { width: 100%; }

    /* 图片/文字叠加 */
    .overlay-item { padding: 10px; }
    .btn-outline { height: 40px; font-size: 13px; }

    /* 用户管理 */
    .add-user-form { flex-direction: column; gap: 8px; }
    .add-user-form input { height: 40px; font-size: 14px; }
    .btn-add { height: 40px; width: 100%; }
    .user-table { font-size: 13px; }
    .user-table th, .user-table td { padding: 8px 10px; }

    /* Toast */
    .toast { top: auto; bottom: 20px; right: 50%; transform: translateX(50%); max-width: 85vw; text-align: center; }
    .toast.show { opacity: 1; }

    /* 上传区域 */
    .upload-area { padding: 14px; font-size: 12px; }
    .breadcrumb { font-size: 12px; padding: 8px 10px; }

    /* 颜色/位置预设 */
    .color-dot { width: 26px; height: 26px; }
    .pos-btn { padding: 5px 10px; font-size: 11px; }
  }

  @media (max-width: 360px) {
    .topbar-left { font-size: 13px; }
    .monitor-grid { grid-template-columns: 1fr 1fr 1fr; gap: 6px; }
    .monitor-value { font-size: 14px; }
    .actions { flex-direction: column; }
    .btn { width: 100%; }
  }
</style>
</head>
<body>

<!-- 顶栏 -->
<div class="topbar">
  <div class="topbar-left">📡 直播推流管理</div>
  <div class="topbar-right">
    <div class="user-info">
      <div class="user-avatar" id="uAvatar">A</div>
      <span id="uName">—</span>
      <span class="role-badge" id="uRole">—</span>
    </div>
    <button class="btn-ghost" onclick="doLogout()">登出</button>
  </div>
</div>

<div class="main-wrap">

  <!-- 状态卡片 -->
  <div class="card">
    <div class="card-title">推流状态</div>
    <div class="status-row"><span class="status-label">状态</span><span class="status-value" id="sRunning">—</span></div>
    <div class="status-row"><span class="status-label">当前播放</span><span class="status-value" id="sVideo">—</span></div>
    <div class="status-row"><span class="status-label">已播放</span><span class="status-value" id="sPlayed">—</span></div>
    <div class="status-row"><span class="status-label">运行时长</span><span class="status-value" id="sUptime">—</span></div>
    <div class="progress-wrap">
      <div class="progress-header">
        <span id="pTime">00:00 / 00:00</span>
        <span id="pPercent">0%</span>
      </div>
      <div class="progress-bg"><div class="progress-fg" id="pBar"></div></div>
    </div>
  </div>

  <!-- 监控卡片 -->
  <div class="monitor-grid">
    <div class="monitor-card">
      <div class="monitor-label">CPU</div>
      <div class="monitor-value cpu" id="mCpu">—</div>
    </div>
    <div class="monitor-card">
      <div class="monitor-label">内存</div>
      <div class="monitor-value mem" id="mMem">—</div>
    </div>
    <div class="monitor-card">
      <div class="monitor-label">码率</div>
      <div class="monitor-value bps" id="mBps">—</div>
    </div>
  </div>

  <!-- 操作按钮 -->
  <div class="actions">
    <button class="btn btn-blue" id="btnSkip" onclick="action('skip')">⏭ 下一集</button>
    <button class="btn btn-red" id="btnStop" onclick="doStop()" style="display:none">⏹ 停止</button>
    <button class="btn btn-green" id="btnStart" onclick="action('start')" style="display:none">▶ 启动</button>
  </div>

  <!-- Tab 切换 -->
  <div class="tabs" id="tabBar">
    <button class="tab-btn active" onclick="switchTab('playlist')">播放列表</button>
    <button class="tab-btn" onclick="switchTab('browser')">目录浏览</button>
    <button class="tab-btn" onclick="switchTab('overlay')" id="tabOverlay" style="display:none">画面设置</button>
    <button class="tab-btn" onclick="switchTab('users')" id="tabUsers" style="display:none">用户管理</button>
  </div>

  <!-- 播放列表 Tab -->
  <div class="tab-panel active" id="tab-playlist">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <span style="font-size:13px;color:var(--muted-foreground)" id="epCount"></span>
    </div>
    <div class="playlist" id="playlist"></div>
  </div>

  <!-- 目录浏览 Tab -->
  <div class="tab-panel" id="tab-browser">
    <div class="breadcrumb" id="breadcrumb"></div>
    <div class="dir-list" id="dirList"><div class="loading">加载中...</div></div>
  </div>

  <!-- 用户管理 Tab -->
  <div class="tab-panel" id="tab-users">
    <div class="card">
      <div class="card-title">房管列表</div>
      <table class="user-table">
        <thead><tr><th>用户名</th><th>角色</th><th>操作</th></tr></thead>
        <tbody id="userList"></tbody>
      </table>
      <div class="add-user-form">
        <input type="text" id="newUsername" placeholder="用户名">
        <input type="password" id="newPassword" placeholder="密码">
        <button class="btn-add" onclick="addUser()">添加房管</button>
      </div>
    </div>
  </div>

  <!-- 画面设置 Tab -->
  <div class="tab-panel" id="tab-overlay">

    <!-- 屏幕预览 -->
    <div class="card preview-wrap">
      <div class="preview-header">
        <span class="preview-title">📺 画面预览</span>
        <span class="preview-hint">实时显示 Logo、文字、时钟位置（编辑后自动刷新）</span>
      </div>
      <div class="preview-canvas-wrap">
        <canvas id="previewCanvas" width="960" height="540"></canvas>
      </div>
    </div>

    <!-- Logo 设置 -->
    <div class="card">
      <div class="form-section">
        <div class="form-section-title">🎨 台标 / Logo</div>
        <div class="upload-area" onclick="document.getElementById('logoFile').click()">
          <input type="file" id="logoFile" accept="image/*" onchange="uploadLogo(this)">
          <div id="logoStatus">📁 点击上传台标图片（PNG / JPG）</div>
        </div>
        <div class="form-grid" style="margin-top:12px">
          <div class="form-group-sm">
            <label>图片路径</label>
            <input type="text" id="logoPath" placeholder="台标.png">
          </div>
          <div class="form-group-sm">
            <label>缩放高度 (px)</label>
            <input type="number" id="logoHeight" value="80" min="20" max="500">
          </div>
          <div class="form-group-sm">
            <label>水平位置 X</label>
            <input type="text" id="logoX" value="20" placeholder="20">
          </div>
          <div class="form-group-sm">
            <label>垂直位置 Y</label>
            <input type="text" id="logoY" value="20" placeholder="20">
          </div>
          <div class="form-group-sm">
            <label>不透明度 (0~1)</label>
            <input type="number" id="logoOpacity" value="0.8" min="0" max="1" step="0.1">
          </div>
        </div>
      </div>
    </div>

    <!-- 图片叠加 (多图) -->
        <div class="form-section-title">🖼️ 图片叠加 (多图)</div>
        <div id="imageList"></div>
        <div class="form-section" style="margin-top:12px; border-top:1px solid var(--border); padding-top:12px; display: flex; gap: 10px;">
           <input type="file" id="newImgUpload" accept="image/*" style="display:none" onchange="uploadAndAddImage(this)">
           <button class="btn-secondary full-width" onclick="document.getElementById('newImgUpload').click()">+ 上传本地图片</button>
           <button class="btn-secondary full-width" onclick="addUrlImageItem()">🌐 + 添加网络图片 (URL)</button>
      </div>
    </div>

    <!-- 时钟设置 -->
    <div class="card">
      <div class="form-section">
        <div class="form-section-title">🕐 实时时钟</div>
        <div class="switch-row">
          <span style="font-size:13px">启用右上角时钟</span>
          <button class="switch on" id="clockSwitch" onclick="toggleClock()"></button>
        </div>
        <div class="form-grid" id="clockFields">
          <div class="form-group-sm">
            <label>字号</label>
            <input type="number" id="clockFontsize" value="24" min="10" max="72">
          </div>
          <div class="form-group-sm">
            <label>字体颜色</label>
            <input type="text" id="clockFontcolor" value="white@0.8" placeholder="white@0.8" oninput="refreshPreview()">
            <div class="color-presets" data-target="clockFontcolor"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- 文字叠加 -->
    <div class="card">
      <div class="form-section">
        <div class="form-section-title">✏️ 文字叠加</div>
        <div id="overlayList"></div>
        <button class="btn-outline" onclick="addOverlayItem()">+ 添加文字叠加</button>
      </div>
    </div>

    <!-- 保存 -->
    <div class="save-bar">
      <button class="btn btn-blue" onclick="saveOverlay()">💾 保存设置</button>
      <button class="btn btn-ghost" onclick="loadOverlay()" style="border:1px solid var(--border)">🔄 重新加载</button>
    </div>

  </div>

  <div class="meta-line">状态每 3 秒自动刷新</div>
</div>

<div class="toast" id="toast"></div>

<script>
  let currentVideo = '';
  let currentBrowsePath = '/';
  let currentUser = null;

  function fmtTime(sec) {
    if (!sec || sec <= 0) return '00:00';
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0');
  }

  // ── 初始化 ──
  async function init() {
    try {
      const res = await fetch('/api/me');
      if (!res.ok) { location.href = '/login'; return; }
      currentUser = await res.json();
      document.getElementById('uName').textContent = currentUser.username;
      document.getElementById('uAvatar').textContent = currentUser.username[0].toUpperCase();
      const badge = document.getElementById('uRole');
      if (currentUser.role === 'admin') {
        badge.textContent = '管理员';
        badge.className = 'role-badge role-admin';
        document.getElementById('btnStop').style.display = '';
        document.getElementById('btnStart').style.display = '';
        document.getElementById('tabUsers').style.display = '';
        document.getElementById('tabOverlay').style.display = '';
      } else {
        badge.textContent = '房管';
        badge.className = 'role-badge role-mod';
      }
    } catch(e) { location.href = '/login'; }
  }

  // ── Tab 切换 ──
  function switchTab(name) {
    const tabs = document.querySelectorAll('.tab-btn');
    const panels = { playlist: 0, browser: 1, overlay: 2, users: 3 };
    tabs.forEach((b, i) => b.classList.toggle('active', i === panels[name]));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.getElementById('tab-' + name).classList.add('active');
    if (name === 'browser') browseTo('/');
    if (name === 'users') fetchUsers();
    if (name === 'overlay') loadOverlay();
  }

  // ── 状态轮询 ──
  async function fetchStatus() {
    try {
      const res = await fetch('/api/status');
      if (!res.ok) return;
      const d = await res.json();
      currentVideo = d.current_video || '';
      document.getElementById('sRunning').innerHTML = d.running
        ? '<span class="badge badge-on">运行中</span>'
        : '<span class="badge badge-off">已停止</span>';
      document.getElementById('sVideo').textContent = currentVideo || '—';
      document.getElementById('sPlayed').textContent = d.videos_played + ' / ' + d.playlist_total;
      document.getElementById('sUptime').textContent = d.uptime || '—';
      document.getElementById('btnSkip').disabled = !d.running;
      if (currentUser && currentUser.role === 'admin') {
        document.getElementById('btnStop').disabled = !d.running;
        document.getElementById('btnStart').disabled = d.running;
      }
      const pct = d.progress || 0;
      document.getElementById('pBar').style.width = pct + '%';
      document.getElementById('pPercent').textContent = pct.toFixed(1) + '%';
      document.getElementById('pTime').textContent = fmtTime(d.current_time) + ' / ' + fmtTime(d.duration);
      document.getElementById('mCpu').textContent = (d.cpu_percent || 0).toFixed(1) + '%';
      document.getElementById('mMem').textContent = (d.memory_percent || 0).toFixed(1) + '%';
      document.getElementById('mBps').textContent = d.bitrate || '—';
    } catch(e) { console.error(e); }
  }

  async function fetchPlaylist() {
    try {
      const res = await fetch('/api/playlist');
      if (!res.ok) return;
      const list = await res.json();
      document.getElementById('epCount').textContent = '共 ' + list.length + ' 个视频';
      const el = document.getElementById('playlist');
      el.innerHTML = list.map(v => {
        const playing = v.name === currentVideo;
        return '<div class="ep-item' + (playing ? ' playing' : '') + '" onclick="playEp(' + v.index + ')">'
          + '<span class="ep-num">' + (v.index + 1) + '</span>'
          + '<span class="ep-name">' + escHtml(v.name) + '</span>'
          + (playing ? '<span class="ep-badge">播放中</span>' : '')
          + '</div>';
      }).join('');
    } catch(e) { console.error(e); }
  }

  // ── 目录浏览 ──
  async function browseTo(path) {
    currentBrowsePath = path;
    renderBreadcrumb(path);
    const el = document.getElementById('dirList');
    el.innerHTML = '<div class="loading">加载中...</div>';
    try {
      const res = await fetch('/api/browse?path=' + encodeURIComponent(path));
      const d = await res.json();
      const items = d.items || [];
      if (items.length === 0) { el.innerHTML = '<div class="empty-hint">此目录为空</div>'; return; }
      el.innerHTML = items.map(item => {
        if (item.type === 'dir') {
          return '<div class="dir-item" onclick="browseTo(\\''+escAttr(item.path)+'\\')">'
            + '<span class="dir-icon">📁</span>'
            + '<span class="dir-name">' + escHtml(item.name) + '</span>'
            + '<button class="dir-action" onclick="event.stopPropagation();switchDir(\\''+escAttr(item.path)+'\\')">▶ 播放</button>'
            + '</div>';
        } else {
          return '<div class="dir-item">'
            + '<span class="dir-icon">🎬</span>'
            + '<span class="dir-name">' + escHtml(item.name) + '</span>'
            + '</div>';
        }
      }).join('');
    } catch(e) { el.innerHTML = '<div class="empty-hint">加载失败</div>'; }
  }

  function renderBreadcrumb(path) {
    const el = document.getElementById('breadcrumb');
    const parts = path.split('/').filter(Boolean);
    let html = '<a onclick="browseTo(\\'/\\')">🏠 根目录</a>';
    let acc = '';
    for (const p of parts) {
      acc += '/' + p;
      const target = acc;
      html += '<span class="sep">/</span><a onclick="browseTo(\\''+escAttr(target)+'\\')">' + escHtml(p) + '</a>';
    }
    el.innerHTML = html;
  }

  async function switchDir(path) {
    try {
      const res = await fetch('/api/switch?path=' + encodeURIComponent(path), { method: 'POST' });
      const d = await res.json();
      showToast(d.msg);
      switchTab('playlist');
      setTimeout(() => { fetchStatus(); fetchPlaylist(); }, 2000);
    } catch(e) { showToast('请求失败'); }
  }

  // ── 用户管理 ──
  async function fetchUsers() {
    try {
      const res = await fetch('/api/users');
      if (!res.ok) return;
      const users = await res.json();
      const el = document.getElementById('userList');
      if (users.length === 0) {
        el.innerHTML = '<tr><td colspan="3" style="text-align:center;color:var(--muted-foreground);padding:20px">暂无房管</td></tr>';
        return;
      }
      el.innerHTML = users.map(u =>
        '<tr><td>' + escHtml(u.username) + '</td><td><span class="role-badge role-mod">房管</span></td>'
        + '<td><button class="btn-sm" onclick="deleteUser(\\''+escAttr(u.username)+'\\')">删除</button></td></tr>'
      ).join('');
    } catch(e) { console.error(e); }
  }

  async function addUser() {
    const username = document.getElementById('newUsername').value.trim();
    const password = document.getElementById('newPassword').value.trim();
    if (!username || !password) { showToast('请填写用户名和密码'); return; }
    try {
      const res = await fetch('/api/users', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ username, password })
      });
      const d = await res.json();
      showToast(d.msg);
      if (d.ok) {
        document.getElementById('newUsername').value = '';
        document.getElementById('newPassword').value = '';
        fetchUsers();
      }
    } catch(e) { showToast('请求失败'); }
  }

  async function deleteUser(username) {
    if (!confirm('确定要删除房管 "' + username + '" 吗？')) return;
    try {
      const res = await fetch('/api/users/' + encodeURIComponent(username), { method: 'DELETE' });
      const d = await res.json();
      showToast(d.msg);
      fetchUsers();
    } catch(e) { showToast('请求失败'); }
  }

  // ── 通用 ──
  function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function escAttr(s) { return s.replace(/\\\\/g,'\\\\\\\\').replace(/'/g,"\\\\'"); }

  async function playEp(idx) {
    try {
      const res = await fetch('/api/play/' + idx, { method: 'POST' });
      const d = await res.json();
      showToast(d.msg);
      setTimeout(() => { fetchStatus(); fetchPlaylist(); }, 1500);
    } catch(e) { showToast('请求失败'); }
  }

  async function action(act) {
    try {
      const res = await fetch('/api/' + act, { method: 'POST' });
      const d = await res.json();
      showToast(d.msg || d.error || '操作完成');
      setTimeout(() => { fetchStatus(); fetchPlaylist(); }, 1000);
    } catch(e) { showToast('请求失败'); }
  }

  function doStop() {
    if (!confirm('确定要停止推流吗？')) return;
    action('stop');
  }

  async function doLogout() {
    await fetch('/api/logout', { method: 'POST' });
    location.href = '/login';
  }

  function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 2500);
  }

  // ── 画面设置 ──
  let overlayData = { logo: {}, overlay: [], clock: {} };

  async function loadOverlay() {
    try {
      const res = await fetch('/api/overlay');
      if (!res.ok) return;
      overlayData = await res.json();
      renderOverlayForm();
    } catch(e) { console.error(e); }
  }

  // 颜色预设列表
  const COLOR_PRESETS = [
    { label: '白', css: '#fff', val: 'white@0.8' },
    { label: '白半透', css: 'rgba(255,255,255,0.5)', val: 'white@0.5' },
    { label: '黄', css: '#facc15', val: 'yellow@0.8' },
    { label: '红', css: '#ef4444', val: 'red@0.8' },
    { label: '绿', css: '#22c55e', val: 'green@0.8' },
    { label: '蓝', css: '#3b82f6', val: 'blue@0.8' },
    { label: '橙', css: '#f97316', val: 'orange@0.8' },
    { label: '粉', css: '#ec4899', val: 'pink@0.8' },
    { label: '黑', css: '#111', val: 'black@0.8' },
  ];

  function initColorPresets() {
    document.querySelectorAll('.color-presets').forEach(wrap => {
      const targetId = wrap.dataset.target;
      const ovlIdx = wrap.dataset.ovlIdx;
      wrap.innerHTML = COLOR_PRESETS.map(c =>
        `<span class="color-dot" style="background:${c.css}" title="${c.label} (${c.val})"
              onclick="pickColor('${targetId}','${c.val}',${ovlIdx !== undefined ? ovlIdx : 'null'})"></span>`
      ).join('');
    });
  }

  function pickColor(targetId, val, ovlIdx) {
    const inp = document.getElementById(targetId);
    if (inp) inp.value = val;
    if (ovlIdx !== null) updateOvl(ovlIdx, 'fontcolor', val);
    refreshPreview();
  }

  function renderOverlayForm() {
    const logo = overlayData.logo || {};
    document.getElementById('logoPath').value = logo.path || '';
    document.getElementById('logoHeight').value = logo.height || 80;
    document.getElementById('logoX').value = logo.x ?? 20;
    document.getElementById('logoY').value = logo.y ?? 20;
    document.getElementById('logoOpacity').value = logo.opacity ?? 0.8;
    if (logo.path) document.getElementById('logoStatus').textContent = '当前: ' + logo.path;

    const clock = overlayData.clock || {};
    const clockEnabled = clock.enabled !== false;
    const sw = document.getElementById('clockSwitch');
    sw.classList.toggle('on', clockEnabled);
    document.getElementById('clockFontsize').value = clock.fontsize || 24;
    document.getElementById('clockFontcolor').value = clock.fontcolor || 'white@0.8';


    const imgList = overlayData.images || [];
    document.getElementById('imageList').innerHTML = imgList.map((item, i) => renderImageItem(item, i)).join('');

    const list = overlayData.overlay || [];
    const el = document.getElementById('overlayList');
    el.innerHTML = list.map((item, i) => renderOverlayItem(item, i)).join('');

    initColorPresets();
    refreshPreview();
  }

  function renderImageItem(item, idx) {
    return `<div class="overlay-item">
      <div class="overlay-item-header">
        <span class="overlay-item-title">图片 #${idx+1}</span>
        <button class="btn-icon" onclick="removeImageItem(${idx})" title="删除">✕</button>
      </div>
      <div class="form-grid">
        <div class="form-group-sm form-group-full">
          <label>文件路径 或 URL</label>
          <input type="text" value="${escAttr(item.path||'')}" onchange="updateImg(${idx},'path',this.value);refreshPreview()">
        </div>
        <div class="form-group-sm">
          <label>高度 (缩放)</label>
          <input type="number" value="${item.height||100}" onchange="updateImg(${idx},'height',+this.value);refreshPreview()">
        </div>
        <div class="form-group-sm">
          <label>透明度 (0-1)</label>
          <input type="number" value="${item.opacity??1}" step="0.1" max="1" min="0" onchange="updateImg(${idx},'opacity',+this.value);refreshPreview()">
        </div>
        <div class="form-group-sm">
          <label>X 坐标</label>
          <input type="number" value="${item.x??0}" onchange="updateImg(${idx},'x',+this.value);refreshPreview()">
        </div>
        <div class="form-group-sm">
          <label>Y 坐标</label>
          <input type="number" value="${item.y??0}" onchange="updateImg(${idx},'y',+this.value);refreshPreview()">
        </div>
      </div>
    </div>`;
  }

  function updateImg(idx, key, val) {
    if (!overlayData.images) overlayData.images = [];
    if (overlayData.images[idx]) overlayData.images[idx][key] = val;
  }

  function addUrlImageItem() {
    if (!overlayData.images) overlayData.images = [];
    overlayData.images.push({
      path: 'https://',
      height: 150,
      x: 20,
      y: 20,
      opacity: 1.0
    });
    renderOverlayForm();
    showToast('已添加网络图片叠加层，请填写 URL');
  }

  function removeImageItem(idx) {
    overlayData.images.splice(idx, 1);
    renderOverlayForm();
  }

  async function uploadAndAddImage(input) {
    if (!input.files || !input.files[0]) return;
    const file = input.files[0];
    const formData = new FormData();
    formData.append('file', file);
    try {
        showToast('正在上传...');
        const res = await fetch('/api/upload-image', {method:'POST', body:formData});
        const data = await res.json();
        if (data.ok) {
            if (!overlayData.images) overlayData.images = [];
            overlayData.images.push({
                path: data.path,
                height: 150,
                x: 20,
                y: 20,
                opacity: 1.0
            });
            renderOverlayForm();
            showToast('图片添加成功');
        } else {
            showToast(data.error || '上传失败');
        }
    } catch(e) {
        showToast('上传出错');
        console.error(e);
    }
    input.value = '';
  }

  function renderOverlayItem(item, idx) {
    return `<div class="overlay-item">
      <div class="overlay-item-header">
        <span class="overlay-item-title">文字 #${idx+1}</span>
        <button class="btn-icon" onclick="removeOverlayItem(${idx})" title="删除">✕</button>
      </div>
      <div class="form-grid">
        <div class="form-group-sm form-group-full">
          <label>文字内容</label>
          <input type="text" value="${escAttr(item.text||'')}" onchange="updateOvl(${idx},'text',this.value);refreshPreview()">
        </div>
        <div class="form-group-sm">
          <label>字号</label>
          <input type="number" value="${item.fontsize||24}" min="10" max="72" onchange="updateOvl(${idx},'fontsize',+this.value);refreshPreview()">
        </div>
        <div class="form-group-sm">
          <label>字体颜色</label>
          <input type="text" id="ovlColor${idx}" value="${escAttr(item.fontcolor||'white@0.7')}" onchange="updateOvl(${idx},'fontcolor',this.value);refreshPreview()">
          <div class="color-presets" data-target="ovlColor${idx}" data-ovl-idx="${idx}"></div>
        </div>
        <div class="form-group-sm">
          <label>水平位置 X</label>
          <input type="text" id="ovlX${idx}" value="${escAttr(String(item.x??30))}" onchange="updateOvl(${idx},'x',this.value);refreshPreview()">
        </div>
        <div class="form-group-sm">
          <label>垂直位置 Y</label>
          <input type="text" id="ovlY${idx}" value="${escAttr(String(item.y??30))}" onchange="updateOvl(${idx},'y',this.value);refreshPreview()">
        </div>
        <div class="form-group-sm">
          <label>描边宽度</label>
          <input type="number" value="${item.borderw||1}" min="0" max="10" onchange="updateOvl(${idx},'borderw',+this.value);refreshPreview()">
        </div>
        <div class="form-group-sm form-group-full">
          <label>快捷位置</label>
          <div class="pos-presets">
            <button class="pos-btn" onclick="setOvlPos(${idx},30,30)">↖ 左上</button>
            <button class="pos-btn" onclick="setOvlPos(${idx},'(w-text_w)/2',30)">↑ 上居中</button>
            <button class="pos-btn" onclick="setOvlPos(${idx},'w-text_w-30',30)">↗ 右上</button>
            <button class="pos-btn" onclick="setOvlPos(${idx},30,'h-th-30')">↙ 左下</button>
            <button class="pos-btn" onclick="setOvlPos(${idx},'(w-text_w)/2','h-th-30')">↓ 下居中</button>
            <button class="pos-btn" onclick="setOvlPos(${idx},'w-text_w-30','h-th-30')">↘ 右下</button>
            <button class="pos-btn" onclick="setOvlPos(${idx},'(w-text_w)/2','(h-th)/2')">❂ 居中</button>
          </div>
        </div>
      </div>
    </div>`;
  }

  function setOvlPos(idx, x, y) {
    updateOvl(idx, 'x', x);
    updateOvl(idx, 'y', y);
    document.getElementById('ovlX'+idx).value = x;
    document.getElementById('ovlY'+idx).value = y;
    refreshPreview();
  }

  function updateOvl(idx, key, val) {
    if (!overlayData.overlay) overlayData.overlay = [];
    if (overlayData.overlay[idx]) overlayData.overlay[idx][key] = val;
  }

  function addOverlayItem() {
    if (!overlayData.overlay) overlayData.overlay = [];
    overlayData.overlay.push({ text: '', fontsize: 24, fontcolor: 'white@0.7', x: 30, y: 'h-th-30', borderw: 1 });
    renderOverlayForm();
  }

  function removeOverlayItem(idx) {
    overlayData.overlay.splice(idx, 1);
    renderOverlayForm();
  }

  function toggleClock() {
    const sw = document.getElementById('clockSwitch');
    sw.classList.toggle('on');
    refreshPreview();
  }

  // ── 屏幕预览渲染 ──
  function parseFFColor(s) {
    if (!s) return 'rgba(255,255,255,0.8)';
    const m = s.match(/^(\w+)@([\d.]+)$/);
    if (m) return `rgba(${nameToRgb(m[1])},${m[2]})`;
    return s;
  }
  function nameToRgb(n) {
    const map = {white:'255,255,255',black:'0,0,0',red:'239,68,68',green:'34,197,94',blue:'59,130,246',yellow:'250,204,21',orange:'249,115,22',pink:'236,72,153',cyan:'6,182,212',purple:'168,85,247'};
    return map[n] || '255,255,255';
  }
  function evalPos(expr, vw, vh, textW, textH) {
    if (typeof expr === 'number') return expr;
    let s = String(expr);
    try {
      // 先替换长 token（占位符防冲突），再替换短 token
      s = s.replaceAll('text_w', String(textW));
      s = s.replaceAll('text_h', String(textH));
      s = s.replaceAll('th', String(textH));
      // w 和 h 必须最后替换，且仅匹配独立字符
      s = s.replace(/([^a-z_])w([^a-z_])/g, '$1' + vw + '$2');
      s = s.replace(/^w([^a-z_])/, vw + '$1');
      s = s.replace(/([^a-z_])w$/, '$1' + vw);
      s = s.replace(/^w$/, String(vw));
      s = s.replace(/([^a-z_])h([^a-z_])/g, '$1' + vh + '$2');
      s = s.replace(/^h([^a-z_])/, vh + '$1');
      s = s.replace(/([^a-z_])h$/, '$1' + vh);
      s = s.replace(/^h$/, String(vh));
      return Function('return ' + s)();
    } catch { return 30; }
  }

  function refreshPreview() {
    const c = document.getElementById('previewCanvas');
    if (!c) return;
    const ctx = c.getContext('2d');
    const W = c.width, H = c.height;
    // 背景
    ctx.clearRect(0,0,W,H);
    const grad = ctx.createLinearGradient(0,0,W,H);
    grad.addColorStop(0,'#1a1a2e'); grad.addColorStop(1,'#16213e');
    ctx.fillStyle = grad; ctx.fillRect(0,0,W,H);
    // 网格线
    ctx.strokeStyle = 'rgba(255,255,255,0.04)';
    for (let x=0;x<W;x+=60) { ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,H);ctx.stroke(); }
    for (let y=0;y<H;y+=60) { ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(W,y);ctx.stroke(); }
    // 中央提示
    ctx.fillStyle = 'rgba(255,255,255,0.08)'; ctx.font = '600 28px Inter,sans-serif';
    ctx.textAlign = 'center'; ctx.fillText('1920 × 1080 视频画面', W/2, H/2);
    ctx.textAlign = 'left';
    // 缩放系数
    const sx = W / 1920, sy = H / 1080;

    // Logo
    const logo = collectOverlayData().logo || {};
    if (logo.path) {
      const lh = (logo.height||80) * sy;
      const lw = lh * 2;
      const lx = (typeof logo.x==='number'? logo.x:20) * sx;
      const ly = (typeof logo.y==='number'? logo.y:20) * sy;
      ctx.globalAlpha = logo.opacity ?? 0.8;
      ctx.fillStyle = 'rgba(100,120,255,0.25)';
      ctx.strokeStyle = 'rgba(100,120,255,0.6)';
      ctx.lineWidth = 1.5;
      ctx.fillRect(lx, ly, lw, lh);
      ctx.strokeRect(lx, ly, lw, lh);
      ctx.fillStyle = 'rgba(255,255,255,0.7)';
      ctx.font = `${Math.max(10,lh*0.35)}px Inter,sans-serif`;
      ctx.textAlign = 'center';
      ctx.fillText('LOGO', lx+lw/2, ly+lh/2+4);
      ctx.textAlign = 'left';
      ctx.globalAlpha = 1;
    }

    // 多图片预览
    const imgs = overlayData.images || [];
    imgs.forEach((img, i) => {
      const idx = i + 1;
      const ih = (img.height||100) * sy;
      // 占位显示，假设 1:1 比例或 1.5 用于示意
      const iw = ih * 1.5; 
      const ix = (typeof img.x==='number'? img.x:20) * sx;
      const iy = (typeof img.y==='number'? img.y:20) * sy;
      
      ctx.save();
      ctx.globalAlpha = img.opacity ?? 1.0;
      ctx.fillStyle = 'rgba(255,100,255,0.25)';
      ctx.strokeStyle = 'rgba(255,100,255,0.6)';
      ctx.lineWidth = 1.5;
      ctx.fillRect(ix, iy, iw, ih);
      ctx.strokeRect(ix, iy, iw, ih);
      
      ctx.fillStyle = 'rgba(255,255,255,0.8)';
      ctx.font = `${Math.max(10,ih*0.3)}px Inter,sans-serif`;
      ctx.textAlign = 'center';
      ctx.fillText(`IMG #${idx}`, ix+iw/2, iy+ih/2+4);
      ctx.restore();
    });

    // 时钟
    const clock = collectOverlayData().clock || {};
    if (document.getElementById('clockSwitch').classList.contains('on')) {
      const cfs = (clock.fontsize||24) * sy;
      ctx.font = `600 ${cfs}px Inter,sans-serif`;
      ctx.fillStyle = parseFFColor(clock.fontcolor);
      const now = new Date();
      const ts = now.getHours().toString().padStart(2,'0')+':'+now.getMinutes().toString().padStart(2,'0')+':'+now.getSeconds().toString().padStart(2,'0');
      const tw = ctx.measureText(ts).width;
      ctx.fillText(ts, W - tw - 30*sx, 30*sy + cfs);
    }

    // 文字叠加 — 在 1920x1080 空间求值，再缩放到 Canvas
    const ovls = overlayData.overlay || [];
    ovls.forEach((item, i) => {
      if (!item.text) return;
      const origFs = item.fontsize || 24;
      const scaledFs = origFs * sy;
      ctx.font = `500 ${scaledFs}px Inter,sans-serif`;
      const canvasTw = ctx.measureText(item.text).width;
      // 推算 1920 空间下的文字宽高
      const fullTw = canvasTw / sx;
      const fullTh = origFs;
      // 在 1920x1080 空间求位置
      const fullX = evalPos(item.x, 1920, 1080, fullTw, fullTh);
      const fullY = evalPos(item.y, 1920, 1080, fullTw, fullTh);
      // 缩放到 canvas
      const rx = fullX * sx;
      const ry = fullY * sy;
      // 描边
      if (item.borderw) {
        ctx.strokeStyle = 'rgba(0,0,0,0.6)'; ctx.lineWidth = item.borderw * sy;
        ctx.strokeText(item.text, rx, ry);
      }
      ctx.fillStyle = parseFFColor(item.fontcolor);
      ctx.fillText(item.text, rx, ry);
      // 索引标记
      ctx.fillStyle = 'rgba(100,120,255,0.6)'; ctx.font = '10px Inter,sans-serif';
      ctx.fillText('#'+(i+1), rx-2, ry - scaledFs - 2);
    });
  }

  function collectOverlayData() {
    const logo = {
      path: document.getElementById('logoPath').value,
      height: +document.getElementById('logoHeight').value,
      x: isNaN(+document.getElementById('logoX').value) ? document.getElementById('logoX').value : +document.getElementById('logoX').value,
      y: isNaN(+document.getElementById('logoY').value) ? document.getElementById('logoY').value : +document.getElementById('logoY').value,
      opacity: +document.getElementById('logoOpacity').value
    };
    const clockEnabled = document.getElementById('clockSwitch').classList.contains('on');
    const clock = {
      enabled: clockEnabled,
      fontsize: +document.getElementById('clockFontsize').value,
      fontcolor: document.getElementById('clockFontcolor').value
    };
    return { logo, overlay: overlayData.overlay || [], clock, images: overlayData.images || [] };
  }

  async function saveOverlay() {
    const data = collectOverlayData();
    try {
      const res = await fetch('/api/overlay', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
      });
      const d = await res.json();
      showToast(d.msg || d.error || '保存完成');
    } catch(e) { showToast('保存失败'); }
  }

  async function uploadLogo(input) {
    if (!input.files.length) return;
    const fd = new FormData();
    fd.append('file', input.files[0]);
    document.getElementById('logoStatus').textContent = '上传中...';
    try {
      const res = await fetch('/api/upload-logo', { method: 'POST', body: fd });
      const d = await res.json();
      if (d.ok) {
        document.getElementById('logoPath').value = d.path;
        document.getElementById('logoStatus').textContent = '已上传: ' + d.path;
        showToast(d.msg);
      } else {
        document.getElementById('logoStatus').textContent = '上传失败';
        showToast(d.error || '上传失败');
      }
    } catch(e) {
      document.getElementById('logoStatus').textContent = '上传失败';
      showToast('上传失败');
    }
    input.value = '';
  }

  // ── 启动 ──
  init().then(() => {
    fetchStatus();
    fetchPlaylist();
    setInterval(fetchStatus, 3000);
    setInterval(fetchPlaylist, 10000);
  });
</script>
</body>
</html>
"""
