"""笔记审查总结系统 —— 程序入口。"""
from __future__ import annotations

import os
import sys

APP_NAME = "笔记审查总结系统"
VERSION = "1.1.2"


def code_dir() -> str:
    """源码目录（backend 包所在处）。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


def base_dir() -> str:
    """程序主目录：打包后 = exe 所在目录；源码运行时 = 其上一级（I:\\workbuddy）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    return parent if os.path.basename(here).lower() == "app" else here


def resource_dir() -> str:
    """前端资源：优先取 exe 同级的 frontend，其次取 PyInstaller 解包目录。"""
    b = base_dir()
    for cand in (os.path.join(b, "frontend"),
                 os.path.join(getattr(sys, "_MEIPASS", b), "frontend"),
                 os.path.join(b, "app", "frontend")):
        if os.path.isdir(cand):
            return cand
    return os.path.join(b, "frontend")


def work_dirs():
    root = base_dir()
    data = os.path.join(root, "data")
    assets = os.path.join(data, "assets")
    exports = os.path.join(root, "导出结果")
    for d in (data, assets, exports):
        os.makedirs(d, exist_ok=True)
    return root, data, assets, exports


def main():
    root, data, assets, exports = work_dirs()
    code = code_dir()

    for p in (code, root):
        if p not in sys.path:
            sys.path.insert(0, p)

    # 必须在 import webview 之前设置。
    # 部分 Windows 环境（带安全软件的机器、受限会话）里 WebView2 的
    # 渲染进程沙箱会导致所有网络导航静默失败 —— 表现为窗口一片空白。
    # 本程序只加载 127.0.0.1 上的自有页面，关闭该沙箱不引入外部风险。
    os.environ.setdefault(
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
        "--no-sandbox")

    import webview
    from backend import server
    from backend.api import Api

    httpd, port = server.start(resource_dir(), assets)
    holder = {"win": None}

    api = Api(workdir=data, get_window=lambda: holder["win"], asset_root=assets)

    win = webview.create_window(
        f"{APP_NAME} v{VERSION}",
        f"http://127.0.0.1:{port}/index.html",
        js_api=api,
        width=1500, height=940,
        min_size=(1080, 680),
        background_color="#0d1017",
        text_select=True,
    )
    holder["win"] = win

    # 事件回调必须容忍 pywebview 传入的参数，否则会在事件分发里抛异常
    win.events.shown += lambda *a: api._log("event: shown")
    win.events.loaded += lambda *a: api._log("event: loaded")

    try:
        webview.start(gui="edgechromium", debug=bool(os.environ.get("NR_DEBUG")),
                      private_mode=False,
                      storage_path=os.path.join(data, "webview"))
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()
