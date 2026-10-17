"""pytest 收集配置。

tests/ 下除 test_suite.py 之外都是脚本式自检（模块级直接跑检查、末尾 sys.exit，
或函数带自定义参数如 tmp/py），pytest 一收集就会 SystemExit 或报 fixture 缺失
（评审 B14）。这里全部排除；`python -m pytest` 只跑 test_suite.py，由它转去执行
tests/run_all.py，等于一次跑齐五份自检。
"""

collect_ignore = [
    "test_error_summary_and_ssrf.py",
    "test_llm_linktest.py",
    "test_manual_context.py",
    "test_review_fixes.py",
    "test_wxdb.py",
]
