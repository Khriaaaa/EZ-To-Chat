#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导入结果的验收脚本：`tools/import_wxexport.py` 跑完之后拿它去对账。

    python3 tools/check_history.py [--db store/history.db] [--csv <chats.csv>]

对三件事：
  1. 展开后的总条数 / 「我」的条数，跟 chats.csv 声明的对不对
  2. 逐个会话对（先按 wxid，csv 里 wxid 为空的行退回按会话名）
  3. 会话名 / wxid 是不是一一对应（撞名会把两个会话并成一个）

注意 db 里行数是**折叠后**的：紧挨着连发的同样内容合成一行（dup_count=N），
所以 count(*) < 条数，要用 sum(dup_count) 才是原始条数。
"""
import argparse
import csv
import os
import sqlite3
import sys

DEFAULT_DB = "store/history.db"
DEFAULT_CSV = "/opt/data/shared/wxanalyze/report/chats.csv"
# 非个人会话（导入时默认剔掉）：wxid 后缀/完整 wxid -> 中文名
SPECIAL = (("@chatroom", "群聊"), ("@openim", "企业微信"))
# 传输助手/机器人通道：csv 里 wxid 为空只能按名字认，凡 db 里也没有的就当作已剔
NON_CHAT_NAMES = ("文件传输助手", "微信ClawBot")

for _f in (sys.stdout, sys.stderr):
    _f.reconfigure(encoding="utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--csv", default=DEFAULT_CSV)
    a = ap.parse_args()

    if not os.path.exists(a.db):
        print(f"✗ 没有 {a.db}，先跑 tools/import_wxexport.py")
        return 2
    con = sqlite3.connect(a.db)
    bad = 0

    rows = list(csv.DictReader(open(a.csv, encoding="utf-8-sig"))) if os.path.exists(a.csv) else []

    # 导入时默认剔了群聊与企微（eztochat 只做个人会话），chats.csv 里却含着 ——
    # 对账要同口径：库里没有哪一类，csv 里就跟着剔哪一类。
    db_special = {}
    notes = []
    for suf, label in SPECIAL:
        n = con.execute("select count(distinct session) from messages"
                        " where wxid like ?", ("%" + suf,)).fetchone()[0]
        db_special[suf] = n
        if rows and not n:
            grp = [r for r in rows if r["wxid"].strip().endswith(suf)]
            tot = sum(int(r["条数"]) for r in grp if r["条数"].isdigit())
            rows = [r for r in rows if not r["wxid"].strip().endswith(suf)]
            if grp:
                notes.append(f"  （db 已剔{label}，这里同口径剔掉 {len(grp)} 个会话 / {tot:,} 条）")
    # 传输助手/机器人通道在 csv 里 wxid 为空，只能按名字剔（改判后的 NON_CHAT_NAMES）
    nc = [r for r in rows if not r["wxid"].strip() and r["会话"] in NON_CHAT_NAMES
          and con.execute("select count(*) from messages where session=?", (r["会话"],)).fetchone()[0] == 0]
    if nc:
        rows = [r for r in rows if r not in nc]
        notes.append(f"  （db 已剔传输助手/机器人通道，这里同口径剔掉 {len(nc)} 个会话 / "
                     f"{sum(int(r['条数']) for r in nc if r['条数'].isdigit()):,} 条）")

    db_rows, db_n, db_me = con.execute(
        "select count(*), sum(dup_count),"
        " coalesce(sum(dup_count) filter(where sender='me'),0) from messages").fetchone()

    print("== 总量 ==")
    if rows:
        csv_tot = sum(int(r["条数"]) for r in rows if r["条数"].isdigit())
        csv_me = sum(int(r["我"]) for r in rows if r["我"].isdigit())
        print(f"  chats.csv   声明 {csv_tot:,} 条 / 我 {csv_me:,}")
        print(f"  history.db  实得 {db_n:,} 条 / 我 {db_me:,} / 对方 {db_n - db_me:,}")
        print(f"  条数差 {db_n - csv_tot:+d}，我差 {db_me - csv_me:+d}"
              f"   （折成 {db_rows:,} 行，省 {db_n - db_rows:,} 行）")
        for n_ in notes:
            print(n_)
        bad += db_n != csv_tot or db_me != csv_me
    else:
        print(f"  history.db  {db_n:,} 条 / 我 {db_me:,}  →  {db_rows:,} 行")
        print(f"  （没找到 {a.csv}，跳过表格对账）")

    if rows:
        print("\n== 逐会话对账 ==")
        by_wxid = {w: (n, me) for w, n, me in con.execute(
            "select wxid, sum(dup_count),"
            " coalesce(sum(dup_count) filter(where sender='me'),0)"
            " from messages group by wxid")}
        by_name = {s: (n, me) for s, n, me in con.execute(
            "select session, sum(dup_count),"
            " coalesce(sum(dup_count) filter(where sender='me'),0)"
            " from messages group by session")}
        miss, diff, ok, byname = [], [], 0, 0
        for r in rows:
            wid, n, me = r["wxid"].strip(), r["条数"], r["我"]
            if not n.isdigit():
                continue
            if wid:
                got = by_wxid.get(wid)
            else:
                got = by_name.get(r["会话"])   # csv 里 wxid 为空的行，退回按会话名对
                byname += bool(got)
            if got is None:
                if not wid and r["会话"] in NON_CHAT_NAMES:
                    continue   # 传输助手/机器人通道，导入时连同特殊会话一起剔了，正常
                miss.append((r["会话"], wid or "(csv 无 wxid)", n))
            elif str(got[0]) != n or str(got[1]) != me:
                diff.append((r["会话"], wid, f"csv {n}/{me}", f"db {got[0]}/{got[1]}"))
            else:
                ok += 1
        print(f"  csv {len(rows)} 行，对上 {ok} 行（按会话名对上的 {byname} 行）")
        for label, items in (("库中缺失", miss), ("条数对不上", diff)):
            print(f"  {label}: {len(items)}")
            for it in items[:6]:
                print("     ", it)
        bad += len(miss) + len(diff)

    print("\n== 特殊会话（群聊 / 企业微信）==")
    for suf, label in SPECIAL:
        print(f"  {label:6s} db 里 {db_special[suf]} 个会话（0 = 已按设置剔除）")

    print("\n== 会话身份 ==")
    ns, nw = con.execute("select count(distinct session), count(distinct wxid) from messages").fetchone()
    empty = con.execute("select count(*) from messages where wxid=''").fetchone()[0]
    print(f"  会话 {ns} 个 / wxid {nw} 个 / 空 wxid 的行 {empty}")
    if ns != nw or empty:
        print("  ⚠️ 会话名与 wxid 不是一一对应（可能有两个会话被并成一个）")
        bad += 1

    print("\n" + ("✅ 全部对上" if not bad else "⚠️ 有出入，见上"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
