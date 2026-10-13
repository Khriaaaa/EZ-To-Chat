#!/usr/bin/env bash
# 一键准备运行环境（Linux 侧：采集器 / 编排 / 面板）
#
#   bash scripts/install.sh           # 装依赖 + 自检
#   bash scripts/install.sh --demo    # 顺便造一份合成演示数据，不连虚机也能看界面
#
# 幂等：重复跑不会重建已存在的 .venv，也不会覆盖已有的 config.local.yaml。
# 国内网络装依赖慢的话：PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple bash scripts/install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DEMO=0
for a in "$@"; do
  case "$a" in
    --demo) DEMO=1 ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$a（可用 --demo）" >&2; exit 2 ;;
  esac
done

if [ -t 1 ]; then G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'; else G=''; Y=''; R=''; N=''; fi
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '  %s✗%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
step() { printf '\n%s\n' "$*"; }

printf '微信回复助手 · 环境准备\n目录 %s\n' "$ROOT"

# ---------------------------------------------------------------- 1 Python
step "[1/5] Python"
PY="${PYTHON:-python3}"
# 锁文件（requirements.lock）里的 numpy/pillow 要求 3.11+，嵌入式运行时也是 3.11
command -v "$PY" >/dev/null 2>&1 || die "找不到 $PY，先装 Python 3.11 或更高"
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "$("$PY" -V) 太老，需要 3.11+（依赖锁文件的要求）"
ok "$("$PY" -V)"

# ---------------------------------------------------------------- 2 venv
step "[2/5] 虚拟环境 .venv"
VENV="$ROOT/.venv"
VPY="$VENV/bin/python3"
if [ -x "$VPY" ]; then
  ok "已存在，复用"
else
  "$PY" -m venv "$VENV" \
    || die "建 venv 失败（Debian/Ubuntu 上补 apt install python3-venv）"
  ok "已创建"
fi

# ---------------------------------------------------------------- 3 依赖
step "[3/5] Python 依赖"
# 有 requirements.lock 就按锁文件装（版本+哈希固定，供应链多一道闸，审计 F-15）；
# 没有就退回 requirements.txt。镜像换源照样可用：哈希是对文件内容算的，镜像文件一致。
if [ -f "$ROOT/requirements.lock" ]; then
  "$VPY" -m pip install -q --disable-pip-version-check --require-hashes -r "$ROOT/requirements.lock" \
    || die "装依赖失败（锁文件）—— 国内网络可换镜像重试：
  PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple bash scripts/install.sh"
else
  "$VPY" -m pip install -q --disable-pip-version-check -r requirements.txt \
    || die "装依赖失败 —— 国内网络可换镜像重试：
  PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple bash scripts/install.sh"
fi
"$VPY" - <<'PY' || die "依赖装上了但 import 不进来，看上面的报错"
import PIL, numpy, yaml
print(f'  \u2713 pillow {PIL.__version__} · numpy {numpy.__version__} · pyyaml {yaml.__version__}')
PY

# ---------------------------------------------------------------- 4 系统依赖
step "[4/5] 系统依赖"
if command -v sshpass >/dev/null 2>&1; then
  ok "sshpass（口令 ssh 到宿主要用）"
else
  warn "没有 sshpass —— config 里 vm.ssh_pw 填了口令就必须有它（apt install sshpass）；
    改用 ssh 公钥认证则可以不要"
fi
if command -v virsh >/dev/null 2>&1; then
  ok "virsh（本机就是虚拟化宿主）"
else
  warn "本机没有 virsh —— 采集器是 ssh 到宿主上跑 virsh 的，
    确认 config.local.yaml 的 vm.ssh 指向那台能 sudo virsh 的机器"
fi

# ---------------------------------------------------------------- 5 配置 + 自检
step "[5/5] 配置与自检"
if [ -f config.local.yaml ]; then
  ok "config.local.yaml 已存在，保持不动"
else
  cp config.example.yaml config.local.yaml
  ok "已从 config.example.yaml 生成 —— 里面的 vm.* 和模型端点还要自己填"
fi

"$VPY" - <<'PY' || die "有文件语法不过，看上面的文件名和行号"
import pathlib, sys
bad = []
for p in sorted(pathlib.Path('.').rglob('*.py')):
    if '.venv' in p.parts:
        continue
    try:
        compile(p.read_text(encoding='utf-8'), str(p), 'exec')
    except SyntaxError as e:
        bad.append(f'{p}:{e.lineno}: {e.msg}')
for b in bad:
    print('    ' + b, file=sys.stderr)
raise SystemExit(1 if bad else 0)
PY
ok "所有 .py 语法检查通过"

if [ "$DEMO" = 1 ]; then
  "$VPY" tools/make_demo_store.py --out store >/dev/null
  ok "已写入合成演示数据：store/messages.jsonl + store/suggestions.jsonl"
fi

cat <<EOF

装完了。下一步：

  看界面（不用连虚机）
      $VPY assistant.py up --port 8801 --no-collector
      然后打开 http://<这台机器的IP>:8801 —— 首次启动的随机密码在 store/.panel_password

  接模型
      编辑 config.local.yaml（或设 WXREPLY_CONFIG 指向别的 yaml）
      先验一遍拼出来的 prompt：$VPY orchestrator/generate.py --session <会话名> --dry-run --verbose

  连虚机真采集
      编辑 config.local.yaml 的 vm.ssh / vm.qga_uuid，然后在 Windows 那侧跑一次
      scripts/setup-windows.ps1（见 INSTALL.md 第 2、3 节）

  Windows 侧还要有 winapp CLI 和 PsExec64，都在 INSTALL.md 第 2 节里
EOF
