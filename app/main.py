"""笔记审查总结系统 —— 程序入口。"""
from __future__ import annotations

import os
import sys

APP_NAME = "笔记审查总结系统"
VERSION = "1.2.0"


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


def app_icon_path() -> str:
    """程序图标（.ico）：窗口标题栏、任务栏都指向它。

    注意：以源码方式运行时进程是 python.exe，若不显式指定，
    Windows 会拿解释器的图标顶上，任务栏里就成了 Python 的图标。
    """
    b = base_dir()
    for cand in (os.path.join(resource_dir(), "app.ico"),
                 os.path.join(getattr(sys, "_MEIPASS", b), "frontend", "app.ico"),
                 os.path.join(b, "app", "frontend", "app.ico"),
                 os.path.join(b, "frontend", "app.ico")):
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    return ""


def set_app_user_model_id():
    """让任务栏按「本程序」而不是 python.exe 归组，图标才不会被解释器覆盖。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "WorkBuddy.NoteReview.1")
    except Exception:
        pass


def apply_window_icon(title: str, icon: str):
    """把图标按到窗口上（标题栏 + 任务栏按钮）。

    只做跨线程安全的动作：按标题找到窗口句柄，用 PostMessage 通知 WM_SETICON。
    绝不能从后台线程给 WinForms 的 Form.Icon 赋值 —— 那会把窗口直接锁死
    （表现为启动后界面不出现、boot() 不触发）。常规路径由 webview.start(icon=...)
    在 UI 线程内设置，这里只是兜底。
    """
    if not title or not icon or os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, title)
        if not hwnd:
            return
        IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
        hicon = u.LoadImageW(None, icon, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if not hicon:
            return
        for which in (1, 0):                      # ICON_BIG / ICON_SMALL
            u.PostMessageW(wintypes.HWND(hwnd), 0x0080, which, hicon)   # WM_SETICON
    except Exception:
        pass


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

    set_app_user_model_id()          # 必须在建窗口前，任务栏图标/归组才用本程序的
    icon = app_icon_path()
    title = f"{APP_NAME} v{VERSION}"

    httpd, port = server.start(resource_dir(), assets)
    holder = {"win": None}

    api = Api(workdir=data, get_window=lambda: holder["win"], asset_root=assets)

    win = webview.create_window(
        title,
        f"http://127.0.0.1:{port}/index.html",
        js_api=api,
        width=1500, height=940,
        min_size=(1080, 680),
        background_color="#0d1017",
        text_select=True,
    )
    holder["win"] = win

    # 事件回调必须容忍 pywebview 传入的参数，否则会在事件分发里抛异常
    win.events.shown += lambda *a: (api._log("event: shown"), apply_window_icon(title, icon))
    win.events.loaded += lambda *a: api._log("event: loaded")

    try:
        webview.start(gui="edgechromium", debug=bool(os.environ.get("NR_DEBUG")),
                      private_mode=False,
                      icon=icon or None,
                      storage_path=os.path.join(data, "webview"))
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()
