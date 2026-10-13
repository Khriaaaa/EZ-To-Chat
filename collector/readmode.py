"""采集读取路线开关：界面树 / 本地库直读。

三条路都能让 messages.jsonl 长出同样的行，下游（watch / generate / 面板）不用改，
区别只在"从哪拿消息"：

  ui    走界面树（原路）：读微信窗口的 UIA 树 + 截图判气泡左右。要求窗口可见、
        桌面没锁屏、会话是打开的那个。稳，但依赖界面状态。
  db    走本地库直读：解密微信本地的 SQLCipher 库。锁屏、最小化、窗口被盖都能读，
        但要能从微信进程内存里取到密钥（微信大版本换了锚点就会失败）。
  auto  先试 db，本轮拿不到就回落 ui，并把原因记进 state —— 默认值。

优先级：环境变量 WXREPLY_READ_MODE > store/collect.yaml 的 read_mode > 默认 auto。
为什么默认 auto：两种读法都会往同一个文件里写同一种行，谁先好用就用谁；
但**实际走的哪条路必须可查**，不然出了问题没法说清是读法的问题还是数据的问题 ——
所以每轮都记 state，面板上直接显示。
"""
import json
import os
import time
from datetime import datetime, timezone

MODES = ("ui", "db", "auto")
DEFAULT = "auto"
CONFIG_NAME = "collect.yaml"
STATE_NAME = ".readmode_state.json"

DESC = {
    "ui": "界面树读取（要求微信窗口可见、桌面未锁屏）",
    "db": "本地库直读（锁屏也能读，需要能从微信进程取到密钥）",
    "auto": "先试本地库直读，失败回落界面树",
}


def _env():
    return (os.environ.get("WXREPLY_READ_MODE") or "").strip().lower()


def config_path(store_dir):
    return os.path.join(store_dir, CONFIG_NAME)


def state_path(store_dir):
    return os.path.join(store_dir, STATE_NAME)


def _read_config(store_dir):
    """store/collect.yaml 只认一行 read_mode，故意手写解析：

    项目里读 yaml 的地方都依赖 pyyaml，而这个开关要在采集器最早的一步就能用，
    不想因为 yaml 缺失把整条采集链拖死。
    """
    try:
        with open(config_path(store_dir), "r", encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line or ":" not in line:
                    continue
                k, v = line.split(":", 1)
                if k.strip() == "read_mode":
                    return v.strip().strip("'\"").lower()
    except OSError:
        pass
    return ""


def resolve(store_dir):
    """当前该走哪条路。非法值一律回落默认，并把非法值记进 state 供面板提示。"""
    choice = _env() or _read_config(store_dir)
    if choice in MODES:
        return choice
    if choice:
        record(store_dir, mode=DEFAULT, via="", note="配置值 %r 不认识，已按 %s 处理"
               % (choice, DEFAULT))
    return DEFAULT


def set_mode(store_dir, mode):
    """写进 store/collect.yaml（保留文件里其它行）。"""
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        raise ValueError("read_mode 只能是 %s" % "/".join(MODES))
    os.makedirs(store_dir, exist_ok=True)
    p = config_path(store_dir)
    lines, seen = [], False
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                if line.split("#", 1)[0].strip().startswith("read_mode"):
                    lines.append("read_mode: %s\n" % mode)
                    seen = True
                else:
                    lines.append(line)
    except OSError:
        lines = []
    if not seen:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append("read_mode: %s\n" % mode)
    tmp = p + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(tmp, p)
    return mode


def record(store_dir, mode, via, note="", detail=None):
    """记下这一轮实际走的路线（面板直接显示，出问题好归因）。"""
    data = {"mode": mode, "via": via, "note": note,
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ts": int(time.time())}
    if detail:
        data["detail"] = detail
    try:
        os.makedirs(store_dir, exist_ok=True)
        tmp = state_path(store_dir) + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, state_path(store_dir))
    except OSError:
        pass
    return data


def last_state(store_dir):
    try:
        with open(state_path(store_dir), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def describe(store_dir=None):
    mode = resolve(store_dir) if store_dir else DEFAULT
    return {"mode": mode, "desc": DESC.get(mode, ""), "last": last_state(store_dir) if store_dir else {}}
