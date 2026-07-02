from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

AUTH_COOKIE_NAME = "autodub_auth"
_TOKEN_MAX_AGE = 60 * 60 * 24 * 7  # 7 days
_ALLOWLIST = {"/login", "/auth/login", "/auth/logout", "/health"}
_ALLOW_PREFIXES = ("/static/",)


def _password() -> str:
    return os.getenv("APP_PASSWORD", "").strip()


def _secret() -> bytes:
    return (os.getenv("APP_SESSION_SECRET", "").strip() or "dev-insecure-secret").encode("utf-8")


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_auth_token() -> str:
    """Signed token carrying its issue time: `<issued_at>.<hmac>`."""
    payload = str(int(time.time()))
    return f"{payload}.{_sign(payload)}"


def is_valid_token(token: str) -> bool:
    try:
        payload, sig = token.rsplit(".", 1)
        issued_at = int(payload)
    except (ValueError, AttributeError):
        return False
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    return (time.time() - issued_at) <= _TOKEN_MAX_AGE


def _is_allowed(path: str) -> bool:
    return path in _ALLOWLIST or any(path.startswith(p) for p in _ALLOW_PREFIXES)


async def auth_middleware(request: Request, call_next):
    if not _password():                       # auth disabled
        return await call_next(request)
    if _is_allowed(request.url.path):
        return await call_next(request)
    token = request.cookies.get(AUTH_COOKIE_NAME, "")
    if token and is_valid_token(token):
        return await call_next(request)
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return RedirectResponse("/login", status_code=302)
    return JSONResponse({"detail": "Authentication required"}, status_code=401)


def register_auth(app: FastAPI) -> None:
    app.middleware("http")(auth_middleware)

    @app.get("/login")
    def login_page() -> FileResponse:
        return FileResponse("./static/login.html")

    @app.post("/auth/login")
    def do_login(password: str = Form(...)) -> RedirectResponse:
        if _password() and secrets.compare_digest(password, _password()):
            resp = RedirectResponse("/studio", status_code=302)
            resp.set_cookie(
                AUTH_COOKIE_NAME, make_auth_token(),
                httponly=True, samesite="lax", max_age=_TOKEN_MAX_AGE,
            )
            return resp
        return RedirectResponse("/login?e=1", status_code=302)

    @app.post("/auth/logout")
    def do_logout() -> RedirectResponse:
        resp = RedirectResponse("/login", status_code=302)
        resp.delete_cookie(AUTH_COOKIE_NAME)
        return resp
