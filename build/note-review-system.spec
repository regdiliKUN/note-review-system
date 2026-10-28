# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

用 SPECPATH 推算项目根目录，不写死盘符 —— 旧版写的是 I:\\workbuddy\\...，
换一台机器打包就找不到文件。新增的 backend 模块也必须显式列进 hiddenimports，
否则打包后运行时才会报 ModuleNotFoundError。
"""
import os

try:
    ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
except NameError:                                    # 直接在项目根目录执行时
    ROOT = os.path.abspath(os.getcwd())

APP = os.path.join(ROOT, "app")
FRONTEND = os.path.join(APP, "frontend")
ICON = os.path.join(FRONTEND, "app.ico")

from PyInstaller.utils.hooks import collect_all  # noqa: E402

datas = [(FRONTEND, "frontend")]
binaries = []
hiddenimports = [
    "clr",
    "PIL.Image",
    "backend",
    "backend.api",
    "backend.docmodel",
    "backend.history",
    "backend.llm",
    "backend.logs",
    "backend.media",
    "backend.pipeline",
    "backend.relayout",
    "backend.report",
    "backend.review",
    "backend.server",
]
tmp_ret = collect_all("webview")
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

a = Analysis(
    [os.path.join(APP, "main.py")],
    pathex=[APP],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["playwright", "numpy", "pytest", "tkinter", "matplotlib"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="note-review-system",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON] if os.path.isfile(ICON) else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="note-review-system",
)
