#!/usr/bin/env python3
"""wx_collector.py - NAS-side driver + parser for the WeChat read-only collector.

Run with: python3 wx_collector.py <cmd> [options]

Commands:
  start    push wx_collect.ps1 to the guest (session 1) and start the endless loop
  stop     kill the collector PowerShell process in the guest
  poll     pull chat.json / sessions.json / shot.png / window.json, parse, append new
           messages to store/messages.jsonl
  once     run one round (Rounds=1) in the guest, then poll
  status   heartbeat time, log tail, stored message count

Sender detection: UIA cannot tell who sent a message, so each message row is cut out of
shot.png (by y/height) and left/right ink is compared (me = right/green, them = left/white).
Never sends anything, never presses keys.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

# 同 assistant.py：Windows 上被重定向的 stdout 按 GBK 编码，非 GBK 字符会崩
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
# WXREPLY_STORE 覆盖（跟 generate/server 同义）：桌面壳把数据放在用户目录下，
# 不往安装目录里写 —— 安装目录可能只读、重装还会被清掉
STORE_DIR = os.environ.get("WXREPLY_STORE") or os.path.join(os.path.dirname(HERE), "store")
MSG_FILE = os.path.join(STORE_DIR, "messages.jsonl")
CACHE_DIR = os.path.join(STORE_DIR, "cache")
# 读取路线开关（ui / db / auto，见 collector/readmode.py）：同目录直接导入，
# 面板/测试从别处导入时再加一次 collector 目录，两条导入路径都能用
sys.path.insert(0, HERE)
import readmode                                  # noqa: E402
from storelock import StoreLock                  # noqa: E402  跨进程 store 锁（面板同源）
try:
    import wxdb                                   # noqa: E402  本地库直读（只读）
except ImportError as _e:                         # 缺文件也不该把界面树路线带崩
    wxdb = None
    _WXDB_ERR = str(_e)
# 虚机连接与工作目录都来自 tools/vmcfg.py（环境变量或 config.local.yaml），
# 代码里不写死主机/口令/UUID。默认用项目自带的 tools/ga.py、tools/ps1run.py。
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
import vmcfg                                     # noqa: E402
SCRATCH = vmcfg.ensure_work()
GA = vmcfg.GA
PS1RUN = vmcfg.PS1RUN
# 在哪跑：Windows 上直跑（单机版，本地 PowerShell）还是容器里（NAS 侧，经 QGA 控虚机）
IN_VM = os.name == "nt"
LOCAL_PS1 = os.path.join(HERE, "wx_collect.ps1")
# 工作目录按运行位置分：
#   NAS 侧（容器里）-> guest 里的 C:\dl\wxc，老路径照旧，不动现状
#   Windows 上直跑（单机版）-> 项目自己的 store\collect，不去 C:\dl 占地，
#     也不会跟别处同时跑的另一份采集实例抢 heartbeat / round.json
if IN_VM:
    GUEST_DIR = os.path.join(STORE_DIR, "collect")
    GUEST_PS1 = os.path.join(GUEST_DIR, "wx_collect.ps1")
else:
    GUEST_DIR = "C:\\dl\\wxc"
    GUEST_PS1 = "C:\\dl\\wx_collect.ps1"


def _psq(s):
    """PS 单引号字符串转义：' 变 ''。GUEST_DIR/GUEST_PS1/winapp 路径都可能来自
    本地配置（WXREPLY_STORE 等），含引号时不转义就是把本地配置变成远端命令
    注入（审计 F-16）。"""
    return str(s).replace("'", "''")

# Screenshot geometry (window screenshot 896x648; UIA coords are screen pixels, so
# screenshot_xy = uia_xy - window origin). Default origin from measurement; overridden
# by window.json when the guest provides it.
# 注意：这里曾经写死过窗口原点 (72,64) 和聊天区左右边界 (372,948) —— 都是按
# 「默认窗口 896x648」量出来的静态值。用户把微信窗口拉宽、最大化、或拖到别处之后
# 这些数字就不再对，裁出来的条带会偏到气泡中段，把「我发的」判成「对方发的」，
# 还会经由 fixes 路径反向改写历史行。现在一律每轮从 UIA 元素现取：
#   窗口原点  -> window_origin()
#   气泡区边界 -> chat_bounds()
# 取不到就整轮不做像素判别（宁可不写，也不写错的）。
AVATAR_MARGIN = 68                   # avatars sit symmetrically at both edges; skip them
BG = (250, 250, 250)
DIFF_THRESH = 12                     # sum |dRGB| vs background
MIN_INK = 60                         # below this on both sides -> unknown
RATIO = 1.5                          # one side must exceed the other by this factor
SEL_GREEN = (21, 172, 112)           # selected-session highlight


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ 传输层
# 两种运行位置：容器里（靠 ga.py 经 QGA 控虚机）和 Windows 上（本地直跑，单机版）。
# Windows 模式下把 PowerShell 的输出包装成 ga.py 的 out-data/exitcode 形状，
# 这样上面那些 re.search("out-data:...") 的解析一行都不用改。
# 注意 IN_VM 在文件更上面就用到了（决定工作目录），所以定义提前，这里不再重复。


def run(args, timeout=180):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def _ps_local(script, timeout=180):
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return "out-data: %s\nexitcode: %d" % ((r.stdout or "").strip(), r.returncode)


if IN_VM:
    def ga_ps(script, timeout=180):
        return _ps_local(script, timeout)

    def ga_get(remote, local):
        """虚机内直接复制 —— 没有 QGA 句柄占用那回事，不用唯一文件名兜底。"""
        try:
            shutil.copyfile(remote, local)
        except OSError:
            return False
        return os.path.getsize(local) > 0
else:
    def ga(*args, timeout=180):
        return run([sys.executable, GA, *args], timeout)

    def ga_ps(script, timeout=180):
        return ga("ps", script, timeout=timeout)

    def ga_get(remote, local):
        """Pull a guest file; return True if non-empty local file produced."""
        tmp = local + ".part"
        if os.path.exists(tmp):
            os.remove(tmp)
        for _ in range(3):
            ga("get", remote, tmp)
            if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, local)
                return True
            time.sleep(2)
        return False


# ---------------------------------------------------------------- 单机版起法
def _winapp_path():
    """桌面壳自带 winapp.exe 时用它的；没带就让 ps1 用默认 C:\\winapp-cli\\winapp.exe。"""
    p = os.environ.get("WXREPLY_WINAPP") or ""
    return p if p and os.path.exists(p) else ""


def _sess_of(expr):
    out = ga_ps("try { %s } catch { '' }" % expr)
    m = re.search(r"out-data:\s*(\d+)", out)
    return int(m.group(1)) if m else None


def _ps_cmd_in_session(ps_args):
    """把采集循环拉起来的那条 PowerShell 命令。

    单机版：采集脚本、微信、我们自己都在同一个登录会话里 —— 直接 Start-Process。
    虚机调试版（本进程是 SYSTEM/会话 0，微信在 session 1）：借 PsExec 注入，
    否则窗口根本读不到。判不出来时：有 PsExec 就用，没有就直接起。
    """
    mine = _sess_of("(Get-Process -Id $PID).SessionId")
    wx = _sess_of("(Get-Process Weixin,WeChat -ErrorAction SilentlyContinue | "
                  "Select-Object -First 1).SessionId")
    psexec = os.environ.get("WXREPLY_PSEXEC", "C:\\dl\\PsExec64.exe")
    if wx is not None and mine is not None and wx == mine:
        print("collector: 与微信同会话（%d），直接起" % mine)
    elif not os.path.exists(psexec):
        print("collector: 没找到 PsExec，直接在本会话起（微信不在本会话就采不到）")
    else:
        print("collector: 本进程会话 %s / 微信会话 %s，借 PsExec 注入" % (mine, wx))
        return ("Start-Process '%s' -ArgumentList "
                "'-accepteula','-nobanner','-i','1','-d','powershell.exe',%s"
                % (_psq(psexec), ",".join(ps_args)))
    return ("Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @(%s)"
            % ",".join(ps_args))


# ---------------------------------------------------------------- guest control
def push_and_launch(rounds):
    """把采集脚本送进 session 1（交互会话）跑起来。

    容器模式：交给 ps1run.py —— 它负责补 UTF-8 BOM、再用 PsExec -i 1 注入会话。
    虚机模式：本地写完带 BOM 的 ps1，再 PsExec -i 1 拉进会话（少了 QGA 那一跳）。
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    src = open(LOCAL_PS1, "r", encoding="utf-8").read()
    gen = src.replace("[int]$Rounds = 0", "[int]$Rounds = %d" % rounds)
    if gen == src and rounds != 0:
        raise SystemExit("cannot patch Rounds default in wx_collect.ps1")

    if IN_VM:
        os.makedirs(GUEST_DIR, exist_ok=True)
        # utf-8-sig 写出 BOM：PS 5.1 见 BOM 才按 UTF-8 读，中文选择器才不会砸坏引号
        with open(GUEST_PS1, "w", encoding="utf-8-sig") as f:
            f.write(gen)
        # -OutDir 必须显式传：ps1 自己的默认还是 C:\dl\wxc（NAS 时代的路径），
        # 不传就会跟老实例共用一个 OutDir，互相抢 heartbeat / collector.lock
        ps_args = ["'-NoProfile'", "'-ExecutionPolicy'", "'Bypass'",
                   "'-WindowStyle'", "'Hidden'",                    # 别在桌面上弹黑窗
                   "'-File','%s'" % _psq(GUEST_PS1), "'-OutDir','%s'" % _psq(GUEST_DIR)]
        wa = _winapp_path()
        if wa:
            ps_args.append("'-Winapp','%s'" % _psq(wa))
        out = ga_ps(_ps_cmd_in_session(ps_args))
        print(out.strip())
        return "exitcode: 0" in out

    gen_path = os.path.join(CACHE_DIR, "wx_collect.ps1")
    # 带 UTF-8 BOM 写：PS 5.1 见 BOM 才按 UTF-8 读，中文选择器才不会砸坏引号
    with open(gen_path, "wb") as f:
        f.write(b"\xef\xbb\xbf" + gen.encode("utf-8"))
    # 走 ga.py put（分片经 guest-file-write 进虚机），不要用 ps1run：
    # ps1run 把整个文件 base64 塞进一条 QGA 命令行，脚本一超过 ~8KB 就被
    # "guest-exec: Failed to execute helper program" 拒掉——跟内容无关，只看长度。
    out = run([sys.executable, GA, "put", gen_path, GUEST_PS1], timeout=240)
    # ga.py put 的收尾阶段偶尔会因为 QGA 的响应不是 JSON 而吐 traceback，
    # 但文件其实已经写完了：所以按虚机上的实际大小校验，不信它的输出文本。
    size = re.search(r"out-data:\s*(\d+)", ga_ps("(Get-Item '%s').Length" % _psq(GUEST_PS1)))
    if not size or int(size.group(1)) != os.path.getsize(gen_path):
        print(out.strip()[-200:])
        return False
    ps = ("Start-Process 'C:\\dl\\PsExec64.exe' -ArgumentList "
          "'-accepteula','-nobanner','-i','1','-d','powershell.exe',"
          # -OutDir 必须显式传：ps1 自己的默认还是 C:\dl\wxc（NAS 时代的路径），
              # 不传就会跟老实例共用一个 OutDir，互相抢 heartbeat / collector.lock
              "'-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden',"  # 别在桌面上弹黑窗
              "'-File','%s','-OutDir','%s'"
              % (_psq(GUEST_PS1), _psq(GUEST_DIR)))
    out2 = ga_ps(ps)
    print(out2.strip()[-200:])
    return "exitcode: 0" in out2


def cmd_start(a):
    ga_ps("New-Item -ItemType Directory -Force -Path '%s' | Out-Null" % _psq(GUEST_DIR))
    if is_running():
        print("collector already running in guest")
        return 0
    ok = push_and_launch(0)
    print("start:", "launched" if ok else "FAILED")
    return 0 if ok else 1


def is_running():
    """虚机里有没有活的采集循环。

    判据是采集脚本自己维护的 collector.lock（文件里是它的 PID + 该 PID 是否还活着）。
    不用 Win32_Process.CommandLine：跨会话查命令行时经常拿不到（返回空），
    于是判定成「没在跑」，check 每 45 秒就再拉一份 —— 实测虚机里堆到 6 个实例，
    它们共用输出文件互相交错，是整个脏数据链的源头。
    """
    out = ga_ps("$d='%s'; $r='RUN=none'; if (Test-Path \"$d\\collector.lock\") { "
                "$p=(Get-Content \"$d\\collector.lock\" -Raw -ErrorAction SilentlyContinue); "
                "if ($p) { $p=$p.Trim(); if ($p -match '^\\d+$' -and (Get-Process -Id ([int]$p) -ErrorAction SilentlyContinue)) "
                "{ $r='RUN=' + $p } } }; $r" % _psq(GUEST_DIR))
    return bool(re.search(r"RUN=(\d+)", out))


def cmd_stop(a):
    out = ga_ps("Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
                "Where-Object { $_.CommandLine -like '*wx_collect.ps1*' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; 'killed ' + $_.ProcessId }")
    killed = re.findall(r"killed \d+", out)
    print("; ".join(killed) if killed else "no collector process found")
    return 0


def cmd_once(a):
    mode = readmode.resolve(STORE_DIR)
    if mode in ("db", "auto") and wxdb is not None:
        # 本地库直读不经过访客侧，没有"跑一轮"这回事：直接读一次就行
        return cmd_poll(a)
    ga_ps("New-Item -ItemType Directory -Force -Path '%s' | Out-Null" % _psq(GUEST_DIR))
    if is_running():
        # 常驻循环在跑时再拉一份，两份会共用 cmd.out.tmp / shot.name，把 dump 拼坏
        print("collector already running in guest; skip once")
        return 0
    ga_ps("Remove-Item '%s\\heartbeat.txt' -Force -ErrorAction SilentlyContinue" % _psq(GUEST_DIR))
    if not push_and_launch(1):
        print("launch failed")
        return 1
    deadline = time.time() + a.wait
    while time.time() < deadline:
        out = ga_ps("if (Test-Path '%s\\heartbeat.txt') { 'HB_OK' } else { 'HB_NO' }" % _psq(GUEST_DIR))
        if "HB_OK" in out:
            break
        time.sleep(4)
    else:
        print("round did not finish in %ds" % a.wait)
        return 2
    time.sleep(1)
    return cmd_poll(a)


# ---------------------------------------------------------------- parsing
def loads_tolerant(txt):
    """尽力解析一段 JSON 文本。

    访客侧 winapp 的输出偶尔会被拼在一起（两个采集实例共用 cmd.out.tmp 时的产物，
    实测拉到过「截图元数据 + 会话列表 + chat」三段拼一体的 chat.json），
    裸 json.load 会抛 JSONDecodeError 把整轮 poll 打断。这里退化成
    「扫描所有完整对象，取最后一个带 windows 的」，拿不到就返回 {}，
    调用方按「这一轮没数据」处理，绝不向上抛。
    """
    try:
        return json.loads(txt)
    except ValueError:
        pass
    dec = json.JSONDecoder()
    pos, best = 0, {}
    n = len(txt)
    while pos < n:
        while pos < n and txt[pos] in " \t\r\n":
            pos += 1
        if pos >= n:
            break
        try:
            obj, pos = dec.raw_decode(txt, pos)
            if isinstance(obj, dict) and "windows" in obj:
                best = obj
        except ValueError:
            pos += 1
    return best


def load_json(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return loads_tolerant(f.read())


def window_origin(win_json):
    """窗口左上角在 UIA 屏幕坐标里的位置。取不到返回 None。

    以前取不到就退回写死的默认原点：用户把窗口拖走之后，这个默认值会让整条
    裁切条带上下偏几十到几百像素（裁到相邻消息的拼接区），sender 判别近乎随机。
    返回 None 后调用方会放弃本轮的像素判别。
    """
    try:
        for w in win_json["windows"]:
            for e in w.get("elements", []):
                if e.get("type") == "Window":
                    return (int(e["x"]), int(e["y"]))
    except Exception:
        pass
    return None


def chat_bounds(chat):
    """聊天气泡区在 UIA 屏幕坐标里的 (左, 右)。取不到返回 None。

    数据来源就是 chat_message_list 元素自己（chat["windows"][0]["elements"][0]），
    它的 x/width 随窗口大小实时变化，所以窗口被拉宽也不会错位。
    """
    try:
        e = chat["windows"][0]["elements"][0]
        x, w = int(e["x"]), int(e["width"])
        if w > 0:
            return (x, x + w)
    except Exception:
        pass
    return None


def parse_chat(chat):
    """Return list of dicts: kind(msg|anchor), text, x,y,w,h, selector."""
    items = []
    try:
        children = chat["windows"][0]["elements"][0]["children"]
    except (KeyError, IndexError, TypeError):
        return items
    for c in children:
        cn = c.get("className")
        if cn == "mmui::ChatTextItemView":
            kind = "msg"
        elif cn == "mmui::ChatItemView":
            kind = "anchor"
        else:
            continue
        if c.get("isOffscreen"):
            continue          # 不在可视区，截图上量不到它的气泡，存下来只会是一条废行
        items.append({"kind": kind, "text": c.get("name") or "",
                      "x": c.get("x", 0), "y": c.get("y", 0),
                      "w": c.get("width", 0), "h": c.get("height", 0),
                      "selector": c.get("selector") or c.get("automationId") or ""})
    return items


def chat_title_from(title_obj):
    """从 title.json 的解析结果里取联系人真名（automationId = current_chat_name_label）。

    为什么不用会话列表的 isSelected / 绿色高亮：用搜索打开一个聊天时，
    会话列表的高亮经常不跟着走，于是采到的消息会被挂到上一个会话名下
    （实测：明明在楚金宇的窗口里，却标成 Hermes）。标题栏是 UIA 直读，不会错。
    """
    if not isinstance(title_obj, (dict, list)):
        return None

    def walk(o):
        if isinstance(o, dict):
            for k in ("automationId", "selector"):
                if str(o.get(k) or "").endswith("current_chat_name_label"):
                    n = (o.get("name") or "").strip()
                    if n and n != "None":
                        return n
            for v in o.values():
                r = walk(v)
                if r:
                    return r
        elif isinstance(o, list):
            for x in o:
                r = walk(x)
                if r:
                    return r
        return None

    return walk(title_obj)


def chat_title(title_p):
    if not title_p or not os.path.exists(title_p):
        return None
    return chat_title_from(load_json(title_p))


def selected_session(sessions, img, origin):
    """Name of the session that is selected (isSelected flag, else green highlight in screenshot)."""
    try:
        lst = sessions["windows"][0]["elements"][0]["children"]
    except (KeyError, IndexError, TypeError):
        return "unknown"

    def sname(c):
        sel = c.get("selector") or c.get("automationId") or ""
        if sel.startswith("session_item_"):
            return sel[len("session_item_"):]
        return (c.get("name") or "").split("\n")[0] or "unknown"

    for c in lst:
        if c.get("isSelected"):
            return sname(c)
    if img is not None:
        import numpy as np
        H, W = img.shape[:2]
        best, best_n = None, 0
        for c in lst:
            x0 = max(0, c["x"] - origin[0]); x1 = min(W, x0 + c["width"])
            y0 = max(0, c["y"] - origin[1]); y1 = min(H, y0 + c["height"])
            if x1 <= x0 or y1 <= y0:
                continue
            box = img[y0:y1, x0:x1].astype(int)
            n = int((np.abs(box - np.array(SEL_GREEN)).sum(2) < 12).sum())
            if n > best_n:
                best, best_n = c, n
        if best is not None and best_n >= 200:
            return sname(best)
    return "unknown"


def classify(img, item, origin, bounds):
    """Return (sender, left_px, right_px) from the screenshot strip of one message.

    bounds 是 chat_bounds() 现取的 (左, 右)，不再用写死的常量。
    """
    import numpy as np
    H, W = img.shape[:2]
    y0r = item["y"] - origin[1]
    y1r = y0r + item["h"]
    y0 = max(0, y0r)
    y1 = min(H, y1r)          # 用未截断的底边算，否则滚出顶部的消息条带会偏（评审 B15）
    xa = max(0, bounds[0] - origin[0] + AVATAR_MARGIN)
    xb = min(W, bounds[1] - origin[0] - AVATAR_MARGIN)
    if y1 - y0 < 4 or xb - xa < 30:
        return "unknown", 0, 0
    strip = img[y0:y1, xa:xb].astype(int)
    ink = np.abs(strip - np.array(BG)).sum(2) > DIFF_THRESH
    third = (xb - xa) // 3
    left = int(ink[:, :third].sum())
    right = int(ink[:, -third:].sum())
    if max(left, right) >= MIN_INK:
        if right > left * RATIO:
            return "me", left, right
        if left > right * RATIO:
            return "them", left, right
    # tie-break for long bubbles that span the middle: green bubble = mine
    g = (strip[:, :, 1] > 200) & (strip[:, :, 0] < 200) & (strip[:, :, 2] < 200) & \
        (strip[:, :, 1] - strip[:, :, 0] > 40)
    if int(g.sum()) > 300:
        return "me", left, right
    if max(left, right) >= MIN_INK:
        white = (strip.min(2) >= 253)
        if int(white.sum()) > 300:
            return "them", left, right
    return "unknown", left, right


def fingerprint(session, text, occ):
    """界面树路线的消息身份（occ = 本轮第几次出现）。

    故意不含 sender：同一条消息重看一遍时，发送方从 unknown 被改判，
    如果 sender 参与哈希就会算成新的一行，实测同一句话会重复入库两遍。

    注意：db 直读路线**不用**这个函数，它用 (会话, local_id) 做身份 ——
    occ 只在本轮内计数，跨轮同一句话会被误判成已存在（评审 B1）。两条路的
    身份口径不可互换。
    """
    raw = "\x1f".join([session, text, str(occ)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


_TIME_HINT_PATTERNS = (
    r"\d{1,2}:\d{2}",
    r"(昨天|前天|星期[一二三四五六日天])\s*\d{1,2}:\d{2}",
    r"(昨天|前天|星期[一二三四五六日天])",
    r"\d{4}年\d{1,2}月\d{1,2}日(\s*(上午|下午|凌晨|晚上)\s*\d{1,2}:\d{2})?",
    r"\d{1,2}月\d{1,2}日(\s*\d{1,2}:\d{2})?",
    r"(上午|下午|凌晨|晚上|中午)\s*\d{1,2}:\d{2}",
)


def _hhmm_like(s):
    """整串就是个时间提示吗：12:34 / 昨天 20:23 / 星期三 / 2026年10月12日 上午10:00

    必须**整串**匹配。早先写成关键词包含判断，正文「明天下午有空吗」里的「下午」
    就被当成时间提示吃掉了，预览错成时间 —— 宁可漏认时间，也不能吃正文。
    """
    s = (s or "").strip()
    if not s:
        return False
    return any(re.fullmatch(p, s) for p in _TIME_HINT_PATTERNS)


def parse_session_list(sessions):
    """会话列表 -> [{'session','preview','time_hint','selected'}]（纯只读）

    结构见 docs/notes.md：子项 selector/automationId 是 session_item_<联系人>，
    name 是「联系人\\n最后一条预览\\n时间」多行拼起来的。
    预览行不写死行号：跳过像时间的行、跳过与联系人同名的行，剩下最长的当预览。
    行数形状变了也不会崩，最坏是取不到预览（那就这一轮不算）。
    """
    out = []
    try:
        lst = sessions["windows"][0]["elements"][0]["children"]
    except (KeyError, IndexError, TypeError):
        return out
    for c in lst or []:
        if not isinstance(c, dict):
            continue
        sel = c.get("selector") or c.get("automationId") or ""
        name = c.get("name") or ""
        contact = sel[len("session_item_"):] if sel.startswith("session_item_") \
            else (name.split("\n")[0] or "")
        contact = contact.strip()
        if not contact:
            continue
        time_hint, cand = "", []
        for line in (l.strip() for l in name.split("\n")):
            if not line or line == contact:
                continue
            if not time_hint and _hhmm_like(line):
                time_hint = line
                continue
            cand.append(line)
        out.append({"session": contact,
                    "preview": max(cand, key=len) if cand else "",
                    "time_hint": time_hint,
                    "selected": bool(c.get("isSelected"))})
    return out


def session_list_rows(sessions, open_session, rows, seen, now=None):
    """会话列表旁路：把「没打开的那些会话」的最新一条也记进库。

    —— 只读界面树，不点击、不切换会话。这是「实时监测全部会话」里风险最低的做法：
    不读微信进程内存、不注入、不发包，跟现有采集走的完全是同一条路。
    保守三条：
      1) 预览为空、或就是当前打开的那个会话（已由聊天窗路径覆盖）-> 不算
      2) 这段话在该会话库里出现过（不管当时判成谁发的）-> 不算，
         免得把我刚发出去的那句当成对方新消息
      3) 指纹（含时间提示）见过 -> 不算
    行里带 src=session_list，方便区分来源。
    """
    now = now or utcnow()
    out = []
    for it in parse_session_list(sessions):
        s, pv = it["session"], it["preview"]
        if not pv or s == open_session or it["selected"]:
            continue
        # 会话列表里自己发的最后一条带「我: 」前缀。不带这个判据的话，未打开会话
        # 里我自己说的话会被当成对方新消息，watch 会生成一条自问自答的回复（评审 B20）。
        sender = "them"
        for prefix in ("我：", "我:"):
            if pv.startswith(prefix):
                pv = pv[len(prefix):].strip()
                sender = "me"
                break
        if not pv:
            continue
        if any(r.get("text") == pv for r in rows
               if isinstance(r, dict) and r.get("session") == s):
            continue
        # 指纹里加一个「当天」桶：会话列表的 time_hint 常为空或只到「昨天」，
        # 同一句预览会被永久去重，那些会话再也进不来（评审 B21）。跨天的重复
        # 能放进来，同一天内的重复仍按原逻辑挡住。
        fp = hashlib.sha1("\x1f".join(["slist", s, pv, it["time_hint"],
                                       time.strftime("%Y-%m-%d")]).encode("utf-8")).hexdigest()
        if fp in seen:
            continue
        seen[fp] = (len(rows) + len(out), sender)
        out.append({"ts_utc": now, "session": s, "sender": sender, "text": pv,
                    "align_left_px": 0, "align_right_px": 0,
                    "selector": "session_item_" + s, "time_hint": it["time_hint"],
                    "fp": fp, "src": "session_list"})
    return out


def append_rows(rows):
    """追加落盘（原子性靠单行 JSONL + 外部 _StoreLock，和原路径一致）。"""
    if not rows:
        return
    os.makedirs(STORE_DIR, exist_ok=True)
    with open(MSG_FILE, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def quote_senders(items, contact):
    """从引用气泡里推出确定的发送方，返回 {文本: sender}。

    一对一聊天里只有两个人：气泡引用了“对方”的话 -> 气泡作者是我；
    引用了“我”的话 -> 气泡作者是对方。这是 UIA 直读的文字，比按像素判断稳。
    顺带把被引用的原文也定下来（引号里那句话的作者是已知的）。

    只在作者能对上号时才认：作者必须是会话对方的昵称、或「我/自己」。
    否则（正文里恰好出现「引用…的消息」这种字样）直接不认，
    免得把一条正常消息的发送方硬改掉、还连带触发对历史行的改写。
    """
    m = {}
    self_names = ("我", "自己", "本人")
    for it in items:
        t = it.get("text") or ""
        if "引用" not in t or "的消息" not in t:
            continue
        lead, rest = t.split("引用", 1)
        if "的消息" not in rest:
            continue
        author, payload = rest.split("的消息", 1)
        author = author.strip().lstrip(":：")
        payload = payload.lstrip(" :：").strip()
        lead = lead.strip()
        if author == contact:
            by_contact = True
        elif author in self_names:
            by_contact = False
        else:
            continue
        if lead:
            m.setdefault(lead, "me" if by_contact else "them")
        if payload:
            m.setdefault(payload, "them" if by_contact else "me")
    return m


def load_store():
    """读整份消息库，返回 (行列表, fp -> (行号, 当时的 sender))。"""
    rows = []
    if os.path.exists(MSG_FILE):
        with open(MSG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    rows.append(None)
    idx = {}
    for i, r in enumerate(rows):
        if isinstance(r, dict) and r.get("fp"):
            idx[r["fp"]] = (i, r.get("sender"))
    return rows, idx


class _StoreLock(StoreLock):
    """跨进程互斥（文件锁）—— store 读改写段共用。

    面板的后台自检（wx_collector check）和 assistant.py up 起的 sync 是两个进程，
    都会走「读整库 -> 判新 -> 追加 / 原地改 sender」。没有这把锁时两边会各追加一份
    同样的行，或者 apply_sender_fixes 的全量重写把对方刚追加的行整个抹掉。
    锁只包住这一小段读改写（毫秒级），不做进程级长期持有。
    实现收口在 collector/storelock.py —— 面板的 /api/msg 追加拿的是同一把（审计 F-04），
    谁改锁文件路径/锁定范围，等于同时改两边的锁协议。
    """

    def __init__(self):
        super().__init__(STORE_DIR)


def _norm(s):
    return (s or "").strip()


def align_window(msg_items, tail_rows):
    """把这一轮的可视窗口和库里该会话的尾部对齐。

    为什么不用指纹判「这条是不是新的」：UIA 不提供每句话的稳定 id，能用的只有
    (会话, 文本, 第几次出现)。同一句话隔一阵再发、而前一次已经滚出视野时，
    两次的「第几次出现」都是 1 -> 老逻辑认成「已见过」，新那条被静默丢掉（实测丢过行）。
    改成序列对齐后：只要窗口尾部能和库尾的文本序列接上，接点之后的一律算新消息。

    对不齐（用户往上翻了、或窗口已经切到别的会话）时返回 None，调用方直接跳过本轮 ——
    宁可少存一轮，也绝不把历史消息当新消息重存一遍或把消息挂错会话。

    返回 (start, mapping)：msg_items[:start] 都已入库；mapping = {窗口下标: 库行号}，
    只覆盖能一对一接上的那一段（用于原地修正 sender）。
    """
    n = len(msg_items)
    if n == 0:
        return None, {}
    if not tail_rows:
        return 0, {}
    need = min(2, len(tail_rows))
    # 在窗口里找库尾那几行出现的位置：从每个位置往回比，取「连续对上的行数最多」的那个位置
    # （最多的那个才是真的接点：同一句话重复发时，靠前的那个位置能对上更多行）
    best_p, best_m = None, 0
    for p in range(n):
        m = 0
        while m < len(tail_rows) and m < 8 and p - m >= 0 and \
                _norm(msg_items[p - m]["text"]) == _norm(tail_rows[-1 - m][1].get("text")):
            m += 1
        if m > best_m or (m == best_m and m and p > best_p):
            best_p, best_m = p, m
    if best_p is None or best_m < need:
        return None, {}
    start = best_p + 1
    mapping = {}
    i, j = best_p, len(tail_rows) - 1
    while i >= 0 and j >= 0 and _norm(msg_items[i]["text"]) == _norm(tail_rows[j][1].get("text")):
        mapping[i] = tail_rows[j][0]
        i -= 1
        j -= 1
    return start, mapping


def _hhmm(s):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", s or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _window_last_hint(items):
    """窗口里最后一条消息头上挂的时间分隔条文本（items 要含分隔条）。"""
    anchor = ""
    for it in items:
        if it["kind"] == "anchor":
            anchor = it["text"]
        elif it.get("text") is not None:
            hint = anchor
    try:
        return hint
    except NameError:
        return ""


def _shares_run(msg_items, other_rows, run=3):
    """窗口里是否存在与另一个会话的历史连续重合 run 句以上的片段。

    用于「标题读错、把别人的聊天挂到当前会话名下」的兜底判定：
    整段连续三句文本完全一致，基本不可能是巧合。
    """
    if len(msg_items) < run or len(other_rows) < run:
        return False
    w = [_norm(it["text"]) for it in msg_items]
    o = [_norm(r.get("text")) for _, r in other_rows]
    for i in range(len(w) - run + 1):
        for j in range(len(o) - run + 1):
            if w[i:i + run] == o[j:j + run]:
                return True
    return False


def _newer_than_tail(items, msg_items, tail_rows):
    """这一屏最新的时间分隔条，是否严格晚于库里最后一条的。

    只用来兜「对不齐」的情况：对不齐可能是用户往上翻了（屏里的更旧 -> 不能写库），
    也可能是中间断采了一大段、库尾那条已经滚出视野（屏里全是新的 -> 得写）。
    """
    a = _hhmm(_window_last_hint(items))
    b = _hhmm(tail_rows[-1][1].get("time_hint") if tail_rows else "")
    return a is not None and b is not None and a > b


def apply_sender_fixes(fixes):
    """把指定行号的 sender 原地改掉，顺序和行数都不动。

    行号必须按 load_store() 的口径（跳过空行后计数）：直接用 readlines() 的下标
    在文件里出现空行（人工编辑、半行写入）时会整体错位，改到别的行上去。
    """
    if not fixes:
        return 0
    with open(MSG_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    phys = [i for i, ln in enumerate(lines) if ln.strip()]
    n = 0
    for k, sender in fixes.items():
        j = int(k)
        if not 0 <= j < len(phys):
            continue
        i = phys[j]
        try:
            r = json.loads(lines[i])
        except ValueError:
            continue
        if not isinstance(r, dict) or r.get("sender") == sender:
            continue
        r["sender"] = sender
        lines[i] = json.dumps(r, ensure_ascii=False) + "\n"
        n += 1
    if n:
        # 不能直接 open(MSG_FILE, "w")：那会先把文件截成 0 再逐行写，进程若在中途
        # 被杀（容器 OOM、NAS 重启），留下的只有前 N 行 —— 丢的是文件尾部的历史消息。
        # 改成「写临时文件 -> 原子替换」，替换前磁盘上一直是完整的老文件。
        tmp = MSG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, MSG_FILE)
    return n


def parse_and_store(chat, sessions, shot_p, window, title_obj=None):
    """把一轮快照（chat/sessions/window 三份 dump + 本轮那张截图）解析入库。

    入参都是已解析好的对象/路径，不再各自 load_json —— 一轮的所有数据必须来自同一份
    round.json，跨轮拼图是 sender 判错和会话串味的源头。
    """
    from PIL import Image
    import numpy as np
    origin = window_origin(window) if isinstance(window, dict) else None
    bounds = chat_bounds(chat if isinstance(chat, dict) else {})
    # 像素判别要三样同时成立：截图在、窗口原点在、气泡区边界在。少任何一样就
    # 整轮不做判别（img=None -> sender 全 unknown -> 由下面统一跳过，不写废行）。
    img = None
    if shot_p and os.path.exists(shot_p) and origin is not None and bounds is not None:
        img = np.array(Image.open(shot_p).convert("RGB"))
    sessions = sessions if isinstance(sessions, dict) else {}
    session = chat_title_from(title_obj) or selected_session(sessions, img, origin)
    items = parse_chat(chat if isinstance(chat, dict) else {})
    msg_items = [it for it in items if it["kind"] == "msg"]

    with _StoreLock():
        rows, seen = load_store()
        # 会话列表旁路先跑：即便本轮聊天窗 dump 不可用（掉了登录、窗口被盖住），
        # 「别的会话来了新消息」照样能被发现 —— 这条旁路存在的意义就在这。
        sess_rows = session_list_rows(sessions, session, rows, seen)
        append_rows(sess_rows)
        tail_rows = [(i, r) for i, r in enumerate(rows)
                     if isinstance(r, dict) and r.get("session") == session]
        if not tail_rows and any(isinstance(r, dict) for r in rows):
            # 这个会话一行都没有、库里却有别的会话：先排除「标题读错/串味」。
            # 只要这一屏能和别的会话的库尾接上，或者整段连续三句以上和别的会话的历史重合，
            # 就认定是挂错了会话，这一轮不写库。
            for other in {r.get("session") for r in rows if isinstance(r, dict)}:
                ot = [(i, r) for i, r in enumerate(rows)
                      if isinstance(r, dict) and r.get("session") == other]
                if align_window(msg_items, ot)[0] is not None or _shares_run(msg_items, ot):
                    return session, len(items), sess_rows
        start, mapping = align_window(msg_items, tail_rows)
        legacy = start is None
        if legacy:
            # 对不齐：用户往上翻了、或中间断采了一大段。退回到逐条按指纹判重。
            # 但只在能确认「这一屏是这条会话的延续」时才写，否则宁可不写：
            #   a) 屏里至少有一条早已在库里（连续性证据）；或
            #   b) 屏里最新的时间分隔条比库里最后一条更新（断采很久、整屏都是新消息）。
            cnt, cont = {}, False
            for it in msg_items:
                c = cnt.get(it["text"], 0) + 1
                cnt[it["text"]] = c
                if fingerprint(session, it["text"], c) in seen:
                    cont = True
                    break
            if not cont and not _newer_than_tail(items, msg_items, tail_rows):
                return session, len(items), sess_rows
            start, mapping = 0, {}

        qmap = quote_senders(items, session)
        anchor = ""
        snap = {}                                    # 本轮里同一句话出现的次数
        new_rows = []
        fixes = {}
        now = utcnow()
        mi = -1
        for it in items:
            if it["kind"] == "anchor":
                anchor = it["text"]
                continue
            mi += 1
            if img is not None:
                sender, lpx, rpx = classify(img, it, origin, bounds)
            else:
                sender, lpx, rpx = "unknown", 0, 0
            # 引用关系是 UIA 直读文字，比像素可靠：先算出来，再决定要不要丢这一行
            quoted = qmap.get(it["text"]) or qmap.get(it["text"].strip())
            if sender == "unknown" and not (lpx or rpx) and not quoted:
                continue                             # 这一轮截图没量到任何东西（窗口被盖/抓图失败）
            if quoted:
                sender = quoted                      # 压过像素判断
            occ = snap.get(it["text"], 0) + 1        # 本轮第几次出现，只用于生成指纹
            snap[it["text"]] = occ
            if mi < start:
                # 已在库里：这次把它判出来了、而库里还是 unknown/别的值，就原地改那一行
                j = mapping.get(mi)
                if j is not None:
                    old = rows[j].get("sender")
                    if old != sender and sender != "unknown":
                        fixes[str(j)] = sender
                continue
            fp = fingerprint(session, it["text"], occ)
            prev = seen.get(fp)
            if legacy and prev is not None:
                j, old = prev
                if old != sender and sender != "unknown":
                    fixes[str(j)] = sender          # 老行这条当时没判出来，这次判出来了就改
                continue
            if prev is not None:
                # 同一句话重复发时编号会撞车，给这条新行换一个唯一指纹，别和旧行并成一条
                fp = hashlib.sha1((fp + "#" + now + "#" + str(len(rows) + len(new_rows))).encode("utf-8")).hexdigest()
            seen[fp] = (len(rows) + len(new_rows), sender)
            new_rows.append({"ts_utc": now, "session": session, "sender": sender,
                             "text": it["text"], "align_left_px": lpx, "align_right_px": rpx,
                             "selector": it["selector"], "time_hint": anchor, "fp": fp})
        if fixes:
            apply_sender_fixes(fixes)
        if new_rows:
            append_rows(new_rows)
    return session, len(items), sess_rows + new_rows


def safe_shot_name(name):
    """round.json 里点名的那张截图，只接受纯文件名。"""
    name = (name or "").strip()
    if not name or name == "NONE":
        return ""
    if "/" in name or "\\" in name or ".." in name:
        return ""
    return name


def poll_db(mode, quiet):
    """本地库直读那一轮：返回 0/1 = 「这轮已处理完」，None = 「请回落界面树」。

    跟界面树那条路共用同一把 _StoreLock 和同一个 fingerprint 定义 —— 指纹只有
    一份（在 fingerprint() 里），两条路写出去的行形状必须一模一样，下游才不会串味。
    """
    def log(msg):
        if not quiet:
            print(msg, flush=True)

    if wxdb is None:
        readmode.record(STORE_DIR, mode=mode, via="ui",
                        note="本地库直读模块不可用：%s" % _WXDB_ERR)
        if mode == "db":
            log("db 路线不可用：本地库直读模块没装上（%s）" % _WXDB_ERR)
            return 1
        return None
    try:
        rows, info = wxdb.collect(STORE_DIR, log=log)
    except wxdb.Unavailable as e:
        note = "本地库直读不可用：%s" % e
        readmode.record(STORE_DIR, mode=mode, via="ui", note=note)
        if mode == "db":
            log("db 路线走不通：%s" % e)
            log("回界面树读取：存一份 store/collect.yaml 写 read_mode: ui（或切成 auto）")
            return 1
        log("%s，这轮回落界面树读取" % note)
        return None
    except Exception as e:                                  # noqa: BLE001
        # 锁超时（RuntimeError）、IO/sqlite 错误、缺库位：这一轮算失败，但必须
        # 回落界面树。以前只捕 Unavailable，别的异常会把整轮采集直接炸掉（评审 B19）。
        note = "本地库直读异常：%s: %s" % (type(e).__name__, e)
        readmode.record(STORE_DIR, mode=mode, via="ui", note=note)
        if mode == "db":
            log(note)
            return 1
        log("%s，这轮回落界面树读取" % note)
        return None
    blank = 0
    new = []
    with _StoreLock():
        old, seen = load_store()
        cnt = {}
        for m in rows:
            text = (m.get("text") or "").strip()
            if not text:
                blank += 1
                continue
            # db 路用 (会话, local_id) 做身份：local_id 是库里的稳定主键。以前和
            # 界面树共用「本轮第几次出现」的指纹，跨轮重复文本（「好」「嗯」）会
            # 被当成已入库静默丢掉，永久补不回来（评审 B1）。没有 local_id 的
            # 异常行才退回 occ 方案。
            lid = m.get("local_id")
            if lid:
                fp = hashlib.sha1(("db\x1f%s\x1f%s" % (m["session"], lid))
                                  .encode("utf-8")).hexdigest()
            else:
                k = (m["session"], text)
                occ = cnt.get(k, 0) + 1
                cnt[k] = occ
                fp = fingerprint(m["session"], text, occ)
            if fp in seen:
                continue
            seen[fp] = (len(old) + len(new), m["sender"])
            new.append({"ts_utc": m.get("ts_utc") or utcnow(), "session": m["session"],
                        "sender": m["sender"], "text": text,
                        "align_left_px": 0, "align_right_px": 0,
                        "selector": "db:%s" % (m.get("local_id") or ""),
                        "time_hint": "", "fp": fp, "src": "db",
                        "kind": m.get("kind") or "text"})
        if new:
            append_rows(new)
    detail = dict(info)
    if blank:
        detail["skipped_blank"] = blank
    readmode.record(STORE_DIR, mode=mode, via="db", detail=detail)
    if new:
        print("db %s  new=%d" % (info.get("account"), len(new)), flush=True)
    elif not quiet:
        print("db %s  无新消息（会话 %d，密钥 %d，跳过无正文 %d）"
              % (info.get("account"), info.get("sessions") or 0,
                 info.get("keys") or 0, blank))
    return 0


def cmd_poll(a):
    """拉一轮快照并入库。

    只拉 round.json（访客侧一轮原子写的完整快照）和它点名的那张截图 ——
    不再逐个拉 chat/sessions/window/title：访客每 2 秒重写，逐个拉要好几秒，
    拼出来的会是「chat 属于第 N 轮、shot 属于第 N+2 轮」，sender 就是这么判错的。
    """
    quiet = getattr(a, "quiet", False)
    mode = readmode.resolve(STORE_DIR)
    if mode in ("db", "auto"):
        rc = poll_db(mode, quiet)
        if rc is not None:
            return rc
    # 显式 ui 也记「本轮实际走了 ui」（复核 R3-2）：否则从 db 切回 ui 后 state 里的
    # via 停在 db，cmd_check 会永久误判 running=true、不再拉起访客采集。
    readmode.record(STORE_DIR, mode=mode, via="ui")
    os.makedirs(CACHE_DIR, exist_ok=True)
    # 本地暂存文件带上本进程 PID：面板自检和 assistant up 的 sync 是两个进程，
    # 会同时各拉一份；共用同一个落点会互相覆盖（ga.py 用的是 .part + replace，
    # 同一个 .part 名字两边抢，就会拉到半截文件）。入库那一段另有 _StoreLock 串行。
    tag = ".%d" % os.getpid()
    rp = os.path.join(CACHE_DIR, "round%s.json" % tag)
    # 面板自检每 45 秒起一个新进程（新 PID），按 PID 分名的暂存文件会堆：
    # 别的 PID 留下的、超过 3 分钟的直接清掉（同轮并发那几个进程自己会重拉）。
    for f in os.listdir(CACHE_DIR):
        p = os.path.join(CACHE_DIR, f)
        if (f.startswith(("round.", "shot.", "heartbeat.")) and not f.endswith(tag)
                and os.path.isfile(p) and time.time() - os.path.getmtime(p) > 180):
            try:
                os.remove(p)
            except OSError:
                pass
    if not ga_get(GUEST_DIR + "\\round.json", rp):
        if os.path.exists(rp):
            os.remove(rp)
        if not quiet:
            print("pull round.json    FAILED")
        return 1

    snap = load_json(rp)

    def _obj(v):
        """round.json 里各份 dump 是「JSON 文本」而不是嵌套对象 ——
        访客侧用 ConvertTo-Json 把 winapp 的原始输出原样当字符串塞进去的。"""
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            return loads_tolerant(v)
        return {}

    chat = _obj(snap.get("chat")) if isinstance(snap, dict) else {}
    if not isinstance(chat, dict) or "windows" not in chat:
        # 访客那一轮没采到 chat（命令行失败或输出被拼坏）：本轮不写库，下一轮再来
        if not quiet:
            print("round.json 里没有可用的 chat dump，跳过本轮")
        return 1

    # 心跳还是要拉：面板靠 store/cache/heartbeat.txt 的 mtime 判「采集在不在跑」。
    # 先拉到本进程私有名，再原子改名到那个固定名字（面板只认固定名）。
    hb_tmp = os.path.join(CACHE_DIR, "heartbeat%s.txt" % tag)
    ok_hb = ga_get(GUEST_DIR + "\\heartbeat.txt", hb_tmp)
    if ok_hb:
        try:
            os.replace(hb_tmp, os.path.join(CACHE_DIR, "heartbeat.txt"))
        except OSError:
            pass
    if not quiet:
        print("pull %-14s %s" % ("heartbeat.txt", "ok" if ok_hb else "FAILED"))

    shot_name = safe_shot_name(snap.get("shot"))
    local_shot = os.path.join(CACHE_DIR, "shot%s.png" % tag)
    ok_shot = bool(shot_name) and ga_get(GUEST_DIR + "\\" + shot_name, local_shot)
    if not quiet:
        print("pull round.json    ok  rid=%s" % snap.get("rid"))
        print("pull %-14s %s" % ("shot.png", "ok" if ok_shot else "FAILED"))
    if not ok_shot and os.path.exists(local_shot):
        os.remove(local_shot)

    try:
        session, n_items, rows = parse_and_store(chat, _obj(snap.get("sessions")),
                                                 local_shot if ok_shot else "",
                                                 _obj(snap.get("window")), _obj(snap.get("title")))
    except Exception as e:
        print("解析入库出错:", type(e).__name__, e, flush=True)
        return 1
    if not quiet:
        print("session=%s  items=%d  new=%d" % (session, n_items, len(rows)))
        for r in rows:
            print("  [%-7s L=%-5d R=%-5d] %s | %s" % (r["sender"], r["align_left_px"],
                  r["align_right_px"], r["time_hint"], r["text"][:40]))
    elif rows:
        print("sync %s  new=%d" % (session, len(rows)), flush=True)
    return 0


def cmd_sync(a):
    """循环 poll：虚机那个采集循环写盘，这里负责持续搬回本地。

    up 里必须有这一环 —— 只 start 采集不搬，本地 store 永远不更新。
    """
    while True:
        try:
            cmd_poll(a)
        except Exception as e:
            print("sync 出错:", type(e).__name__, e, flush=True)
        if a.once:
            return 0
        time.sleep(a.interval)


def cmd_check(a):
    """自检一轮：虚机采集没在跑就先拉起来，再搬运一次。

    给面板的后台自检线程用 —— 面板不该依赖外部先把采集起好。
    最后打一行 CHECK_JSON，调用方按它判断「采集在不在跑」。

    走本地库直读时不需要访客侧那个采集循环：面板每次自检就是一次读取，
    所以那条路下 running 直接算真，也不会去拉起访客脚本。
    """
    rc = cmd_poll(a)
    st = readmode.last_state(STORE_DIR)
    via = st.get("via") or ""
    if via == "db":
        print('CHECK_JSON={"running": true, "started": false, "rc": %d, '
              '"mode": "%s", "via": "db"}' % (rc, st.get("mode") or "db"), flush=True)
        return rc
    running = is_running()
    started = False
    if not running:
        started = push_and_launch(0)          # 0 = 常驻循环
        if not started:
            if not a.quiet:
                print("check: 拉起虚机采集失败")   # 静默也绝不进 else 白拉一轮（复核 R3-5）
        else:
            rc = cmd_poll(a)                  # 刚拉起来的采集，再搬一次
    print('CHECK_JSON={"running": %s, "started": %s, "rc": %d, "mode": "%s", '
          '"via": "%s"}' % ("true" if (running or started) else "false",
                            "true" if started else "false", rc,
                            st.get("mode") or readmode.resolve(STORE_DIR),
                            via or "ui"), flush=True)
    return rc


def cmd_status(a):
    out = ga_ps("$d='%s'; if (Test-Path \"$d\\heartbeat.txt\") { 'HB=' + (Get-Content \"$d\\heartbeat.txt\" -Raw).Trim() } "
                "else { 'HB=none' }; if (Test-Path \"$d\\collector.log\") { Get-Content \"$d\\collector.log\" -Tail %d } "
                "else { 'LOG=none' }" % (_psq(GUEST_DIR), a.tail))
    m = re.search(r"HB=(\S+)", out)
    hb = m.group(1) if m else "unknown"
    age = ""
    try:
        t = datetime.strptime(hb, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = " (%ds ago)" % (datetime.now(timezone.utc) - t).total_seconds()
    except ValueError:
        pass
    st = readmode.last_state(STORE_DIR)
    mode = readmode.resolve(STORE_DIR)
    print("读取路线:", mode, "|", readmode.DESC.get(mode, ""))
    if st:
        print("上一轮实际走:", st.get("via") or "?", "at", st.get("at") or "?",
              ("｜" + st["note"]) if st.get("note") else "")
        if st.get("detail"):
            print("  明细:", json.dumps(st["detail"], ensure_ascii=False))
    print("heartbeat:", hb + age)
    print("log tail:")
    body = out.split("out-data:", 1)[-1]
    for line in body.splitlines():
        if re.match(r"\d{4}-\d\d-\d\dT", line.strip()):
            print("  " + line.strip())
    n = 0
    if os.path.exists(MSG_FILE):
        with open(MSG_FILE, "r", encoding="utf-8") as f:
            n = sum(1 for _ in f)
    print("stored messages:", n)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start", help="start endless collector loop in guest")
    sub.add_parser("stop", help="kill collector PowerShell in guest")
    p = sub.add_parser("poll", help="pull files, parse, append to store/messages.jsonl")
    p.add_argument("--quiet", action="store_true", help="只在有新消息时输出")
    p = sub.add_parser("sync", help="loop poll: keep pulling guest results into the local store")
    p.add_argument("--interval", type=float, default=4.0, help="seconds between polls")
    p.add_argument("--quiet", action="store_true", help="只在有新消息时输出")
    p.add_argument("--once", action="store_true")
    p = sub.add_parser("once", help="run one guest round then poll")
    p.add_argument("--wait", type=int, default=120, help="seconds to wait for the round")
    p = sub.add_parser("check", help="自检一轮：确保虚机采集在跑 + 搬运一次（给面板后台自检用）")
    p.add_argument("--quiet", action="store_true", help="只在有新消息时输出")
    p = sub.add_parser("status", help="heartbeat, log tail, stored count")
    p.add_argument("--tail", type=int, default=5)
    p = sub.add_parser("mode", help="看/改读取路线：mode [ui|db|auto]")
    p.add_argument("value", nargs="?", help="不填=只看当前值")
    sub.add_parser("dbcheck", help="本地库直读自检：环境/账号/密钥/会话，逐项说清卡在哪")
    a = ap.parse_args()
    if a.cmd == "mode":
        if a.value:
            try:
                print("读取路线已设为", readmode.set_mode(STORE_DIR, a.value))
            except ValueError as e:
                print("参数不对：%s" % e)
                return 1
        cur = readmode.resolve(STORE_DIR)
        print("当前读取路线:", cur, "|", readmode.DESC.get(cur, ""))
        st = readmode.last_state(STORE_DIR)
        if st:
            print("上一轮实际走:", st.get("via") or "?", "at", st.get("at") or "?",
                  ("｜" + st["note"]) if st.get("note") else "")
        return 0
    if a.cmd == "dbcheck":
        if wxdb is None:
            print("本地库直读模块不可用：%s" % _WXDB_ERR)
            return 1
        os.environ.setdefault("WXREPLY_STORE", STORE_DIR)
        print(json.dumps(wxdb.dbcheck(), ensure_ascii=False, indent=2))
        return 0
    sys.exit({"start": cmd_start, "stop": cmd_stop, "poll": cmd_poll, "sync": cmd_sync,
              "once": cmd_once, "check": cmd_check, "status": cmd_status}[a.cmd](a))


if __name__ == "__main__":
    main()
