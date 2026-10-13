"""虚机连接与工作目录配置。

仓库里不写死主机名 / 口令 / 虚机 UUID —— 那些是本机环境的东西。运行时按顺序取：

  1. 环境变量（WXREPLY_SSH / WXREPLY_SSH_PW / WXREPLY_QGA_UUID / WXREPLY_WORK ...）
  2. 项目根下的 config.local.yaml（已在 .gitignore 里排掉），形如：

        vm:
          ssh: user@host
          ssh_pw: 你的口令
          qga_uuid: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

口令留空则走 ssh 公钥认证。
"""
import os
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _local():
    p = os.path.join(ROOT, "config.local.yaml")
    if not os.path.exists(p):
        return {}
    try:
        import yaml
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


_VM = (_local().get("vm") or {})


def get(env, key=None, default=""):
    v = os.environ.get(env)
    if not v and key:
        v = _VM.get(key)
    return v or default


TOOLS = os.path.join(ROOT, "tools")
WORK = get("WXREPLY_WORK", "work", os.path.join(ROOT, "store", "work"))
GA = get("WXREPLY_GA", None, os.path.join(TOOLS, "ga.py"))
PS1RUN = get("WXREPLY_PS1RUN", None, os.path.join(TOOLS, "ps1run.py"))
QGA_UUID = get("WXREPLY_QGA_UUID", "qga_uuid")
SSH_HOST = get("WXREPLY_SSH", "ssh")
SSH_PW = get("WXREPLY_SSH_PW", "ssh_pw")
QGA_LOCK = get("WXREPLY_QGA_LOCK", None, os.path.join(tempfile.gettempdir(), "wxreply-qga.lock"))


def ssh_cmd():
    """ssh 前缀。口令为空就走公钥认证。"""
    if not SSH_HOST:
        raise SystemExit("没配虚机 SSH：设 WXREPLY_SSH=user@host（配 WXREPLY_SSH_PW），"
                         "或写 config.local.yaml 的 vm.ssh / vm.ssh_pw")
    # accept-new：首次连接信任、之后按 known_hosts 校验 —— 别再 no 了，
    # 那等于每次连接都可能连到伪装的主机（审计 F-08）。
    base = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-n", SSH_HOST]
    if SSH_PW:
        # sshpass 会把口令放进本机进程命令行（ps 可见）。能配公钥就用公钥，
        # 这条路只为兼容现有配置保留，别再扩散。
        return ["sshpass", "-p", SSH_PW] + base
    return base


def ensure_work():
    os.makedirs(WORK, exist_ok=True)
    return WORK
