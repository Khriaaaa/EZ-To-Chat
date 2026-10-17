#!/usr/bin/env python3
"""自动生成触发器：盯 store/messages.jsonl，来了新消息就调 generate.py

用法:
  python3 watch.py [--store DIR] [--debounce SEC] [--interval SEC] [--once]

规则（对应方案里的「合并窗口」）:
  - 只看 sender == "them" 的新消息，我方自己发的不触发
  - 一条新消息到达后不立刻生成，等 --debounce 秒（默认 1.2）内没有新的再来
    才开始，避免对方连着发三条就生成三次
  - 启动时把已有消息全部记成「已见」，历史的不会触发
  - 触发后调 generate.py（子进程），结果追加到 store/suggestions.jsonl
  - 状态写到 store/watcher.json：last_trigger / last_result / last_error / seen_count

--once 跑一轮检查就退出，给测试用。
不发送任何消息、不碰键鼠。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 同 assistant.py：Windows 上被重定向的 stdout 按 GBK 编码，非 GBK 字符会崩
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

HERE = Path(__file__).resolve().parent
DEFAULT_STORE = HERE.parent / "store"
GENERATE = HERE / "generate.py"
PY = sys.executable


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_messages(path):
    if not path.exists():
        return []
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append((i, json.loads(line)))
        except json.JSONDecodeError:
            continue
    return out


# messages.jsonl 只追加，但 apply_sender_fixes 会「原地重写整文件」（行数不变、
# 每行长度可能变）。所以游标只能用「非空行数」，不能用字节偏移。整文件读一次
# 之后按 (size, mtime_ns) 缓存：没被改写就不重复读，也不重复解析（评审 B10）。
_FILE_CACHE = {"sig": None, "lines": []}


def read_lines(path):
    """返回 (非空行列表, 签名)。签名不变就复用上一次的读结果。"""
    try:
        st = path.stat()
    except OSError:
        return [], None
    sig = (st.st_size, st.st_mtime_ns)
    if _FILE_CACHE["sig"] == sig:
        return _FILE_CACHE["lines"], sig
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(line)
    except OSError:
        return [], sig
    _FILE_CACHE["sig"] = sig
    _FILE_CACHE["lines"] = out
    return out, sig


def fp_of(m):
    """消息身份：优先用采集层算好的 fp，老行（没 fp 字段）按内容算。

    没有 fp 的老行以前退到 "line-<物理行号>"，而物理行号会随文件重写而整体错位
    （人工编辑插了一行、半行写入被跳过），后果是同一条消息算出两个指纹 ->
    重复触发；反过来新消息的指纹撞上旧指纹 -> 永远不触发，且没有任何报错。
    内容哈希和行号无关，且和采集层一样故意不含 sender（sender 会被
    apply_sender_fixes 原地改写，含进去就会把同一条消息算成新的）。
    """
    fp = m.get("fp")
    if fp:
        return fp
    parts = [str(m.get("session") or ""), str(m.get("text") or ""), str(m.get("time_hint") or "")]
    if not "".join(parts):
        return None          # 整行没内容，不参与触发
    return "h-" + hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]


def new_incoming(store, seen, start=0):
    """从第 start 个非空行往后扫，返回 (新对方消息, {会话: 指纹}, 其它新指纹, 总行数)。

    指纹按会话分开返回，是为了「生成失败时只撤回这一个会话」。老实现把整批指纹
    放在一个列表里，前面的会话生成成功就把列表清空了，后面的会话再失败时已经
    无指纹可撤 —— 它的消息被永久记成「已见」，不生成、不报错、面板也看不出来。

    start 是「已确认的行数」游标。以前靠一份上限 2000 条的指纹集合判新旧，库里
    消息一多最老的指纹就被挤掉，重启后整段历史都被当成新消息，逐个会话调模型
    （评审 B3：5000 条时一次重启触发 30 个会话）。行数游标不受总量影响。
    """
    path = store / "messages.jsonl"
    lines, _sig = read_lines(path)
    if len(lines) < start:
        # 文件被截断或换了一份：宁可这轮不触发，也绝不把整库当新消息
        return [], {}, [], len(lines)
    fresh, by_sess, others = [], {}, []
    for line in lines[start:]:
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = fp_of(m)
        if key is None or key in seen:
            continue
        seen[key] = None          # 有序：save_seen 截断时丢的才是最旧的
        if m.get("sender") == "them":
            fresh.append(m)
            by_sess.setdefault(m.get("session"), []).append(key)
        else:
            others.append(key)
    return fresh, by_sess, others, len(lines)


# generate.py 自己管一个总预算（WXREPLY_BUDGET，默认 150s，见那边的 BUDGET_SEC）；
# 这里只要比它多一点，免得它还没轮到备用 provider 就被杀掉。
GEN_TIMEOUT = float(os.environ.get("WXREPLY_BUDGET", "150")) + 30


def run_generate(store, session, timeout=GEN_TIMEOUT):
    cmd = [PY, str(GENERATE), "--session", session,
           "--messages", str(store / "messages.jsonl"),
           "--out", str(store / "suggestions.jsonl")]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or "").strip()[-400:]
    try:
        rec = json.loads(p.stdout[p.stdout.index("{"):])
        return True, rec.get("strategy") or "ok"
    except Exception:
        return True, "ok"


def write_status(store, **kw):
    path = store / "watcher.json"
    cur = {}
    if path.exists():
        try:
            cur = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cur = {}
    cur.update(kw)
    cur["updated_utc"] = now_utc()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state(store):
    """返回 (已确认的非空行数, 已确认指纹列表)；None = 没有可用状态。"""
    path = store / "watcher_seen.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict) and data.get("version") == 2:
        try:
            return int(data.get("lines") or 0), list(data.get("done") or [])
        except (TypeError, ValueError):
            return None
    # 旧格式（纯指纹数组）：没有行游标，按「首次」处理 —— 历史不触发
    return None


def save_state(store, lines, done, cap=5000):
    """落盘「已确认行数」游标 + 游标之后的已确认指纹。

    done 只装游标之后、还没推进的那一段；所有会话都生成完就清空，所以 cap
    只是保险丝。真被截断也只是多生成一次，不会像旧实现那样把整段历史当成
    新消息（评审 B3：上限 2000 条，5000 条库重启一次触发 30 个会话）。
    """
    path = store / "watcher_seen.json"
    items = list(done)[-cap:]
    payload = {"version": 2, "lines": int(lines), "done": items}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def main():
    ap = argparse.ArgumentParser(description="盯新消息并自动触发候选生成（不发送）")
    ap.add_argument("--store", default=str(DEFAULT_STORE), help="数据目录，默认 ../store")
    ap.add_argument("--debounce", type=float, default=1.2, help="静默多少秒后才生成")
    ap.add_argument("--interval", type=float, default=2.0, help="轮询间隔")
    ap.add_argument("--retry", type=float, default=60.0, help="生成失败后多久重试（秒）")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    store = Path(a.store)
    store.mkdir(parents=True, exist_ok=True)

    state = load_state(store)
    if state is None:
        # 首次跑（或状态是旧格式）：把已有消息全记成已见，历史的不会触发
        lines_all, _sig = read_lines(store / "messages.jsonl")
        n_lines = len(lines_all)
        done = {}
        save_state(store, n_lines, done)
    else:
        n_lines, done_list = state
        done = {k: None for k in done_list}
    # seen = 内存里「已扫过」的指纹；done = 已确认、可以落盘的指纹。
    # 两者分开是为了「待生成」的行不被提前写盘：只有生成成功、或本来就不需要
    # 触发的行（我方消息/未知发送方）才进 done。watch 若在待生成期间被杀，
    # 重启后这些行仍然是「没见过」，不会静默丢掉。
    seen = dict(done)
    if a.verbose:
        print(f"[watch] 起步已见 {len(seen)} 条", file=sys.stderr)

    pending_sessions = []          # 待生成的会话，一次 poll 批里可能有多个
    pending_at = 0.0
    pending = {}                   # 会话 -> 该会话待生成行的指纹
    retry_at = 0.0
    total = 0                      # 最近一次扫描看到的非空行数

    while True:
        if time.time() >= retry_at:
            fresh, by_sess, others, total = new_incoming(store, seen, n_lines)
            if others:
                for k in others:                     # 我方消息 / 未知发送方：不触发，直接确认
                    done[k] = None
            if fresh:
                for s, ks in by_sess.items():        # 每个会话都要生成，不能只留最后一个
                    if s and s not in pending_sessions:
                        pending_sessions.append(s)
                    if s:
                        pending[s] = pending.get(s, []) + ks
                pending_at = time.time()
                if a.verbose:
                    print(f"[watch] 收到 {len(fresh)} 条新消息，会话={pending_sessions}", file=sys.stderr)
                if a.once:
                    # --once 也要走完静默期，否则永远来不及触发
                    time.sleep(a.debounce)

        if pending_sessions and (time.time() - pending_at) >= a.debounce:
            sess = pending_sessions.pop(0)
            pending_at = time.time()                 # 多个会话时逐个生成，各自留一个静默期
            try:
                ok, info = run_generate(store, sess)
            except Exception as e:
                # 生成超时/子进程异常以前会直接击穿 while，watch 静默死掉、自动生成从此停摆
                ok, info = False, ("%s: %s" % (type(e).__name__, e))[:400]
            if ok:
                for k in pending.pop(sess, []):      # 只有成功才把这一批指纹落盘
                    done[k] = None
                if not pending_sessions and total:
                    # 所有会话都生成完了：游标推到文件尾，done/seen 清空
                    n_lines = total
                    done = {}
                    seen = {}
                save_state(store, n_lines, done)
            else:
                # 失败：只撤回**这个会话**的指纹，别的会话不受影响（它们的指纹本来
                # 就没进 done，下一轮照常生成）。撤回后这批消息下一轮还是「新消息」，
                # 于是会被重试，而不是被静默丢掉。
                for k in pending.pop(sess, []):
                    seen.pop(k, None)
                # 注意不要在这里把 sess 塞回 pending_sessions：那样下一轮 debounce
                # 一到就会立刻重跑（绕过 retry 间隔），失败一次变成连跑两次。
                # 它的指纹已经从 seen 撤掉，retry 到点后重扫时自然会被当成新消息再进来。
                retry_at = time.time() + a.retry
            write_status(
                store,
                last_trigger=now_utc(),
                last_session=sess,
                last_result="ok" if ok else "error",
                last_error=None if ok else info,
                last_detail=info,
                seen_count=len(seen),
            )
            print(f"[watch] {now_utc()} 生成 {sess}: {'成功' if ok else '失败'} - {info[:120]}")

        if a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
