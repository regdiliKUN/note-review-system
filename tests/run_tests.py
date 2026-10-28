"""测试入口：Python 回归 + 前端语法/纯函数，一次跑完。

用法：
    python tests/run_tests.py            # 全部
    python tests/run_tests.py -q         # 只报失败
    python tests/run_tests.py TestRelayout  # 只跑某个 Python 测试类
    python tests/run_tests.py --py       # 跳过前端检查
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))

FRONTEND = os.path.join(ROOT, "app", "frontend")


def run_frontend() -> int:
    """node 可用时跑前端用例与语法检查；不可用则跳过（不算失败）。"""
    node = shutil.which("node")
    if not node:
        print("· 未安装 node，跳过前端检查")
        return 0
    bad = 0
    for f in ("lib.js", "app.js"):
        p = os.path.join(FRONTEND, f)
        if not os.path.isfile(p):
            continue
        r = subprocess.run([node, "--check", p], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"✗ 语法错误 {f}\n{r.stderr}")
            bad += 1
    script = os.path.join(HERE, "test_frontend.mjs")
    if os.path.isfile(script):
        r = subprocess.run([node, script], capture_output=True, text=True)
        out = (r.stdout or "") + (r.stderr or "")
        for line in out.strip().splitlines():
            if "前端用例" in line or line.strip().startswith("✗"):
                print(line)
        if r.returncode != 0:
            bad += 1
    return bad


def main() -> int:
    argv = sys.argv[1:]
    verbosity = 1 if ("-q" in argv or "--quiet" in argv) else 2
    skip_front = "--py" in argv
    pattern = None
    for a in argv:
        if not a.startswith("-"):
            pattern = a
            break

    loader = unittest.TestLoader()
    if pattern:
        suite = loader.loadTestsFromName(pattern, __import__("test_core"))
    else:
        suite = loader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)

    t0 = time.time()
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    total = result.testsRun
    bad = len(result.failures) + len(result.errors)

    front_bad = 0
    if not pattern and not skip_front:
        print("\n—— 前端检查 ——")
        front_bad = run_frontend()

    dt = time.time() - t0
    print("\n" + "=" * 62)
    print(f"Python 用例 {total}　通过 {total - bad}　失败 {bad}")
    if not pattern and not skip_front:
        print(f"前端检查 {'通过' if front_bad == 0 else str(front_bad) + ' 项失败'}")
    print(f"总耗时 {dt:.2f}s")
    print("=" * 62)
    return 0 if (bad == 0 and front_bad == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
