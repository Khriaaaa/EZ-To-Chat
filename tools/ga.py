#!/usr/bin/env python3
"""通过 qemu-guest-agent 在 Windows guest 里执行命令 / 读写文件。

前提: guest 内 qemu-ga 服务在跑（vioserial 驱动已装 -> 通道连通, guest-ping 通），
      宿主机（NAS）能 ssh 上去并 sudo virsh。连接参数见 tools/vmcfg.py。
用法:
  ./ga.py raw '{"execute":"guest-ping"}'
  ./ga.py exec cmd.exe /c "dir C:\\Windows\\System32\\sethc*.exe"
  ./ga.py put <本地文件> <guest路径>      # 把脚本送进 guest，绕开引号地狱
  ./ga.py get <guest路径> <本地文件>
  ./ga.py ps 'powershell 命令'            # 用 -EncodedCommand 跑，无需转义
"""
import base64
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import vmcfg                                     # noqa: E402


def agent(payload):
    if not vmcfg.QGA_UUID:
        sys.exit("没配虚机 UUID：设 WXREPLY_QGA_UUID，或写 config.local.yaml 的 vm.qga_uuid")
    if not re.match(r"^[0-9a-fA-F-]{36}$", vmcfg.QGA_UUID):
        # UUID 直接拼进远端 shell 命令：形状不对（含 ; $() 之类）就是在注入。
        # 先卡死格式再拼，别指望 shell 引用（审计 F-16）。
        sys.exit("QGA UUID 格式不对（应形如 xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx）：%r"
                 % (vmcfg.QGA_UUID,))
    # 共用一个互斥锁：多个 virsh qemu-agent-command 并发时响应会串台
    # （A 的命令收到 B 的结果），表现就是随机「guest agent 无响应」或拿到别人的输出
    os.makedirs(os.path.dirname(vmcfg.QGA_LOCK) or ".", exist_ok=True)
    with open(vmcfg.QGA_LOCK, "w") as _lk:
        fcntl.flock(_lk, fcntl.LOCK_EX)
        try:
            js = json.dumps(payload)
            cmd = "sudo virsh qemu-agent-command %s %s" % (vmcfg.QGA_UUID, shlex.quote(js))
            r = subprocess.run(vmcfg.ssh_cmd() + [cmd], capture_output=True, text=True)
            return (r.stdout.strip() or r.stderr.strip())
        finally:
            fcntl.flock(_lk, fcntl.LOCK_UN)


def agent_json(payload):
    raw = agent(payload)
    try:
        return json.loads(raw)
    except Exception:
        # guest agent 出错时回的是纯文本（比如 "No file found"），
        # 直接 json.loads 会丢成一个看不懂的 JSONDecodeError
        raise SystemExit("guest agent 返回的不是 JSON：%s" % raw[:300])


def write_file_guest(guest_path, data: bytes):
    handle = agent_json({"execute": "guest-file-open",
                         "arguments": {"path": guest_path, "mode": "wb"}})["return"]
    try:
        for i in range(0, len(data), 3000):
            chunk = base64.b64encode(data[i:i + 3000]).decode()
            agent({"execute": "guest-file-write",
                   "arguments": {"handle": handle, "buf-b64": chunk}})
    finally:
        agent({"execute": "guest-file-close", "arguments": {"handle": handle}})


def read_file_guest(guest_path) -> bytes:
    out = b""
    handle = agent_json({"execute": "guest-file-open",
                         "arguments": {"path": guest_path, "mode": "rb"}})["return"]
    try:
        while True:
            r = agent_json({"execute": "guest-file-read",
                            "arguments": {"handle": handle, "count": 65536}})["return"]
            if r.get("buf-b64"):
                out += base64.b64decode(r["buf-b64"])
            if r.get("eof"):
                break
    finally:
        agent({"execute": "guest-file-close", "arguments": {"handle": handle}})
    return out


def run_and_print(path, args):
    started = agent({"execute": "guest-exec",
                     "arguments": {"path": path, "arg": args, "capture-output": True}})
    try:
        pid = json.loads(started)["return"]["pid"]
    except Exception:
        sys.exit("启动失败: %s" % started)
    for _ in range(60):
        time.sleep(0.5)
        st = agent({"execute": "guest-exec-status", "arguments": {"pid": pid}})
        try:
            d = json.loads(st)["return"]
        except Exception:
            continue
        if d.get("exited"):
            for k in ("out-data", "err-data"):
                if d.get(k):
                    print("%s: %s" % (k, base64.b64decode(d[k]).decode("utf-8", "replace")))
            print("exitcode:", d.get("exitcode"))
            return d.get("exitcode")
    print("超时: 命令未在 30 秒内结束")
    return None


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    mode = sys.argv[1]

    if mode == "raw":
        print(agent(json.loads(sys.argv[2])))
        return

    if mode == "exec":
        path, *args = sys.argv[2:]
        run_and_print(path, args)
        return

    if mode == "put":
        local, guest = sys.argv[2], sys.argv[3]
        data = open(local, "rb").read()
        write_file_guest(guest, data)
        print("已写入 %s (%d 字节)" % (guest, len(data)))
        return

    if mode == "get":
        guest, local = sys.argv[2], sys.argv[3]
        data = read_file_guest(guest)
        open(local, "wb").write(data)
        print("已取回 %s (%d 字节)" % (local, len(data)))
        return

    if mode == "ps":                       # powershell -EncodedCommand，彻底免转义
        script = sys.argv[2]
        enc = base64.b64encode(script.encode("utf-16-le")).decode()
        run_and_print("powershell.exe", ["-NoProfile", "-EncodedCommand", enc])
        return

    sys.exit(__doc__)


main()
