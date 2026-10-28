"""极简本地静态服务：给 WebView 提供前端页面与图片资源。

用本地 HTTP 而不是 file:// 或 base64，好处是浏览器能正常缓存图片、
大图不占内存、导出时路径也好处理。
"""
from __future__ import annotations

import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
    ".svg": "image/svg+xml", ".emf": "image/emf", ".wmf": "image/wmf",
    ".ico": "image/x-icon", ".json": "application/json; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    root = ""
    asset_root = ""
    protocol_version = "HTTP/1.1"
    log_path = ""

    def log_message(self, fmt, *args):
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")
        except Exception:
            pass

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/":
            path = "/index.html"
        if path.startswith("/assets/"):
            base = self.asset_root
            rel = path[len("/assets/"):]
            cacheable = True
        else:
            base = self.root
            rel = path.lstrip("/")
            cacheable = False
        rel = rel.replace("\\", "/")
        # 防目录穿越
        full = os.path.normpath(os.path.join(base, rel))
        if not os.path.normpath(full).startswith(os.path.normpath(base)):
            self.send_error(403)
            return
        if not os.path.isfile(full):
            self.send_error(404)
            return
        ext = os.path.splitext(full)[1].lower()
        try:
            data = open(full, "rb").read()
        except Exception:
            self.send_error(500)
            return
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        if cacheable:
            # 图片内容由内容哈希命名，永不变化 → 可以长缓存
            self.send_header("Cache-Control", "public, max-age=86400")
        else:
            # 页面与脚本必须每次校验：
            # 否则升级到新版本后，WebView2 仍会拿缓存里的旧 app.js，
            # 界面就会「新版后端 + 旧版前端」地错位，甚至整页白屏。
            self.send_header("Cache-Control", "no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass


def start(frontend_dir: str, asset_root: str) -> tuple[ThreadingHTTPServer, int]:
    Handler.root = os.path.abspath(frontend_dir)
    Handler.asset_root = os.path.abspath(asset_root)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port
