"""认证模块 — JWT Token + 用户管理"""

import hashlib
import json
import logging
import time
from pathlib import Path

import jwt

log = logging.getLogger(__name__)

USERS_FILE = Path("users.json")

# 运行时状态
_secret_key = "default-secret"
_admin_user = None  # {"username": ..., "password_hash": ..., "role": "admin"}


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def init_admin(config: dict):
    """从配置初始化超级管理员"""
    global _secret_key, _admin_user
    auth_cfg = config.get("auth", {})
    _secret_key = auth_cfg.get("secret_key", "default-secret")
    username = auth_cfg.get("admin_username", "admin")
    password = auth_cfg.get("admin_password", "admin123")
    _admin_user = {
        "username": username,
        "password_hash": hash_password(password),
        "role": "admin",
    }
    log.info("超级管理员已初始化: %s", username)


def load_users() -> list[dict]:
    """加载房管列表"""
    if not USERS_FILE.exists():
        return []
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def save_users(users: list[dict]):
    """保存房管列表"""
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def authenticate(username: str, password: str) -> dict | None:
    """验证登录，返回用户信息或 None"""
    pw_hash = hash_password(password)

    # 检查超级管理员
    if _admin_user and username == _admin_user["username"] and pw_hash == _admin_user["password_hash"]:
        return {"username": username, "role": "admin"}

    # 检查房管
    for user in load_users():
        if user["username"] == username and user["password_hash"] == pw_hash:
            return {"username": username, "role": "mod"}

    return None


def create_token(username: str, role: str) -> str:
    """生成 JWT Token"""
    payload = {
        "sub": username,
        "role": role,
        "exp": int(time.time()) + 86400 * 7,  # 7 天有效
    }
    return jwt.encode(payload, _secret_key, algorithm="HS256")


def verify_token(token: str) -> dict | None:
    """验证 Token，返回 payload 或 None"""
    try:
        payload = jwt.decode(token, _secret_key, algorithms=["HS256"])
        return {"username": payload["sub"], "role": payload["role"]}
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def add_user(username: str, password: str) -> tuple[bool, str]:
    """添加房管"""
    if _admin_user and username == _admin_user["username"]:
        return False, "不能与管理员同名"

    users = load_users()
    if any(u["username"] == username for u in users):
        return False, "用户名已存在"

    users.append({
        "username": username,
        "password_hash": hash_password(password),
        "role": "mod",
    })
    save_users(users)
    log.info("创建房管: %s", username)
    return True, "创建成功"


def delete_user(username: str) -> tuple[bool, str]:
    """删除房管"""
    users = load_users()
    new_users = [u for u in users if u["username"] != username]
    if len(new_users) == len(users):
        return False, "用户不存在"
    save_users(new_users)
    log.info("删除房管: %s", username)
    return True, "删除成功"


def list_users() -> list[dict]:
    """列出所有房管（隐藏密码）"""
    return [{"username": u["username"], "role": u["role"]} for u in load_users()]
