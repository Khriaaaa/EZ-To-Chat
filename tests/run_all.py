#!/usr/bin/env python3
"""跑齐 tests/ 下的全部自检。pytest 只收集 test_wxdb.py，其余是独立脚本。

用法：python3 tests/run_all.py   （任一项失败则退出码非 0）
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = ["test_error_summary_and_ssrf.py", "test_llm_linktest.py",
           "test_manual_context.py", "test_review_fixes.py", "test_wxdb.py"]

fail = 0
for name in SCRIPTS:
    p = HERE / name
    print("\n=== %s" % name, flush=True)
    r = subprocess.run([sys.executable, str(p)], cwd=str(HERE.parent))
    if r.returncode != 0:
        fail += 1
        print("!! %s 退出码 %d" % (name, r.returncode), flush=True)
print("\n全部通过" if not fail else "\n%d 个脚本失败" % fail)
sys.exit(1 if fail else 0)
