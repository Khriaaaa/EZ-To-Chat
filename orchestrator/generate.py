#!/usr/bin/env python3
"""编排层 v1：读聊天记录 -> 拼 prompt -> 调 LLM -> 校验 -> 追加 suggestions.jsonl

用法:
  python3 generate.py [--session NAME] [--limit 20]
                                            [--dry-run] [--out PATH] [--verbose]
只读 store/messages.jsonl，只写 store/suggestions.jsonl（或 --out 指定）。
不发消息、不碰键鼠。API key 不会被打印。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_history import load_history_ctx

import yaml

# 同 assistant.py：Windows 上被重定向的 stdout 按 GBK 编码，非 GBK 字符会崩
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = Path(__file__).resolve().parent.parent
# store 目录可用 WXREPLY_STORE 覆盖（只为测试和多实例；面板侧同名变量同义）。
# 注意 main() 里 store 是拿来放 .gen.lock 的：不传 --messages 时以前引用了一个
# 从没定义过的名字，面板那条路径（带 --session 不带 --messages）会直接 NameError。
STORE = Path(os.environ.get("WXREPLY_STORE", str(ROOT / "store")))
MESSAGES = STORE / "messages.jsonl"
SUGGESTIONS = STORE / "suggestions.jsonl"
PROMPT = ROOT / "prompts" / "reply_system.md"

# provider 凭据来自 Hermes 的 config.yaml。
# 容器里用绝对路径；放到虚机/别的机器上跑时那个路径不存在，
# 于是支持两种覆盖：环境变量 WXREPLY_CONFIG，或项目根下的 config.local.yaml。
CONFIG_HINT = Path("/opt/data/config.yaml")
# 面板「首次配置」写的就是这个文件：单独的、只留在本机的小配置，专门放模型接口凭据。
# 不去改用户的 config.local.yaml —— 那里面常有自己的注释和 vm 段，整段重写会把注释吃掉。
# 默认跟 STORE 走（WXREPLY_STORE 覆盖 Finding 时一起挪）：不然面板把 .llm.yaml
# 写进覆盖的 store，generate 却回根 store 去找 —— 面板说已配置、生成说没 provider，
# 实测就是这么岔开的。
LLM_CFG = Path(os.environ.get("WXREPLY_LLM_CONFIG", str(STORE / ".llm.yaml")))
_explicit_cfg = os.environ.get("WXREPLY_CONFIG")
if _explicit_cfg:
    CONFIG = Path(_explicit_cfg)
elif (ROOT / "config.local.yaml").exists():
    CONFIG = ROOT / "config.local.yaml"
else:
    CONFIG = CONFIG_HINT


def _config_candidates():
    """依次找 provider 凭据的候选配置文件。

    WXREPLY_CONFIG 给了就只认它（写错了要吵出来）；
    否则面板写的 .llm.yaml 优先（那是用户在网页上刚填的），
    再 config.local.yaml，最后才是随 Hermes 一起来的 /opt/data/config.yaml。
    拼不出 provider 链时继续往下找，而不是停在第一份上 ——
    config.local.yaml 常常只填了 vm 段。
    """
    if _explicit_cfg:
        return [Path(_explicit_cfg)]
    cands = []
    if LLM_CFG.exists():
        cands.append(LLM_CFG)
    local = ROOT / "config.local.yaml"
    if local.exists():
        cands.append(local)
    if CONFIG_HINT.exists():
        cands.append(CONFIG_HINT)
    return cands or [CONFIG_HINT]

# 链首模型：只影响这个工具，不动 Hermes 主链路（Hermes 自己继续走 deepseek-v4.1-flash）。
# 传 --provider none 可回退到 config.yaml 的 model.default + fallback_providers。
# 2026-10-05 起用 mimo-v2.6-flash：带推理、直接出 JSON，不受 commandcode 5 小时窗口影响。
# 想要更强的用 --model mimo-v2.6-pro（实测 9.0s / 181 out tok，flash 是 5.7s / 115）。
# 换成自己的 provider / 模型：设 WXREPLY_LEAD_PROVIDER / WXREPLY_LEAD_MODEL。
# provider 名的写法跟 config.yaml 里 provider 段的键一致（形如 custom:xxx）。
# 下面这对是「本机默认值」：别人的配置里查不到它就自动让位给 model/fallback 链，
# 不会因为一个本机专有的 provider 名把整个生成打挂（显式指定才报错）。
BUNDLED_LEAD = ("custom:mimoplan", "mimo-v2.6-flash")
LEAD_PROVIDER = os.environ.get("WXREPLY_LEAD_PROVIDER", BUNDLED_LEAD[0])
LEAD_MODEL = os.environ.get("WXREPLY_LEAD_MODEL", BUNDLED_LEAD[1])

# 整条候选链的总时间预算（秒），可用 WXREPLY_BUDGET 覆盖。
# 链上每个 provider 的单发超时是 120s，链长 4 个时最坏 480s —— 比调用方的
# subprocess 超时（watch / 面板自检都是 180s 上下）还长，于是 generate 会在
# 还没轮到备用 provider 时就被 SIGKILL，60 秒后再原样重试一遍，永远轮不到降级。
# 所以这里自己管总预算：每个 provider 只分到「剩余预算」，用完就带着每个候选的
# 真实原因退出。调用方的超时取 budget + 30 即可，不需要跟着链长改。
BUDGET_SEC = float(os.environ.get("WXREPLY_BUDGET", "150"))
# 历史上下文条数上限（展开条数计），见 orchestrator/render_history.py
HISTORY_LIMIT = 40


def pick_session(msgs):
    """要回的是「最后一条对方消息」所在的会话。

    这条规则必须只有一份：面板展示的会话、面板自检决定生成哪个会话、generate 自己
    算 session，三处以前各写各的 —— 面板取 msgs[-1]（最后一条不论谁发的），于是
    「我刚在 B 会话回了一句」会让面板切到 B，而 A 会话的新消息生成的建议被
    按会话过滤掉，界面上就成了「明明有建议却显示还没有生成」。
    没有对方消息时退回最后一条消息的会话。
    """
    for m in reversed(msgs):
        if not isinstance(m, dict):
            continue
        if m.get("sender") == "them" and m.get("session"):
            return m["session"]
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("session"):
            return m["session"]
    return None


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


# 退出码分档：调用方（面板）按码给出一句人话，stderr 原文只留在服务端日志里。
# stderr 里会出现模型返回原文 —— 那里面就是聊天内容，不能回浏览器、不能进 checker.json。
EXIT_BUSY = 75           # 拿不到跨进程生成锁：调用方据此回「已有生成任务在跑」
EXIT_NO_PROVIDER = 2     # 一个能用的 provider 都没配出来
EXIT_PROVIDER_FAIL = 3   # provider 都试过，请求层面全失败（Key / 额度 / 网络）
EXIT_BAD_OUTPUT = 4      # 请求成功但内容不合规（JSON 挖不出来 / score 不合法）
EXIT_NO_MESSAGE = 5      # 没有消息可回


class ProviderError(RuntimeError):
    """请求层面没成功：连不上、HTTP 报错、响应信封不是 OpenAI 形状。"""


class OutputError(ValueError):
    """请求成功，但模型给的内容不合规。子类化 ValueError，老的 except 写法照样接得住。"""


class GenLock:
    """跨进程互斥：面板自检线程、面板手动「生成」、watch.py 是三路人马。

    server.py 里那把 threading.Lock 只在面板自己的进程内有效，watch.py 是另一个
    进程、拿 subprocess 直接拉 generate，于是同一条新消息会被两边各生成一次，
    suggestions.jsonl 里多一条近重复记录、白烧一次模型额度。锁放在 store 下，
    谁要生成都得先拿到它。非阻塞：拿不到说明别人正在跑，直接退出（退出码 75），
    让调用方去报「已有生成任务在跑」，而不是排队再跑一遍。
    """

    def __init__(self, store=None):
        self.path = Path(store or STORE) / ".gen.lock"
        self.f = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close()
            self.f = None
            die("另一处正在生成（store/.gen.lock 被别人拿着），这次不重复跑", code=EXIT_BUSY)
        return self

    def __exit__(self, *exc):
        try:
            if self.f is not None:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
                self.f.close()
        except Exception:
            pass
        self.f = None
        return False


def load_messages(path=None):
    src = Path(path) if path else MESSAGES
    if not src.exists():
        die(f"找不到 {src}")
    msgs = []
    for i, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            msgs.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"警告: messages.jsonl 第 {i} 行解析失败: {e}", file=sys.stderr)
    if not msgs:
        die("messages.jsonl 里没有消息", code=EXIT_NO_MESSAGE)
    return msgs


def build_transcript(msgs, session, limit):
    sel = [m for m in msgs if m.get("session") == session][-limit:]
    if not sel:
        die(f"会话 {session!r} 没有消息", code=EXIT_NO_MESSAGE)
    lines = []
    for m in sel:
        s = m.get("sender")
        if s == "me":
            who = "我"
        elif s == "them":
            who = f"对方({session})"
        else:
            who = "未知"
        t = m.get("time_hint") or ""
        # 单条消息长度封顶（审计 F-11）：对方可以发超长文本，不封顶会挤爆 prompt 预算
        txt = str(m.get("text", ""))
        if len(txt) > 2000:
            txt = txt[:2000] + " …[截断]"
        lines.append(f"[{t}] {who}: {txt}" if t else f"{who}: {txt}")
    # 顺带把最后一条「对方」消息的指纹带出去：自检线程靠它判断「建议是不是已经
    # 跟上最新消息」。故意不看最后一条是不是我发的 —— 我方消息不该触发新一轮生成
    # （watch 侧同样只认 sender=="them"），否则我自己每发一条消息，自检就白烧一次模型。
    last_fp = next((m.get("fp") for m in reversed(sel)
                    if m.get("sender") == "them" and m.get("fp")), None)
    return "\n".join(lines), len(sel), last_fp


def _provider_creds(cfg, prov_ref):
    """prov_ref 形如 custom:commandcode；返回 (base_url, api_key)。"""
    name = prov_ref.split(":", 1)[1] if ":" in prov_ref else prov_ref
    for p in cfg.get("custom_providers") or []:
        if p.get("name") == name:
            return (p.get("base_url") or "").rstrip("/"), p.get("api_key")
    return None, None


def _chain_from(cfg, override):
    """从一份配置里拼链：override(如有) -> model(主) -> fallback_providers(依次)。

    返回 [(label, base_url, api_key, model), ...]，拼不出就是空表。
    实测必要：commandcode 的 5 小时窗口被吃光后主模型直接 429，
    没有这条链生成就整个废掉。
    override 形如 ("custom:mimoplan", "mimo-v2.5")，只排链首、不写配置文件，
    所以换这个工具用哪家模型不动 Hermes 主链路。
    """
    chain, seen = [], set()

    if override:
        prov_ref, mid = override
        base, key = _provider_creds(cfg, prov_ref)
        if base and key:
            chain.append((prov_ref, base.rstrip("/"), key, mid))

    m = cfg.get("model") or {}
    if m.get("default") and m.get("provider"):
        base, key = _provider_creds(cfg, m["provider"])
        base = (m.get("base_url") or base or "").rstrip("/")
        if base and key:
            chain.append((m["provider"], base, key, m["default"]))

    for fb in cfg.get("fallback_providers") or []:
        prov, mid = fb.get("provider"), fb.get("model")
        if not prov or not mid:
            continue
        base, key = _provider_creds(cfg, prov)
        base = (fb.get("base_url") or base or "").rstrip("/")
        if not base or not key:
            continue
        sig = (base, mid)
        if sig in seen:
            continue
        seen.add(sig)
        chain.append((prov, base, key, mid))

    return chain


def load_providers(override=None):
    """按顺序在多份配置里找能用的 provider 链，取第一份拼得出来的。

    config.local.yaml 常常只填了 vm 段 —— 不该因为它存在，
    就把 /opt/data/config.yaml 里的模型凭据整个遮蔽掉（实测踩过）。
    """
    tried, cfgs = [], []
    for path in _config_candidates():
        tried.append(str(path))
        try:
            cfgs.append(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        except Exception as e:
            print(f"[info] 读不了 {path}: {e}", file=sys.stderr)

    # 显式点名要用的 provider 全都没找到 —— 直接报错，别偷偷换一个模型跑
    if override:
        explicit = tuple(override) != BUNDLED_LEAD or bool(os.environ.get("WXREPLY_LEAD_PROVIDER"))
        if explicit and not any(all(_provider_creds(cfg, override[0])) for cfg in cfgs):
            die(f"指定 provider {override[0]} 在配置里查不到 base_url / api_key（找过 {', '.join(tried)}）",
                code=EXIT_NO_PROVIDER)

    for cfg in cfgs:
        chain = _chain_from(cfg, override)
        if chain:
            return chain

    die("没解析出任何可用的 provider（找过 " + ", ".join(tried) + "）", code=EXIT_NO_PROVIDER)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不许 urllib 跟着 3xx 走。

    默认 opener 会跟随 3xx，把带 Authorization 的请求转发到 Location 指定的
    任意地址（含内网）—— API Key 一起送过去（审计 F-03；本地实测 301/302/303
    都会转发）。redirect_request 返回 None → urllib 直接抛 HTTPError，下面的
    错误分支会把它当「HTTP 3xx」报失败。面板侧有同样的实现（web/server.py）。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _try_one(base, key, model, prompt_text, verbose, timeout=120):
    """打一发；成功返回 content 字符串，失败抛 Exception（带真实原因）。"""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt_text}],
        "temperature": 0.7,
        "max_tokens": 2000,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        # 裸 urllib 不带 UA 会被 Cloudflare 拦成 403 code 1010
        "User-Agent": "OpenAI/Python 1.0.0",
    }
    # opencode Go 强制要求 x-opencode-session，缺了直接 400 MissingSessionID
    #（Hermes 自己会注入这个头，裸脚本得手写）
    if "opencode.ai" in base:
        headers["x-opencode-session"] = "hermes-wxreply"
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with _opener.open(req, timeout=timeout) as r:
            # 响应大小封顶（审计 F-18）：恶意/故障 provider 返回超大响应时别把内存打爆
            raw = r.read(300000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise ProviderError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}") from None
    except Exception as e:
        raise ProviderError(f"{type(e).__name__}: {e}") from None
    if verbose:
        print(f"[verbose] {model} 响应长度 {len(raw)}", file=sys.stderr)
    try:
        content = json.loads(raw)["choices"][0]["message"]["content"]
    except Exception as e:
        raise ProviderError(f"响应结构异常 ({e})，原始内容: {raw[:400]}") from None
    if not content or not content.strip():
        # 实测：带推理的模型 max_tokens 太小时 content 会是空串 —— 请求是通的，
        # 是模型没吐出东西，所以算「输出不合规」，让调用方提示「可重试」
        raise OutputError("返回内容为空")
    return content


def call_llm(prompt_text, verbose, override=None, budget=None):
    """依次尝试候选 provider，第一个成功的胜出。全挂才报错，并带上每一条的真实原因。

    每个候选只分到「总预算剩余」这么多时间，保证整轮一定在预算内结束 —— 否则链尾的
    备用 provider 永远轮不到（外层 subprocess 先把 generate 杀了）。
    """
    errors = []
    req_fail = 0
    bad_out = 0
    budget = BUDGET_SEC if budget is None else float(budget)
    deadline = time.monotonic() + budget
    for label, base, key, model in load_providers(override):
        left = deadline - time.monotonic()
        if left <= 2:            # 剩不到 2 秒就发出去也只会超时，直接跳过
            errors.append(f"  - {label} / {model}: 跳过（总预算 {budget:.0f}s 已用完）")
            continue
        try:
            content = _try_one(base, key, model, prompt_text, verbose, timeout=min(120.0, left))
            # 校验也留在这个 try 里：返回 200 但 JSON 不合规（弱模型最常见的坏法）
            # 同样算这个 provider 失败，接着试下一个，而不是整轮 die 掉。
            obj = parse_and_validate(content)
            if verbose and errors:
                print(f"[verbose] 主 provider 失败、已降级到 {label}/{model}", file=sys.stderr)
            return obj, label, model
        except Exception as e:
            errors.append(f"  - {label} / {model}: {e}")
            if isinstance(e, ValueError):    # OutputError：200 但内容不合规
                bad_out += 1
            else:
                req_fail += 1
            if verbose:
                print(f"[verbose] {label} 失败: {e}", file=sys.stderr)
    # 分开报：全是「内容不合规」时提示可重试，别让用户去查 Key 和额度
    if bad_out and not req_fail:
        die("所有 provider 都返回了不合规的内容:\n" + "\n".join(errors), code=EXIT_BAD_OUTPUT)
    die("所有 provider 都失败了:\n" + "\n".join(errors), code=EXIT_PROVIDER_FAIL)


def extract_json(content):
    """从模型返回里挖出那个 JSON 对象。

    只剥首尾代码围栏不够用：模型很爱在前面垫一句「好的，以下是回复建议」、后面
    再补一句「希望有帮助」，json.loads 直接挂。所以先按围栏剥，再退到「第一个 {
    到最后一个 }」。都挖不出来才抛，异常里带原始返回，方便看是谁家的坏毛病。
    """
    s = content.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        i, j = s.find("{"), s.rfind("}")
        if 0 <= i < j:
            try:
                return json.loads(s[i:j + 1])
            except json.JSONDecodeError:
                pass
        raise OutputError(f"JSON 解析失败，原始返回:\n{content[:600]}") from None


def parse_and_validate(content):
    """解析 + 校验；失败一律抛异常（不是 die）。

    外层 call_llm 靠这个异常决定「换下一个 provider」——以前这里是 die()，于是
    备用模型只在网络报错时救场，遇到「200 但 JSON 不合规」这个最常见的坏返回反而
    直接退出，配了备用模型也白配。
    """
    obj = extract_json(content)
    err = None
    if not isinstance(obj, dict):
        err = "顶层不是对象"
    else:
        if not isinstance(obj.get("need"), str) or not obj["need"].strip():
            err = "缺 need（对方此刻可能需要什么）"
        c = obj.get("candidates")
        if not isinstance(c, list) or len(c) != 3:
            err = "candidates 必须是长度 3 的数组"
        else:
            for i, it in enumerate(c):
                if not isinstance(it, dict) or not all(k in it for k in ("text", "style", "score")):
                    err = f"candidates[{i}] 缺 text/style/score"
                    break
                t = it["text"]
                if not isinstance(t, str) or not t.strip():
                    err = f"candidates[{i}].text 为空"
                    break
                if "\n" in t or "\r" in t:
                    err = f"candidates[{i}].text 含换行"
                    break
                # score 得是数字：模型给过 "80%"，面板 Math.round("80%") 会显示 NaN。
                # 数字字符串宽容接受（"80" / "80%"），其它一律判坏。
                sc = it["score"]
                if isinstance(sc, str):
                    try:
                        sc = float(sc.strip().rstrip("%").strip())
                    except ValueError:
                        err = f"candidates[{i}].score 不是数字（{it['score']!r}）"
                        break
                if isinstance(sc, bool) or not isinstance(sc, (int, float)):
                    err = f"candidates[{i}].score 不是数字（{it['score']!r}）"
                    break
                if not (0 <= sc <= 100):
                    err = f"candidates[{i}].score 越界（{it['score']!r}）"
                    break
                it["score"] = round(float(sc))
    if err:
        raise OutputError(f"校验失败: {err}\n原始返回:\n{content[:600]}")
    # tension 是展示用的弱字段：模型没给或给了越界值也不该让整轮生成失败，夹到 1-9
    try:
        t = int(obj.get("tension"))
    except (TypeError, ValueError):
        t = 5
    obj["tension"] = max(1, min(9, t))
    # 面板把第一条当「推荐回复」，所以按分数降序排一遍（模型偶尔不按高到低给）
    try:
        obj["candidates"].sort(key=lambda x: x.get("score") or 0, reverse=True)
    except Exception:
        pass
    return obj





def _last_suggestion(path):
    """已落盘的最后一条建议；读不出来就当没有（去重判断用，坏了不能挡住生成）"""
    try:
        lines = [l for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
        return json.loads(lines[-1]) if lines else None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="根据微信聊天记录生成 3 条候选回复（不发送）")
    ap.add_argument("--session", help="会话名，默认取最后一条对方消息的会话")
    ap.add_argument("--limit", type=int, default=20, help="取最近多少条消息，默认 20")
    ap.add_argument("--dry-run", action="store_true", help="只打印 prompt，不调 API")
    ap.add_argument("--provider", default=LEAD_PROVIDER,
                    help="链首 provider，传 none 走 config.yaml 默认链")
    ap.add_argument("--model", default=LEAD_MODEL, help="链首模型名，传 none 走默认链")
    ap.add_argument("--out", help="结果追加到此文件，默认 store/suggestions.jsonl")
    ap.add_argument("--messages", help="聊天记录文件，默认 store/messages.jsonl")
    ap.add_argument("--budget", type=float, default=BUDGET_SEC,
                    help=f"整条候选链的总时间预算（秒），默认 {BUDGET_SEC:.0f}")
    ap.add_argument("--force", action="store_true",
                    help="同一份记录已经生成过也再来一次（面板手动点「生成」走这个）")
    ap.add_argument("--no-history", action="store_true",
                    help="不给 prompt 加历史库背景（A/B 对照测试用）")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    msgs = load_messages(a.messages)
    session = a.session or pick_session(msgs)     # 规则只此一份，见 pick_session()
    transcript, n, last_fp = build_transcript(msgs, session, a.limit)
    # 历史背景：历史库里的这个会话，只读不回写。渲染表在 render_history.py 一份
    # 收口（撤回留、系统通知滤、泄漏链接占位、quote 不猜原句）。--no-history 关。
    hist = "" if a.no_history else load_history_ctx(session, limit=HISTORY_LIMIT)
    prompt = PROMPT.read_text(encoding="utf-8").rstrip("\n") + "\n\n" + transcript + "\n"
    if hist:
        prompt += ("\n---\n\n以下是同一人的更早聊天历史（供参考语气、称呼和常用梗，"
                   "不是现在这段对话；候选回帖别提历史里已经过八百年的具体事件）：\n\n"
                   + hist + "\n")

    if a.dry_run:
        print(prompt)
        return

    out = Path(a.out) if a.out else SUGGESTIONS
    store = Path(a.messages).parent if a.messages else STORE
    # 跨进程锁 + 「这份记录已经生成过就不再来一遍」。面板自检线程、面板手动生成、
    # watch.py 三路人马以前各跑各的：同一条消息被生成两次，建议库里多一条近重复，
    # 还白烧一次额度。--force 是「我知道生成过了，但我要重来」（面板那个按钮）。
    with GenLock(store):
        if not a.force:
            prev = _last_suggestion(out)
            if prev and prev.get("session") == session and prev.get("source_last_fp") == last_fp:
                print(f"[skip] {session} 这条（fp {last_fp}）已经生成过，不重复烧模型", file=sys.stderr)
                return
        if a.verbose:
            print(f"[verbose] session={session} 消息数={n}", file=sys.stderr)
        override = None
        if a.provider not in ("", "none") and a.model not in ("", "none"):
            override = (a.provider, a.model)
        obj, label, used_model = call_llm(prompt, a.verbose, override, budget=a.budget)
    rec = {
        "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session": session,
        "provider": label,
        "model": used_model,
        "strategy": obj.get("strategy"),
        "intent": obj.get("intent"),
        "need": obj.get("need"),
        "tension": obj.get("tension"),
        "candidates": obj["candidates"],
        "source_msg_count": n,
        "source_last_fp": last_fp,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
