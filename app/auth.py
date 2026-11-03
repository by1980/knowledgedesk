"""Passwords, login tokens and permission checks."""
import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .db import conn

_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, digest_b64 = stored.split("$")
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return hmac.compare_digest(digest, expected)


def make_token(user_id: int) -> str:
    payload = {"sub": str(user_id), "exp": datetime.now(timezone.utc) + timedelta(hours=settings.token_hours)}
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def load_user(user_id: int) -> dict | None:
    with conn() as c:
        user = c.execute(
            "SELECT id, email, name, role, active FROM users WHERE id=%s", (user_id,)
        ).fetchone()
        if not user:
            return None
        user["departments"] = c.execute(
            "SELECT d.id, d.name FROM departments d JOIN user_departments ud ON ud.department_id=d.id "
            "WHERE ud.user_id=%s ORDER BY d.name",
            (user_id,),
        ).fetchall()
    return user


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    if creds is None:
        raise HTTPException(401, "Not logged in")
    try:
        payload = jwt.decode(creds.credentials, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Session expired, please log in again")
    user = load_user(int(payload["sub"]))
    if not user or not user["active"]:
        raise HTTPException(401, "Account disabled")
    return user


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admins only")
    return user


def ensure_first_admin() -> None:
    with conn() as c:
        if c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return
        c.execute(
            "INSERT INTO users(email, name, password_hash, role) VALUES (%s, %s, %s, 'admin')",
            (settings.admin_email.lower(), settings.admin_name, hash_password(settings.admin_password)),
        )
