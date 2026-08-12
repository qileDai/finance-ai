"""独立管理后台 HTTP 服务（与 wework-external-bot 分进程）。"""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

from config.settings import PROJECT_ROOT, settings
from src.storage.db import ExternalGroupStore
from src.web import admin_auth
from src.web.admin_api import handle_admin_api

logger = logging.getLogger(__name__)

ADMIN_STATIC_ROOT = PROJECT_ROOT / "static" / "admin"

# 无需 Cookie 的 API
_PUBLIC_API = frozenset({"login", "logout", "me"})


@dataclass
class AdminWebServer:
    """仅托管 React SPA + /admin/api + 轻量 /health。"""

    store: ExternalGroupStore = field(default_factory=ExternalGroupStore)
    host: str = "0.0.0.0"
    port: int = 8082

    def start(self, *, blocking: bool = True) -> None:
        store = self.store
        port = self.port
        host = self.host

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                logger.debug("HTTP %s", fmt % args)

            def _send_html(self, html: str, code: int = 200) -> None:
                body = html.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_json(
                self,
                data: dict,
                code: int = 200,
                *,
                extra_headers: list[tuple[str, str]] | None = None,
            ) -> None:
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                for k, v in extra_headers or []:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _read_body(self, *, max_bytes: int = 1_000_000) -> bytes:
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length <= 0:
                    return b""
                return self.rfile.read(min(length, max_bytes))

            def _api_rel(self, path: str) -> str:
                parsed = urlparse(path)
                raw = (parsed.path or "").rstrip("/") or "/"
                if not raw.startswith("/admin/api"):
                    return ""
                return raw[len("/admin/api") :].lstrip("/")

            def _require_session(self) -> str | None:
                user = admin_auth.username_from_request(self.headers)
                if not user:
                    self._send_json({"ok": False, "error": "unauthorized"}, 401)
                    return None
                return user

            def _handle_health(self) -> None:
                self._send_json(
                    {
                        "ok": True,
                        "service": "finance-ai-admin",
                        "port": port,
                        "static_ready": (ADMIN_STATIC_ROOT / "index.html").is_file(),
                    }
                )

            def _handle_auth_api(self, method: str, rel: str) -> bool:
                """处理 login/logout/me。已处理返回 True。"""
                if rel == "me" and method == "GET":
                    user = admin_auth.username_from_request(self.headers)
                    if user:
                        self._send_json(
                            {"ok": True, "authenticated": True, "username": user}
                        )
                    else:
                        self._send_json(
                            {"ok": True, "authenticated": False, "username": ""}
                        )
                    return True
                if rel == "logout" and method == "POST":
                    self._send_json(
                        {"ok": True},
                        200,
                        extra_headers=[
                            ("Set-Cookie", admin_auth.clear_session_cookie_header())
                        ],
                    )
                    return True
                if rel == "login" and method == "POST":
                    body = admin_auth.read_json_body(self._read_body())
                    username = str(body.get("username") or "")
                    password = str(body.get("password") or "")
                    ok, err = admin_auth.verify_credentials(username, password)
                    if not ok:
                        self._send_json({"ok": False, "error": err}, 401)
                        return True
                    token = admin_auth.issue_session_token(username)
                    self._send_json(
                        {
                            "ok": True,
                            "username": admin_auth.expected_username(),
                        },
                        200,
                        extra_headers=[
                            ("Set-Cookie", admin_auth.session_cookie_header(token))
                        ],
                    )
                    return True
                return False

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path in ("/health", "/healthz"):
                    return self._handle_health()
                if path == "/" or path == "":
                    self.send_response(302)
                    self.send_header("Location", "/admin/")
                    self.end_headers()
                    return
                if path.startswith("/admin/api"):
                    rel = self._api_rel(self.path)
                    if rel in _PUBLIC_API:
                        if self._handle_auth_api("GET", rel):
                            return
                    else:
                        if not self._require_session():
                            return
                    result = handle_admin_api(
                        method="GET",
                        path=self.path,
                        store=store,
                        icris_worker=None,
                    )
                    if result is None:
                        return self._send_json({"ok": False, "error": "not found"}, 404)
                    data, code = result
                    return self._send_json(data, code)
                if path.startswith("/admin"):
                    # SPA 静态资源匿名可访问（登录页需加载）
                    return self._handle_admin_spa(path)
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                if path.startswith("/admin/api"):
                    rel = self._api_rel(self.path)
                    if rel in _PUBLIC_API:
                        if self._handle_auth_api("POST", rel):
                            return
                        return self._send_json({"ok": False, "error": "not found"}, 404)
                    if not self._require_session():
                        return
                    # 需 body 的 POST 端点：register-runner/submit 大 body(30MB)；wework/send 默认 1MB
                    body: dict | None = None
                    if rel == "register-runner/submit":
                        raw = self._read_body(max_bytes=30_000_000)
                    elif rel in ("wework/send",):
                        raw = self._read_body()
                    else:
                        raw = b""
                    if raw:
                        try:
                            body = json.loads(raw.decode("utf-8"))
                        except Exception:
                            return self._send_json(
                                {"ok": False, "error": "invalid json body"}, 400
                            )
                    result = handle_admin_api(
                        method="POST",
                        path=self.path,
                        store=store,
                        icris_worker=None,
                        body=body,
                    )
                    if result is None:
                        return self._send_json({"ok": False, "error": "not found"}, 404)
                    data, code = result
                    return self._send_json(data, code)
                self.send_response(404)
                self.end_headers()

            def _handle_admin_spa(self, path: str) -> None:
                root = ADMIN_STATIC_ROOT.resolve()
                if not root.is_dir():
                    return self._send_html(
                        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                        "<title>Admin not built</title></head><body>"
                        "<h1>管理后台前端未构建</h1>"
                        "<p>请执行：</p>"
                        "<pre>cd web/admin\nnpm install\nnpm run build</pre>"
                        "<p>产物应位于 <code>static/admin/</code></p>"
                        "</body></html>",
                        503,
                    )
                rel = path[len("/admin") :].lstrip("/")
                if not rel or rel.endswith("/"):
                    candidate = root / "index.html"
                else:
                    candidate = (root / unquote(rel)).resolve()
                    if not str(candidate).startswith(str(root)):
                        self.send_response(403)
                        self.end_headers()
                        return
                    if not candidate.is_file():
                        candidate = root / "index.html"
                if not candidate.is_file():
                    return self._send_html(
                        "<h1>Admin index missing</h1>"
                        "<p>Run <code>npm run build</code> in web/admin</p>",
                        503,
                    )
                data = candidate.read_bytes()
                ctype, _ = mimetypes.guess_type(str(candidate))
                if not ctype:
                    ctype = "application/octet-stream"
                if candidate.name.endswith(".html"):
                    ctype = "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                if candidate.suffix in (".js", ".css", ".woff2", ".woff"):
                    self.send_header("Cache-Control", "public, max-age=86400")
                else:
                    self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer((host, port), Handler)
        logger.info(
            "[Admin] 已启动 http://%s:%s/admin （登录页；需 ADMIN_PASSWORD；静态目录 %s）",
            host,
            port,
            ADMIN_STATIC_ROOT,
        )
        if blocking:
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                server.shutdown()
        else:
            threading.Thread(target=server.serve_forever, daemon=True).start()
