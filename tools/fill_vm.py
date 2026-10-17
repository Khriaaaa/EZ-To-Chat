#!/usr/bin/env python3
"""把一句回复填进 NAS 虚机里微信的输入框（只填，绝不发送）。

为什么不用模拟键盘：实测从计划任务（交互用户 + 管理员权限）里用 SendInput /
keybd_event 发的键盘事件，微信一律忽略；连从同一条路发的鼠标点击也不可靠。
只有 QMP 注进虚拟硬件的输入才真的落到微信上。所以走
「剪贴板在虚机内设置 + Ctrl+V 由 QMP 发」这条路。

链路：
  text -> C:\\dl\\fill.txt --(guest-agent put)--> 虚机
       -> 计划任务 hermesact（以交互用户跑 act.ps1）设置剪贴板并保活 25s，
          同时把微信窗口矩形写进 C:\\dl\\winrect.txt
       -> QMP 点输入框 -> QMP Ctrl+V

前提（一次性搭好，已搭）：
  * 虚机里存在计划任务 hermesact，action 指向 C:\\dl\\act.ps1，
    principal = <交互用户> / InteractiveToken / HighestAvailable
  * guest-agent 通道通（ga.py probe 的 raw guest-ping）

用法：
  fill_vm.py "要填的文字"          # 命令行自测
  from fill_vm import fill; fill("文字")
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vmcfg                                     # noqa: E402
SCRATCH = Path(vmcfg.ensure_work())
GA = Path(vmcfg.GA)
# QMP 注入（click / key）由外部脚本提供：它要能 `vmctl.py click X Y` 和
# `vmctl.py key ctrl v`。填入通道默认是关的，只有你确实要开才需要它。
VMCTL = Path(os.environ.get("WXREPLY_VMCTL") or (Path(vmcfg.TOOLS) / "vmctl.py"))
PY = sys.executable

TASK = "hermesact"
GUEST_FILL = r"C:\dl\fill.txt"
GUEST_RECT = r"C:\dl\winrect.txt"
INPUT_ABOVE_BOTTOM = 90      # 输入框中心距窗口底边的高度（1024x768 实测）
HOLD_SECONDS = 25            # act.ps1 保活剪贴板的时长，取够一次点击+粘贴


class FillError(RuntimeError):
    pass


def _run(args, timeout=90):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise FillError("超时: %s" % " ".join(map(str, args[:3])))
    if r.returncode != 0:
        raise FillError("失败(%s): %s" % (r.returncode, (r.stderr or r.stdout).strip()[:300]))
    return r.stdout


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 16), b""):
            h.update(blk)
    return h.hexdigest()


def _parse_rect(text):
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) == 4 and all(p.strip().lstrip("-").isdigit() for p in parts):
            return [int(p) for p in parts]
    raise FillError("读不到窗口矩形: %r" % text[:200])


def fill(text, log=print):
    """把 text 填进虚机微信输入框。返回耗时秒数。"""
    text = (text or "").strip()
    if not text:
        raise FillError("空文本")
    if len(text) > 2000:
        raise FillError("文本过长(%d)" % len(text))
    t0 = time.time()

    SCRATCH.mkdir(parents=True, exist_ok=True)
    local = SCRATCH / "fill.txt"
    local.write_text(text, encoding="utf-8")

    _run([PY, str(GA), "put", str(local), GUEST_FILL])
    # 校验投放结果（评审 B11）：剪贴板真正读的是虚机里的 fill.txt，不是本地这份。
    # 传坏了就重传一次，还不对宁可失败，也别把上一条旧文本粘进聊天框。
    check = SCRATCH / "fill.check.txt"
    want = _sha256(local)
    for attempt in (1, 2):
        try:
            _run([PY, str(GA), "get", GUEST_FILL, str(check)])
        except FillError:
            if attempt == 2:
                raise
            continue
        if _sha256(check) == want:
            break
        if attempt == 2:
            raise FillError("投放内容校验失败（虚机里的 fill.txt 与本地不一致）")
        _run([PY, str(GA), "put", str(local), GUEST_FILL])
    log("已投放文本 %d 字（校验一致）" % len(text))

    # 结束上一次还在保活的实例，保证剪贴板归属唯一
    _run([PY, str(GA), "exec", "cmd.exe", "/c",
          'del "%s" 2>nul & schtasks /end /tn %s >nul 2>&1 & schtasks /run /tn %s'
          % (GUEST_RECT, TASK, TASK)])
    log("已触发 %s" % TASK)

    rect_local = SCRATCH / "winrect.txt"
    rect = None
    for _ in range(20):                      # 最多等 10 秒
        time.sleep(0.5)
        try:
            _run([PY, str(GA), "get", GUEST_RECT, str(rect_local)])
            rect = _parse_rect(rect_local.read_text(encoding="utf-8", errors="replace"))
            break
        except FillError:
            continue
    if not rect:
        raise FillError("等不到微信窗口矩形，虚机里微信可能没开")
    l, t, r, b = rect
    if r - l < 400 or b - t < 300 or t < -100:
        raise FillError("微信窗口矩形不合理 %s，可能最小化了" % rect)
    # 输入框贴窗口底边。90px 是 1024x768 实测值；窗口更矮（或换了 DPI/分辨率）时
    # 按比例收，且必须落在窗口下半部 —— 否则一点就点进聊天记录或工具栏（评审 B11）。
    h = b - t
    off = INPUT_ABOVE_BOTTOM if h >= 600 else max(40, int(h * 0.14))
    cx, cy = (l + r) // 2, b - off
    if not (t + h * 0.5 < cy < b - 8):
        raise FillError("算出的输入框位置 %d 不在窗口下半部 %s" % (cy, rect))
    log("微信窗口 %d,%d,%d,%d → 点输入框 %d,%d" % (l, t, r, b, cx, cy))

    _run([PY, str(VMCTL), "click", str(cx), str(cy)])
    time.sleep(0.8)
    _run([PY, str(VMCTL), "key", "ctrl", "v"])
    time.sleep(0.4)
    dt = time.time() - t0
    log("已填入（未发送）耗时 %.1fs" % dt)
    return dt


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    try:
        fill(sys.argv[1])
    except FillError as e:
        print("填入失败: %s" % e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
