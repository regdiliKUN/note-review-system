"""测试入口：自动发现 tests/ 下所有用例，输出汇总。

用法：
    python tests/run_tests.py            # 全部
    python tests/run_tests.py -q         # 只报失败
    python tests/run_tests.py TestRelayout  # 只跑某个类
"""
from __future__ import annotations

import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))


def main() -> int:
    argv = sys.argv[1:]
    verbosity = 1 if ("-q" in argv or "--quiet" in argv) else 2
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
    dt = time.time() - t0
    total = result.testsRun
    bad = len(result.failures) + len(result.errors)
    print("\n" + "=" * 62)
    print(f"用例 {total}　通过 {total - bad}　失败 {bad}　耗时 {dt:.2f}s")
    print("=" * 62)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
