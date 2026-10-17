"""pytest 入口：仓库自检是脚本式的，这里统一跑一遍 run_all.py（评审 B14）。

直接 `python3 tests/run_all.py` 等价；名字带 test_ 是为了 `python -m pytest` 能一键跑。
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def test_all_self_checks():
    r = subprocess.run([sys.executable, str(HERE / "run_all.py")],
                       cwd=str(HERE.parent))
    assert r.returncode == 0, "有自检脚本失败，见上方输出"
