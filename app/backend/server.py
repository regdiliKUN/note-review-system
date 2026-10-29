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
    # 大图不再一次性读进内存：超过这个尺寸就分块流式写出去
    STREAM_THRESHOLD = 1 * 1024 * 1024
    CHUNK = 64 * 1024

    def log_message(self, fmt, *args):
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")
        except Exception:
            pass

    def _common_headers(self):
        # 本地服务也要给基础安全头：万一哪天页面里混进了外部脚本，
        # 这些头能挡住内容嗅探与跨窗口引用。
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _etag(self, st) -> str:
        return f'"{int(st.st_mtime_ns):x}-{st.st_size:x}"'

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
            st = os.stat(full)
        except Exception:
            self.send_error(500)
            return
        etag = self._etag(st)

        # 条件请求：内容没变就回 304，省掉重复传输（大图尤其明显）
        inm = (self.headers.get("If-None-Match") or "").strip()
        if inm and inm == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            if cacheable:
                self.send_header("Cache-Control", "public, max-age=86400")
            else:
                self.send_header("Cache-Control", "no-cache, must-revalidate")
            self._common_headers()
            self.end_headers()
            return

        ctype = MIME.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        size = st.st_size

        # 大文件分块流式输出，避免一次性把几十 MB 读进内存
        if size > self.STREAM_THRESHOLD:
            try:
                f = open(full, "rb")
            except Exception:
                self.send_error(500)
                return
            with f:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("ETag", etag)
                self.send_header("Accept-Ranges", "bytes")
                self._common_headers()
                if cacheable:
                    self.send_header("Cache-Control", "public, max-age=86400")
                else:
                    self.send_header("Cache-Control", "no-cache, must-revalidate")
                    self.send_header("Pragma", "no-cache")
                self.end_headers()
                try:
                    while True:
                        chunk = f.read(self.CHUNK)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                except Exception:
                    pass
            return

        try:
            data = open(full, "rb").read()
        except Exception:
            self.send_error(500)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self._common_headers()
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
