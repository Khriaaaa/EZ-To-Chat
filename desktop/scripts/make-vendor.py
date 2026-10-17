#!/usr/bin/env python3
"""准备 desktop/vendor/：自带 Python（embeddable）+ 三个依赖 + winapp.exe。

打包前跑一次。vendor/ 不进 git（.gitignore 了），谁来构建谁现生成。

跑法（Windows 构建机上，仓库里）：
    C:\\Py311\\python.exe desktop\\scripts\\make-vendor.py
    # 可选：--winapp-dir D:\\somewhere\\winapp-cli   直接拿本地这份，不下载
    # 可选：--python 3.11.9                          指定 embeddable 版本

来源：
    Python embeddable  —— npmmirror 的 python 镜像（国内快），失败退 python.org
    pillow/numpy/pyyaml —— pip download 只拉 win_amd64 / cp311 的 wheel（源：清华 PyPI 镜像）
    winapp.exe         —— 先找本地 C:\\winapp-cli，没有就下 GitHub Releases（MIT，可再分发）
"""
import argparse
import hashlib
import io
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DESKTOP = os.path.dirname(HERE)
VENDOR = os.path.join(DESKTOP, "vendor")
PYDIR = os.path.join(VENDOR, "python")
WINAPPDIR = os.path.join(VENDOR, "winapp")

MIRRORS = [
    "https://registry.npmmirror.com/-/binary/python/{v}/python-{v}-embed-amd64.zip",
    "https://www.python.org/ftp/python/{v}/python-{v}-embed-amd64.zip",
]
PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
SITE_PKGS = os.path.join(PYDIR, "Lib", "site-packages")
# winapp 下载锁死在具体版本 + SHA256（审计 F-01）；升版本时同步改
# desktop/preflight.ps1 与 scripts/setup-windows.ps1 里的同名常量
WINAPP_TAG = "v0.7.1"
WINAPP_ZIP_SHA256 = "d925d1e32cdc320b6d271f653fd2cd3c05b5d7558deb20d1b548bead77900fcf"


def fetch(url, dest, timeout=180):
    print("下载", url)
    req = urllib.request.Request(url, headers={"User-Agent": "make-vendor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    print("  ->", dest, os.path.getsize(dest), "bytes")


def get_embeddable(version):
    dest = os.path.join(VENDOR, "python-embed.zip")
    if os.path.exists(dest) and os.path.getsize(dest) > 5 * 1024 * 1024:
        print("已有", dest, "跳过下载")
    else:
        last = None
        for tpl in MIRRORS:
            try:
                fetch(tpl.format(v=version), dest)
                break
            except Exception as e:      # noqa: BLE001
                last = e
                print("  失败:", e)
        else:
            raise SystemExit("embeddable 下不下来: %s" % last)

    if os.path.isdir(PYDIR):
        shutil.rmtree(PYDIR)
    os.makedirs(PYDIR)
    with zipfile.ZipFile(dest) as z:
        for n in z.namelist():
            if n.startswith("/") or ".." in n.replace("\\", "/").split("/") or (len(n) > 1 and n[1] == ":"):
                raise SystemExit("embeddable 包里有不安全条目：%s" % n)
        z.extractall(PYDIR)
    print("解包到", PYDIR)

    # embeddable 默认不 import site、也不看 site-packages；显式补上，
    # 不然 numpy/pillow 一个都 import 不到
    tag = "".join(version.split(".")[:2])          # 3.11.9 -> "311"
    pth = os.path.join(PYDIR, "python%s._pth" % tag)
    with open(pth, "w", encoding="utf-8") as f:
        f.write("python%s.zip\n.\nLib\\site-packages\nimport site\n" % tag)
    print("写", os.path.basename(pth))


def _unsafe_member(name):
    """zip 条目名不安全（绝对路径 / 上跳 / 盘符）→ True。

    wheel 与 winapp 包统一走这一个判断（评审 B8）：wheel 名字由 PyPI 决定，
    但没理由相信压缩包里的路径。
    """
    n = name.replace("\\", "/")
    return (n.startswith("/") or ".." in n.split("/")
            or (len(n) > 1 and n[1] == ":"))


def get_wheels(version):
    os.makedirs(SITE_PKGS, exist_ok=True)
    whl_dir = os.path.join(VENDOR, "wheels")
    if os.path.isdir(whl_dir):
        shutil.rmtree(whl_dir)
    os.makedirs(whl_dir)
    short = "".join(version.split(".")[:2])
    # 锁文件在项目根（和 install.sh 共用同一份），不在 desktop/ 下（复核 R2-1）
    lock = os.path.join(os.path.dirname(DESKTOP), "requirements.lock")
    if os.path.isfile(lock):
        # 有锁文件就按锁下（--require-hashes 全哈希校验）—— 和 install.sh 同一道
        # 供应链闸门（复核 F-15）。锁里的版本要求 py3.11+，与嵌入式运行时一致。
        cmd = [sys.executable, "-m", "pip", "download", "--dest", whl_dir,
               "--only-binary=:all:", "--platform", "win_amd64",
               "--python-version", version.rsplit(".", 1)[0],
               "--implementation", "cp", "--abi", "cp" + short,
               "--require-hashes", "-r", lock, "-i", PIP_INDEX]
    else:
        print("⚠️ 找不到 requirements.lock —— 本次按「pillow/numpy/pyyaml 最新版」下载，"
              "没有哈希校验（供应链闸门失效）。打包发货前先把锁文件放进项目根。")
        cmd = [sys.executable, "-m", "pip", "download", "--dest", whl_dir,
               "--only-binary=:all:", "--platform", "win_amd64",
               "--python-version", version.rsplit(".", 1)[0],
               "--implementation", "cp", "--abi", "cp" + short,
               "-i", PIP_INDEX, "pillow", "numpy", "pyyaml"]
    print("pip download ...")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise SystemExit("pip download 失败，rc=%d" % r.returncode)
    for name in sorted(os.listdir(whl_dir)):
        if not name.endswith(".whl"):
            continue
        with zipfile.ZipFile(os.path.join(whl_dir, name)) as z:
            for member in z.namelist():
                if member.endswith("/"):
                    continue
                # .data 段（如果有）按 wheel 规范该铺到 sys.prefix，这几个包都没有，忽略
                if ".data/" in member:
                    continue
                if _unsafe_member(member):
                    raise SystemExit("%s 里有不安全条目：%s" % (name, member))
                target = os.path.join(SITE_PKGS, member)
                if os.path.commonpath([os.path.abspath(SITE_PKGS),
                                       os.path.abspath(target)]) != os.path.abspath(SITE_PKGS):
                    raise SystemExit("%s 的条目跑出 site-packages：%s" % (name, member))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(member) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        print("  铺好", name)


def get_winapp(local_dir):
    os.makedirs(WINAPPDIR, exist_ok=True)
    need = ["winapp.exe", "libSkiaSharp.dll", "libHarfBuzzSharp.dll"]
    src = None
    for cand in [local_dir, r"C:\winapp-cli", os.path.join(VENDOR, "winapp-cli")]:
        if cand and os.path.isfile(os.path.join(cand, need[0])):
            src = cand
            break
    if src:
        for n in need:
            shutil.copy2(os.path.join(src, n), os.path.join(WINAPPDIR, n))
            print("拷", n, os.path.getsize(os.path.join(WINAPPDIR, n)), "bytes")
    else:
        zip_path = os.path.join(VENDOR, "winapp-cli.zip")
        # 锁死版本 + SHA256（审计 F-01）：不用 releases/latest 动态挑资产，
        # 下载链路上任何一环改了内容，都会在解压前先失败
        url = ("https://github.com/microsoft/winappCli/releases/download/"
               "%s/winappcli-x64.zip" % WINAPP_TAG)
        fetch(url, zip_path)
        digest = hashlib.sha256(open(zip_path, "rb").read()).hexdigest()
        if digest != WINAPP_ZIP_SHA256:
            raise SystemExit("winapp 包哈希不对（期望 %s，实际 %s）" % (WINAPP_ZIP_SHA256, digest))
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                if _unsafe_member(n):
                    raise SystemExit("winapp 包里有不安全条目：%s" % n)
                base = os.path.basename(n)
                if base in need:
                    with z.open(n) as srcf, open(os.path.join(WINAPPDIR, base), "wb") as out:
                        shutil.copyfileobj(srcf, out)
        for n in need:
            p = os.path.join(WINAPPDIR, n)
            if not os.path.isfile(p):
                raise SystemExit("winapp 包里缺 %s" % n)

    lic = os.path.join(VENDOR, "winappLICENSE.txt")
    with open(lic, "w", encoding="utf-8") as f:
        f.write("winapp.exe / libSkiaSharp.dll / libHarfBuzzSharp.dll\n"
                "来自 microsoft/winappCli（MIT License）。\n"
                "https://github.com/microsoft/winappCli\n")
    print("vendor 就绪:", VENDOR)


def make_python_pack():
    """把整份运行时（python.exe + Lib\\site-packages 里的依赖）压成一个包，随安装包一起走。
    用户机器上若自带那份被删/被杀软清掉，preflight 直接本地解这个包补回去 ——
    不用联网、字节和自带那份一致。光下官方 embeddable 是不行的：它不带 pillow/numpy/pyyaml。"""
    pack = os.path.join(VENDOR, "python-pack.zip")
    if os.path.exists(pack):
        os.remove(pack)
    n = 0
    with zipfile.ZipFile(pack, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(PYDIR):
            for name in files:
                full = os.path.join(root, name)
                z.write(full, os.path.relpath(full, PYDIR))
                n += 1
    print("python-pack.zip: %d 个文件, %.1f MB" % (n, os.path.getsize(pack) / 1e6))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default="3.11.9", help="embeddable 版本，默认 3.11.9")
    ap.add_argument("--winapp-dir", default="", help="本地已有的 winapp-cli 目录（不下载）")
    a = ap.parse_args()

    os.makedirs(VENDOR, exist_ok=True)
    get_embeddable(a.python)
    get_wheels(a.python)
    get_winapp(a.winapp_dir)

    # 自检：能不能 import 到三个包（用自带那份 python）
    exe = os.path.join(PYDIR, "python.exe")
    if os.path.isfile(exe):
        r = subprocess.run([exe, "-c", "import PIL, numpy, yaml; print('PIL', PIL.__version__, '| numpy', numpy.__version__)"],
                           capture_output=True, text=True)
        print("自检:", (r.stdout or r.stderr).strip())
        if r.returncode != 0:
            raise SystemExit("自带 Python import 不了依赖，检查 wheels 铺的路径")
    else:
        print("（非 Windows：跳过自检，python.exe 不在这台机器上）")

    # 自检过了才打包 —— 补包必须是一份能跑起来的运行时
    make_python_pack()


if __name__ == "__main__":
    main()
