"""统一日志：分级、轮转、崩溃可追溯。

为什么需要它：打包成 exe 后 `console=False`，`print()` 与 `traceback.print_exc()`
写进的是一个看不见的管道 —— 用户报「闪退」，开发者手上却什么都没有。
所有异常都必须落到磁盘上的 app.log，并且日志文件不能无限增长把 C 盘写满。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler

MAX_BYTES = 2 * 1024 * 1024      # 单文件 2MB
BACKUPS = 3                      # 保留 3 个历史文件

_LOGGER: logging.Logger | None = None
_HOOKED = False
_CURRENT_DIR: str | None = None


def setup(data_dir: str, level: int = logging.INFO) -> logging.Logger:
    """初始化日志。重复调用是安全的；换了目录会自动重定向到新文件。"""
    global _LOGGER, _CURRENT_DIR
    lg = logging.getLogger("notereview")
    target = os.path.abspath(data_dir)
    if _CURRENT_DIR != target:
        # 目录变了：摘掉旧的磁盘 handler，换成新目录的
        for h in list(lg.handlers):
            if isinstance(h, RotatingFileHandler):
                lg.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
        _CURRENT_DIR = target
    lg.setLevel(level)
    if not any(isinstance(h, RotatingFileHandler) for h in lg.handlers):
        try:
            os.makedirs(data_dir, exist_ok=True)
            h = RotatingFileHandler(os.path.join(data_dir, "app.log"),
                                    maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                    encoding="utf-8")
            h.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname).1s %(message)s", "%m-%d %H:%M:%S"))
            lg.addHandler(h)
        except Exception:
            if not lg.handlers:
                lg.addHandler(logging.NullHandler())
    lg.propagate = False
    _LOGGER = lg
    _install_hooks()
    return lg


def get() -> logging.Logger:
    return _LOGGER or logging.getLogger("notereview")


def _install_hooks() -> None:
    global _HOOKED
    if _HOOKED:
        return
    _HOOKED = True

    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        get().error("未捕获异常：\n" +
                    "".join(traceback.format_exception(exc_type, exc, tb)))

    sys.excepthook = excepthook

    def thread_hook(args):
        name = getattr(getattr(args, "thread", None), "name", "?")
        get().error(f"线程 [{name}] 未捕获异常：\n" +
                    "".join(traceback.format_exception(
                        args.exc_type, args.exc_value, args.exc_traceback)))

    threading.excepthook = thread_hook


def exception(msg: str) -> None:
    """记录当前异常（等价于 traceback.print_exc，但会落盘）。"""
    get().error(msg + "\n" + traceback.format_exc())


def log_path(data_dir: str) -> str:
    return os.path.join(data_dir, "app.log")


def tail(data_dir: str, lines: int = 120) -> str:
    """读日志尾部，供「帮助 → 查看日志」用。"""
    p = log_path(data_dir)
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except Exception as e:
        return f"（无法读取日志：{e}）"
