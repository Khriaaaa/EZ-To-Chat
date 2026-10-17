#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把微信导出（wxanalyze/full/*.txt）导入成 eztochat 的历史库 store/history.db

为什么不写进 store/messages.jsonl：
  messages.jsonl 是「实时窗口」，generate.pick_session 取的是「最后一条对方消息的会话」，
  build_transcript 取该会话最后 N 条。把 15 万条历史倒进去，assistant 会挑中一个
  早就没聊的会话给建议，watch 也会把导入当新消息触发一轮生成。
  历史是「背景」，单独一层，按会话查。

用法：
  python3 tools/import_wxexport.py [--src DIR] [--out DB] [--dry-run]
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s([^:]{1,80}):\s?(.*)$")
IDX_RE = re.compile(r"^\d+\.\s(.+?)\s\((wxid_[^)]+|[^)]+@chatroom|[^)]+@openim)\)\s-\s(\d+)\s条\s->\s(.+)$")
HEAD_RE = re.compile(r"^#\s*微信聊天记录\s*-\s*(.+?)\s*$")
# 非个人会话的 wxid 后缀：群聊、企业微信/客服接口。导入时默认剔掉。
SPECIAL_SUFFIXES = ("@chatroom", "@openim")
# 非目标联系人：文件传输助手机器、机器人/自动化通道（带来的历史全是自言自语和脚本输出）。
# 判据恒定 wxid，不按名字 —— 和群聊同一条原则，名字撞车误伤不起。
NON_CHAT_WXIDS = ("filehelper", )
NON_CHAT_SUFFIXES = ("@weclaw", )
CONV_RE = re.compile(r"^#\s*会话:\s*(\S+)", re.M)

# 跟 wxanalyze/parse.py 一致的分类（保真，别自己发明）
KIND_RULES = [("emoji", "[表情]"), ("voice", "[语音]"), ("video", "[视频]"),
              ("image", "[图片]"), ("file", "[文件]"), ("quote", "[引用]"),
              ("link", "[链接]"), ("recall", "撤回了一条消息")]

# kind=text 里其实混进去的「系统通知」——不是任何人说的话，生成时必须滤掉。
# 固定正枚举，宁可漏一个也别错抓普通消息；真名单独留给 sweep 脚本扫。
NOTICE_RULES = [
    ("recall",   "撤回了一条消息"),                # 「"某人" 撤回了一条消息」/「你撤回了…」
    ("notice",   "以上是打招呼的消息"),
    ("notice",   "朋友验证请求"),
    ("notice",   "开启了朋友验证"),
    ("notice",   "你已添加了"),
    ("notice",   "把你加入了通讯录黑名单"),
    ("notice",   "拍了拍"),
    ("notice",   "发出了红包"),
    ("notice",   "领取了你的红包"),
    ("notice",   "向你转账"),
    ("notice",   "收到了你转账"),
    ("notice",   "发起了一个日程"),
    ("notice",   "邀请你加入了群聊"),
    ("notice",   "你已退出群聊"),

    ("notice",   "发出了名片"),
    ("notice",   "发出了位置"),
]

# 链接消息里 14 条泄漏的内部元数据长这样（base64/内部字段，不可读）。
# 规则：正文非空且不含中文、不是 http(s) —— 就当泄漏处理。
_LINK_META_RE = re.compile(r"^[0-9a-fA-F\s:]+$|^(?:[0-9]+\s){2,}")


def looks_like_link_payload(body):
    """[链接] 后面紧跟的是不可读内部字段（内部数字字段 / md5 / base64）？

    可读判据：只要含有 CJK 就不是泄漏；含 http(s) 也是正经链接。
    剩下的「全部由数字/十六进制/等号/斜杠/空白组成且确实带数字」归为泄漏。
    """
    rest = body[len("[链接]"):].strip()
    if not rest:
        return False
    if rest.startswith(("http://", "https://")):
        return False          # 可读 URL 不算泄漏
    if re.search(r"[\u4e00-\u9fff]", rest):
        return False          # 有中文 = 人写的说明文字，不是内部字段
    if not re.search(r"[0-9a-fA-F]", rest):
        return False
    if re.fullmatch(r"[A-Za-z0-9+/=\s:.\-]+", rest):
        return True
    return False


def reclassify(body, kind):
    """kind=text 里混进去的撤回/系统通知，改判 kind（只改 kind 不删行）。"""
    if kind != "text":
        return kind
    for k, p in NOTICE_RULES:
        if p in body:
            return k
    return kind


def kind_and_text(body):
    """classify 的完整一步：前缀分类 + 泄漏检测 + 系统通知改判。

    泄漏的链接消息 kind 改成 'link_meta'（内部字段，模型不该看，界面灰提示）。
    """
    kind, _ = classify(body)
    if kind == "link" and looks_like_link_payload(body):
        # 泄漏面统一占位：原 payload 不落库（面板/文档/上下文任何一处展示 text 都会漏）
        return "link_meta", "[链接]（内容不可读：内部字段）"
    kind = reclassify(body, kind)
    return kind, body


def classify(body):
    for kind, p in KIND_RULES:
        if body.startswith(p):
            return kind, body[len(p):].strip()
    if re.fullmatch(r"(\[[^\]]{1,8}\])+", body):
        return "emoji", body
    return "text", body


def read_index(src):
    """00_索引.txt -> {文件名: {name, wxid, n}}"""
    idx = {}
    p = os.path.join(src, "00_索引.txt")
    if not os.path.exists(p):
        return idx
    for ln in open(p, encoding="utf-8-sig", errors="replace").read().splitlines():
        m = IDX_RE.match(ln.strip())
        if m:
            idx[m.group(4).strip()] = {"name": m.group(1).strip(),
                                       "wxid": m.group(2), "n": int(m.group(3))}
    return idx


def parse_file(path, session):
    """一条消息一行，续行并入上一条（对齐 wxanalyze/parse.py 的解析）"""
    rows, cur = [], None
    for ln in open(path, encoding="utf-8-sig", errors="replace").read().splitlines():
        ln = ln.rstrip("\r")
        if not ln.strip() or ln.startswith("#") or ln.startswith("==="):
            continue
        m = LINE_RE.match(ln)
        if m:
            if cur:
                rows.append(cur)
            who = m.group(2).strip()
            kind, body = kind_and_text(m.group(3))
            cur = {"who": who, "sender": "me" if who == "我" else "them",
                   "kind": kind, "text": body, "dt": m.group(1),
                   "logical": session}
        elif cur is not None:
            cur["text"] += "\n" + ln
    if cur:
        rows.append(cur)
    return rows


def _secs(dt):
    return datetime.strptime(dt, "%Y-%m-%d %H:%M:%S").timestamp()


def fold_runs(rows, gap=60):
    """把紧挨着的、同一个人的同样内容折成一条，dup_count=N，dt 取第一条。

    只为上下文好读：18 张 [图片] 一次发、[裂开] 连点十几下，铺开就是噪声。
    超过 gap 秒的同内容算两次独立发言，不折（不然「ok」上午下午各一次会被并成 ×2）。
    展开后条数不变（sum(dup_count) == 原始条数），原文一字不动。
    """
    out = []
    for r in rows:
        if out:
            p = out[-1]
            if (p["who"] == r["who"] and p["text"] == r["text"]
                    and _secs(r["dt"]) - _secs(p["_last"]) <= gap):
                p["dup_count"] += 1
                p["_last"] = r["dt"]
                continue
        r = dict(r)
        r["dup_count"], r["_last"] = 1, r["dt"]
        out.append(r)
    for r in out:
        r.pop("_last", None)
    return out


def fp_of(session, dt, who, text, occ):
    """occ = 同一(会话,时间,说话人,文本)的第几次出现。

    必须带上它：同一秒里连发两条一样的内容（「撤回了一条消息」「ok」）很常见，
    只用 session|dt|who|text 会在 UNIQUE 上把这些真消息当重复丢掉（实测少 722 条）。
    采集器侧的 fingerprint(session, text, occ) 是同一个思路。
    """
    raw = "\x1f".join([session, dt, who, text, str(occ)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/opt/data/shared/wxanalyze/full")
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "store", "history.db"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-special", action="store_true",
                    help="也导入群聊/企业微信。默认只留个人会话")
    a = ap.parse_args()

    src, out = a.src, a.out
    for f in (sys.stdout, sys.stderr):
        f.reconfigure(encoding="utf-8", errors="replace")

    index = read_index(src)
    files = sorted(f for f in os.listdir(src) if f.endswith(".txt") and f != "00_索引.txt")
    print(f"源 {src}\n  索引 {len(index)} 个会话，{len(files)} 个 txt")

    # 先在 .building 上重建，成功才原子替换（评审 B9）：中途失败/断电只会留下
    # 一个临时库，原来的 history.db 一点没动 —— 旧实现先删再建，失败就没历史了。
    build = out + ".building"
    if not a.dry_run:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        for p in (build, build + "-wal", build + "-shm"):
            if os.path.exists(p):
                os.remove(p)          # 上一轮崩了留下的半成品
        con = sqlite3.connect(build)
        con.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE messages(
            id INTEGER PRIMARY KEY,
            session TEXT NOT NULL,    -- 会话名（和采集器 session 对齐）
            wxid TEXT NOT NULL DEFAULT '',  -- 导出里的会话 id，撞名时用来认人
            seq INTEGER NOT NULL,     -- 全局时序
            dt TEXT NOT NULL,         -- 本地时间 YYYY-MM-DD HH:MM:SS
            ts INTEGER NOT NULL,      -- UTC epoch
            ts_utc TEXT NOT NULL,
            sender TEXT NOT NULL,     -- me / them
            who TEXT NOT NULL,        -- 导出里的原始说话人（群里有用）
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            dup_count INTEGER NOT NULL DEFAULT 1,  -- 紧挨着连发的同样内容折成几条
            fp TEXT NOT NULL UNIQUE
          );
          CREATE INDEX idx_sess_ts ON messages(session, ts);
          CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
        """)

    total, skipped, seq = 0, [], 0
    per = {}
    ins = []
    seen_occ = {}

    # 先给每个文件定 (会话名, wxid)。导出 300 个文件只有 299 个会话名 —— 有一个名字
    # 被两个不同的人共用（朋友 / wxid_07le6z… + wxid_vw7x74…），不问 wxid 就并成一个会话。
    plan = {}
    for fn in files:
        p = os.path.join(src, fn)
        info = index.get(fn)
        if info:
            plan[fn] = (info["name"], info["wxid"])
        else:
            txt = open(p, encoding="utf-8-sig", errors="replace").read(400)
            mh = HEAD_RE.search(txt)
            mc = CONV_RE.search(txt)
            plan[fn] = ((mh.group(1) if mh else os.path.splitext(fn)[0]),
                        (mc.group(1) if mc else ""))

    # 群聊与企业微信一律不导（eztochat 只做个人会话）。判据用 wxid 后缀 ——
    # 56 个群全是 @chatroom、20 个企微接口会话全是 @openim，而名字里带「群」的
    # （Fantasy 同学群）其实是个人号，绝不能按名字删。
    special = []
    if not a.include_special:
        special = [fn for fn in files
                   if plan[fn][1].endswith(SPECIAL_SUFFIXES + NON_CHAT_SUFFIXES)
                   or plan[fn][1] in NON_CHAT_WXIDS]
        drop = set(special)
        files = [fn for fn in files if fn not in drop]
        for fn in special:
            plan.pop(fn, None)

    by_name = {}
    for fn, (nm, _) in plan.items():
        by_name.setdefault(nm, []).append(fn)
    for nm, fns in by_name.items():
        if len({plan[f][1] for f in fns}) > 1:
            for f in sorted(fns)[1:]:
                plan[f] = (f"{nm}（{plan[f][1]}）", plan[f][1])
            skipped.append(f"会话名 {nm!r} 被多个 wxid 共用，已按 wxid 拆开：{sorted(fns)}")

    for fn in files:
        session, wxid = plan[fn]
        info = index.get(fn)
        rows = parse_file(os.path.join(src, fn), session)
        if not rows:
            skipped.append(fn)
            continue
        if info and info["n"] != len(rows):
            skipped.append(f"{fn} 条数不符 声明{info['n']} 实得{len(rows)}")
        rows = fold_runs(rows)
        for r in rows:
            seq += 1
            dt = datetime.strptime(r["dt"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
            ts = int(dt.timestamp())
            text = "[链接]" if r["kind"] == "link" and not r["text"][3:].strip() else r["text"]
            k = (session, r["dt"], r["who"], r["text"])
            occ = seen_occ.get(k, 0)
            seen_occ[k] = occ + 1
            ins.append((session, wxid, seq, r["dt"], ts,
                        dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        r["sender"], r["who"], r["kind"], text, r["dup_count"],
                        fp_of(session, r["dt"], r["who"], r["text"], occ)))
            per[session] = per.get(session, 0) + r["dup_count"]
        total += len(rows)
        if not a.dry_run and len(ins) >= 20000:
            con.executemany("INSERT OR IGNORE INTO messages"
                            "(session,wxid,seq,dt,ts,ts_utc,sender,who,kind,text,dup_count,fp)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ins)
            ins = []

    if not a.dry_run:
        if ins:
            con.executemany("INSERT OR IGNORE INTO messages"
                            "(session,wxid,seq,dt,ts,ts_utc,sender,who,kind,text,dup_count,fp)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ins)
        con.executemany("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                        [("source", src), ("imported_at", datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S %z")),
                         ("sessions", str(len(per))), ("messages", str(total)),
                         ("folded", "runs<=60s"),
                         ("special_excluded", str(len(special)))])
        con.commit()
        rows = con.execute("select count(*) from messages").fetchone()[0]
        n = con.execute("select sum(dup_count) from messages").fetchone()[0]
        nm = con.execute("select sum(dup_count) from messages where sender='me'").fetchone()[0]
        run = con.execute("select count(*) from messages where dup_count>1").fetchone()[0]
        rng = con.execute("select min(ts_utc), max(ts_utc) from messages").fetchone()
        size_mb = os.path.getsize(build) / 1048576
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        con.close()
        os.replace(build, out)        # 到这一步旧库才被换掉
        print(f"\n入库 {rows:,} 行（折叠前 {n:,} 条，我 {nm:,} / 对方 {n - nm:,}），"
              f"{len(per)} 个会话")
        print(f"  折叠了 {run:,} 组连发，省下 {n - rows:,} 行")
        print(f"  时间跨度 {rng[0]} ~ {rng[1]}")
        print(f"  大小 {size_mb:.1f} MB -> {out}")
        if special:
            print(f"  剔除群聊/企微 {len(special)} 个会话"
                  f"（eztochat 只做个人会话；要留加 --include-special）")
    else:
        print(f"\n[dry-run] 解析出 {total:,} 条，{len(per)} 个会话")

    if skipped:
        print(f"\n跳过/异常 {len(skipped)} 个：")
        for s in skipped[:10]:
            print("   ", s)
    # 跨会话重名检查（重名会把两个会话合并）
    names = [n for n in per]
    if len(names) != len(set(names)):
        print("⚠️ 有重名会话")
    return 0


if __name__ == "__main__":
    sys.exit(main())
