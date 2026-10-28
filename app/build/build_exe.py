"""打包脚本：PyInstaller onedir -> build/dist/笔记审查总结系统/"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _python() -> str:
    """优先用装了 Pillow 的打包环境（视觉能力需要 PIL 缩放图片）。"""
    for name in ("build-env2", ".venv"):
        p = os.path.join(ROOT, name, "Scripts", "python.exe")
        if os.path.isfile(p):
            try:
                r = subprocess.run([p, "-c", "import PIL, docx, webview"],
                                   capture_output=True)
                if r.returncode == 0:
                    return p
            except Exception:
                pass
    return sys.executable


PY = _python()
NAME = "笔记审查总结系统"
print("python:", PY, flush=True)

cmd = [
    PY, "-m", "PyInstaller",
    "--noconfirm", "--clean",
    "--windowed",
    "--name", NAME,
    "--icon", os.path.join(ROOT, "app", "frontend", "app.ico"),
    "--add-data", os.path.join(ROOT, "app", "frontend") + ";frontend",
    "--paths", os.path.join(ROOT, "app"),
    "--collect-all", "webview",
    "--hidden-import", "clr",
    "--hidden-import", "PIL.Image",
    "--hidden-import", "backend",
    "--hidden-import", "backend.api",
    "--hidden-import", "backend.docmodel",
    "--hidden-import", "backend.history",
    "--hidden-import", "backend.llm",
    "--hidden-import", "backend.media",
    "--hidden-import", "backend.pipeline",
    "--hidden-import", "backend.relayout",
    "--hidden-import", "backend.review",
    "--hidden-import", "backend.server",
    "--exclude-module", "playwright",
    "--exclude-module", "numpy",
    "--exclude-module", "pytest",
    "--distpath", os.path.join(ROOT, "build", "dist"),
    "--workpath", os.path.join(ROOT, "build", "pywork"),
    "--specpath", os.path.join(ROOT, "build"),
    os.path.join(ROOT, "app", "main.py"),
]

print("building...", flush=True)
r = subprocess.run(cmd, cwd=ROOT)
print("exit", r.returncode, flush=True)

out = os.path.join(ROOT, "build", "dist", NAME)
if os.path.isdir(out):
    total = 0
    for dp, _, fs in os.walk(out):
        for f in fs:
            total += os.path.getsize(os.path.join(dp, f))
    print(f"输出目录: {out}")
    print(f"体积: {total / 1024 / 1024:.1f} MB")
sys.exit(r.returncode)
