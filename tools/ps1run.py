#!/usr/bin/env python3
"""把本地 .ps1 送进 win10 guest 的 session 1（交互会话）执行，再把输出取回来。

硬规则（都踩过）：
  1. ga.py 起的进程在 session 0，UIA / winapp-cli 在 session 0 读不到窗口，
     必须 PsExec -i 1 注入交互会话。
  2. .ps1 只送 ASCII 会被 PS 5.1 按 ANSI 读 —— 中文变乱码砸坏引号。
     解法：写文件时**加 UTF-8 BOM**（PS 5.1 认 BOM），中文选择器就能安全落地。
  3. 不要把长脚本塞进 -EncodedCommand：外层 QGA 会在某个长度阈值上直接
     报 `guest-exec: Failed to execute child process (Permission denied)`，
     跟内容无关、跟长度有关，别在这上面浪费时间。落地成文件最稳。

用法:
    ps1run.py <本地ps1> [--out-remote C:\\dl\\x.txt] [--out-local 路径]
              [--wait 90] [--pull-extra C:\\dl\\y.png:本地.png] [--no-wait]
"""
import argparse
import base64
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GA = os.path.join(HERE, "ga.py")


def ga(*args, timeout=150):
    r = subprocess.run([sys.executable, GA, *args], capture_output=True,
                       text=True, timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def push(local_path, remote_path):
    src = open(local_path, "rb").read()
    data = b"\xef\xbb\xbf" + src            # UTF-8 BOM，PS 5.1 才会按 UTF-8 读
    b64 = base64.b64encode(data).decode()
    # 单引号字符串里 ' 要写成 ''：remote_path 可能来自含引号的目录名，
    # 不转义就是命令注入面（审计 F-16）
    script = ("[IO.File]::WriteAllBytes('%s',[Convert]::FromBase64String('%s'))"
              % (remote_path.replace("'", "''"), b64))
    out = ga("ps", script)
    if "exitcode: 0" not in out:
        print("[!] 写入失败:", out[-300:])
        sys.exit(3)
    return len(data)


def launch(remote_ps1):
    ps = ("Start-Process 'C:\\dl\\PsExec64.exe' -ArgumentList "
          "'-accepteula','-nobanner','-i','1','-d','powershell.exe',"
          "'-NoProfile','-ExecutionPolicy','Bypass','-File','%s'"
          % remote_ps1.replace("'", "''"))
    return ga("ps", ps)


def wait_for(remote_out, seconds, min_bytes=1):
    """轮询 guest 侧文件大小。注意只认 out-data 那一行，CLIXML 里也有数字会骗人。"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        chk = ga("ps", "$p='%s'; if (Test-Path $p) { (Get-Item $p).Length } else { 'NO' }"
                 % remote_out.replace("'", "''"))
        val = 0
        for line in chk.splitlines():
            line = line.strip()
            if line.startswith("out-data:"):
                body = line.split(":", 1)[1].strip()
                if body.isdigit():
                    val = int(body)
                break
        if val >= min_bytes:
            return val
        time.sleep(4)
    return 0


def pull(remote, local, tries=4):
    for _ in range(tries):
        ga("get", remote, local)
        if os.path.exists(local) and os.path.getsize(local) > 0:
            return os.path.getsize(local)
        time.sleep(3)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ps1")
    ap.add_argument("--out-remote", default="C:\\dl\\ps1run_out.txt")
    ap.add_argument("--out-local", default="")
    ap.add_argument("--wait", type=int, default=90)
    ap.add_argument("--min-bytes", type=int, default=1)
    ap.add_argument("--pull-extra", action="append", default=[],
                    help="C:\\dl\\x.png:本地.png")
    ap.add_argument("--no-wait", action="store_true")
    a = ap.parse_args()

    remote_ps1 = "C:\\dl\\" + os.path.basename(a.ps1)
    out_local = a.out_local or os.path.splitext(a.ps1)[0] + ".out.txt"

    n = push(a.ps1, remote_ps1)
    print("[ps1run] 已写入 %s（含 BOM %d 字节）" % (remote_ps1, n))
    r = launch(remote_ps1)
    print("[ps1run] 启动 " + ("失败" if "exitcode: 0" not in r else "成功"))

    if a.no_wait:
        return
    size = wait_for(a.out_remote, a.wait, a.min_bytes)
    if not size:
        print("[!] 等 %ds 没等到 %s（或不足 %d 字节）" % (a.wait, a.out_remote, a.min_bytes))
        sys.exit(4)
    time.sleep(2)
    got = pull(a.out_remote, out_local)
    print("[ps1run] 取回 %s（guest %d / 本地 %d 字节）" % (out_local, size, got))
    for spec in a.pull_extra:
        remote, local = spec.split(":", 1)
        print("[ps1run] 附件 %s -> %s（%d 字节）" % (remote, local, pull(remote, local)))


if __name__ == "__main__":
    main()
