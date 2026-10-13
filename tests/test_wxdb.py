#!/usr/bin/env python3
"""本地库直读（SQLCipher 4）离线自检。

只用标准库跑（跟 tests/test_review_fixes.py 一个路子），不碰真实微信目录、不连网、
不需要 Windows。三层验证，越往下越硬：

  1. AES 层：NIST 已知答案 + 自造加密器往返（加密器与解密器是两段独立代码）；
  2. 页格式层：SQLCipher 4 的页变换必须是可逆的（随机页字节，逐页比对）；
  3. 金标准层：用**真正的 SQLCipher**（sqlcipher3-binary）造库，再用我们的解密器
     解开、用标准库 sqlite3 查行 —— 对的是参考实现，不是自己对自己。

第 3 层需要装了 sqlcipher3 的解释器：本环境直接在 venv 里跑本测试，或用
WXREPLY_SQLCIPHER_PY 指过去（没装就跳过，前三层照跑）。

跑法：/opt/hermes/.venv/bin/python tests/test_wxdb.py
"""
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "collector"))

import readmode                                              # noqa: E402
import wxaes                                                 # noqa: E402
import wxdb                                                 # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("%s %s%s" % ("✅" if ok else "❌", name,
                       ("  " + str(detail)) if detail and not ok else ""))


def skip(name, why):
    SKIP.append(name)
    print("… %s（跳过：%s）" % (name, why))


# ---------------------------------------------------------------------------
# 测试用 AES 加密器（独立实现，只与 wxaes 共用 S 盒与乘法表）
# ---------------------------------------------------------------------------
def _expand_enc_key(key):
    nk, nr = 8, 14
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [wxaes._SBOX[b] for b in t]
            t[0] ^= rcon
            rcon = wxaes._xtime(rcon)
        elif i % nk == 4:
            t = [wxaes._SBOX[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return w


def _mul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        a = wxaes._xtime(a)
        b >>= 1
    return p


def _encrypt_block(w, block):
    s = [[block[4 * c + r] for c in range(4)] for r in range(4)]

    def ark(rnd):
        for c in range(4):
            for r in range(4):
                s[r][c] ^= w[4 * rnd + c][r]

    ark(0)
    for rnd in range(1, 14):
        s = [[wxaes._SBOX[s[r][c]] for c in range(4)] for r in range(4)]
        for r in range(1, 4):
            s[r] = s[r][r:] + s[r][:r]
        ns = [[0] * 4 for _ in range(4)]
        for c in range(4):
            a0, a1, a2, a3 = s[0][c], s[1][c], s[2][c], s[3][c]
            ns[0][c] = _mul(a0, 2) ^ _mul(a1, 3) ^ a2 ^ a3
            ns[1][c] = a0 ^ _mul(a1, 2) ^ _mul(a2, 3) ^ a3
            ns[2][c] = a0 ^ a1 ^ _mul(a2, 2) ^ _mul(a3, 3)
            ns[3][c] = _mul(a0, 3) ^ a1 ^ a2 ^ _mul(a3, 2)
        s = ns
        ark(rnd)
    s = [[wxaes._SBOX[s[r][c]] for c in range(4)] for r in range(4)]
    for r in range(1, 4):
        s[r] = s[r][r:] + s[r][:r]
    ark(14)
    return bytes(s[r][c] for c in range(4) for r in range(4))


def aes_cbc_encrypt(key, iv, data):
    assert len(data) % 16 == 0
    w = _expand_enc_key(key)
    out, prev = bytearray(), iv
    for off in range(0, len(data), 16):
        blk = bytes(a ^ b for a, b in zip(data[off:off + 16], prev))
        prev = _encrypt_block(w, blk)
        out += prev
    return bytes(out)


def sqlcipher_page(plain, pgno, key, salt):
    """把一页明文按 SQLCipher 4 布局加密（页 1 头 16 字节放 salt）。"""
    page_sz = wxdb.PAGE_SZ
    iv = bytes((pgno * 37 + j) % 256 for j in range(16))
    body = plain[16:page_sz - wxdb.RESERVE_SZ] if pgno == 1 else \
        plain[0:page_sz - wxdb.RESERVE_SZ]
    ct = aes_cbc_encrypt(key, iv, body)
    page = bytearray(page_sz)
    if pgno == 1:
        page[0:16] = salt            # 第 1 页头 16 字节是 salt，密文从 16 起
        page[16:16 + len(ct)] = ct
    else:
        page[0:len(ct)] = ct         # 其余页密文从 0 起（第 1 页那 16 字节不对应）
    page[page_sz - wxdb.RESERVE_SZ:page_sz - wxdb.RESERVE_SZ + 16] = iv
    start = 16 if pgno == 1 else 0     # 与真 SQLCipher 一致（实测得出）
    mac = hmac_new(wxdb._mac_key(key, salt),
                   bytes(page[start:page_sz - wxdb.RESERVE_SZ + 16]), pgno)
    page[page_sz - 64:page_sz] = mac
    return bytes(page)


def hmac_new(mac_key, data, pgno):
    import hmac as _h
    m = _h.new(mac_key, data, hashlib.sha512)
    m.update(struct.pack("<I", pgno))
    return m.digest()


# ---------------------------------------------------------------------------
# 金标准：真正的 SQLCipher（有就上，没有就跳）
# ---------------------------------------------------------------------------
GOLD_SNIPPET = r'''
import sqlcipher3, sys, json
path, keyhex, sql = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
c = sqlcipher3.connect(path)
c.execute("PRAGMA key = \"x'%s'\"" % keyhex)
c.execute("PRAGMA cipher_page_size = 4096")
c.execute("PRAGMA kdf_iter = 256000")
c.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512")
c.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")
for stmt in sql:
    c.execute(stmt)
c.commit()
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
c.close()
'''


def gold_python():
    """返回一个能 import sqlcipher3 的解释器路径；没有返回 None。"""
    try:
        import sqlcipher3                                       # noqa: F401
        return sys.executable
    except ImportError:
        pass
    for cand in (os.environ.get("WXREPLY_SQLCIPHER_PY"),):
        if cand and os.path.exists(cand):
            try:
                out = subprocess.run([cand, "-c", "import sqlcipher3; print('ok')"],
                                     capture_output=True, text=True, timeout=60)
                if "ok" in out.stdout:
                    return cand
            except (OSError, subprocess.SubprocessError):
                continue
    return None


def make_gold_db(py, path, key, statements):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    r = subprocess.run([py, "-c", GOLD_SNIPPET, path, key.hex(),
                        json.dumps(statements)], capture_output=True, text=True,
                       timeout=180)
    if r.returncode != 0:
        raise RuntimeError("造金标准库失败：%s" % (r.stderr or r.stdout))


KEY = bytes(range(32))


def gold_layout(tmp, py):
    """按微信的库结构造一套真 SQLCipher 库，返回 (账号目录, Msg_ 表名)。

    账号目录名必须等于自己的 wxid —— 真实环境就是这样，采集器靠目录名认
    "哪条是自己发的"。
    """
    acc = os.path.join(tmp, "wxid_me")
    for sub in ("", "session", "message"):
        os.makedirs(os.path.join(acc, "db_storage", sub), exist_ok=True)
    d = os.path.join(acc, "db_storage")
    table = "Msg_" + hashlib.md5(b"wxid_friend").hexdigest()
    make_gold_db(py, os.path.join(d, "contact.db"), KEY, [
        "CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT)",
        "INSERT INTO contact VALUES ('wxid_me','我自己','我')",
        "INSERT INTO contact VALUES ('wxid_friend','小林','小林同学')",
        "INSERT INTO contact VALUES ('filehelper','文件传输助手','文件传输助手')"])
    make_gold_db(py, os.path.join(d, "session", "session.db"), KEY, [
        "CREATE TABLE SessionTable(username TEXT, unread_count INT, summary TEXT, "
        "last_timestamp INT, is_hidden INT)",
        "INSERT INTO SessionTable VALUES ('wxid_friend',2,'在吗',1760000300,0)"])
    make_gold_db(py, os.path.join(d, "message", "message_resource.db"), KEY, [
        "CREATE TABLE SenderName2Id(rowid INTEGER PRIMARY KEY, user_name TEXT)",
        "INSERT INTO SenderName2Id VALUES (1,'wxid_friend')",
        "INSERT INTO SenderName2Id VALUES (2,'wxid_me')"])
    make_gold_db(py, os.path.join(d, "message", "message_0.db"), KEY, [
        "CREATE TABLE %s(local_id INTEGER PRIMARY KEY, local_type INT, "
        "real_sender_id INT, create_time INT, message_content BLOB, sort_seq INT)" % table,
        "CREATE TABLE Name2Id(rowid INTEGER PRIMARY KEY, user_name TEXT)",
        "INSERT INTO Name2Id VALUES (2,'wxid_me')",
        "INSERT INTO %s VALUES (1,1,1,1760000100,'在吗',100)" % table,
        "INSERT INTO %s VALUES (2,1,2,1760000200,'在的',101)" % table,
        "INSERT INTO %s VALUES (3,3,1,1760000300,zeroblob(8),102)" % table])
    return acc, table


# ---------------------------------------------------------------------------
# 1. AES 层
# ---------------------------------------------------------------------------
def test_aes_kat():
    key, iv, ct, pt = wxaes._selftest_vectors()
    check("AES-256-CBC 对 NIST 已知答案（纯 Python）",
          wxaes._pure_cbc_decrypt(key, iv, ct) == pt)
    wxaes._OSSL = wxaes._load_openssl()
    if wxaes._OSSL is not None:
        check("AES-256-CBC 对 NIST 已知答案（libcrypto 快路）",
              wxaes._openssl_cbc_decrypt(key, iv, ct) == pt)
    else:
        skip("libcrypto 快路已知答案", "本机没有 libcrypto")
    st = wxaes.status()
    check("AES 后端自检有结论", st["backend"] in ("pure", "openssl", "bcrypt"), st)


def test_aes_roundtrip():
    import random
    random.seed(11)
    ok = True
    for n in (16, 4000, 4016, 4096):
        pt = bytes(random.getrandbits(8) for _ in range(n))
        key = bytes(random.getrandbits(8) for _ in range(32))
        iv = bytes(random.getrandbits(8) for _ in range(16))
        ct = aes_cbc_encrypt(key, iv, pt)
        if wxaes.cbc_decrypt(key, iv, ct) != pt or \
                wxaes._pure_cbc_decrypt(key, iv, ct) != pt:
            ok = False
            print("   长度 %d 不一致" % n)
    check("自造加密器 ↔ 解密器往返一致（快路与纯 Python 各一遍）", ok)


# ---------------------------------------------------------------------------
# 2. SQLCipher 页格式
# ---------------------------------------------------------------------------
def test_page_inverse():
    """页变换必须是可逆的：加密一页 → 解密回同一页（含第 1 页特殊头）。"""
    import random
    random.seed(5)
    salt = bytes(range(16))
    ok1 = ok2 = True
    for _ in range(3):
        plain = bytes(random.getrandbits(8) for _ in range(wxdb.PAGE_SZ))
        p1 = sqlcipher_page(plain, 1, KEY, salt)
        got1 = wxdb.decrypt_page(KEY, p1, 1)
        # 第 1 页：头 16 字节换成 SQLite 明文头；正文一致；
        # 尾部 reserve 段按设计补 0（原明文那段是随机字节，两边本来就不同）
        if (got1[16:wxdb.PAGE_SZ - wxdb.RESERVE_SZ]
                != plain[16:wxdb.PAGE_SZ - wxdb.RESERVE_SZ]
                or got1[:16] != b"SQLite format 3\x00"):
            ok1 = False
        p2 = sqlcipher_page(plain, 2, KEY, salt)
        got2 = wxdb.decrypt_page(KEY, p2, 2)
        if got2[:wxdb.PAGE_SZ - wxdb.RESERVE_SZ] != plain[:wxdb.PAGE_SZ - wxdb.RESERVE_SZ]:
            ok2 = False
    check("第 1 页解密正确（明文头 + 内容）", ok1)
    check("第 2 页解密正确（整页）", ok2)
    p1 = sqlcipher_page(bytes(wxdb.PAGE_SZ), 1, KEY, salt)
    check("HMAC 认对的钥匙（页 1）", wxdb.verify_key(KEY, p1))
    check("HMAC 挡错的钥匙", not wxdb.verify_key(bytes(32), p1))
    check("HMAC 挡只差一位的钥匙",
          not wxdb.verify_key(bytes([1]) + KEY[1:], p1))
    check("非第 1 页不给 salt 时必须拒绝（不能拿密文当头当 salt）",
          not wxdb.page_ok(KEY, sqlcipher_page(bytes(wxdb.PAGE_SZ), 2, KEY, salt), 2))


def test_wal_mechanics(tmp):
    """WAL 帧合并：salt 对得上才并，对不上就不并。"""
    salt = bytes(range(16))
    src = os.path.join(tmp, "m.db")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    page1 = sqlcipher_page(b"P" * wxdb.PAGE_SZ, 1, KEY, salt)
    page2 = sqlcipher_page(b"A" * wxdb.PAGE_SZ, 2, KEY, salt)
    with open(src, "wb") as f:
        f.write(page1 + page2)

    new_page2 = sqlcipher_page(b"B" * wxdb.PAGE_SZ, 2, KEY, salt)
    h_salt = bytes([0xAA] * 8)

    def write_wal(path, frame_salt):
        with open(path, "wb") as f:
            # WAL 头 32 字节：magic(4) + 版本(4) + 页大小(4) + checkpoint 序号(4)
            #                + salt1(4) + salt2(4) + 校验1(4) + 校验2(4)
            f.write(b"\x37\x7f\x06\x82" +
                    struct.pack("<III", 3007000, wxdb.PAGE_SZ, 1) +
                    h_salt + b"\x00" * 8)
            f.write(struct.pack(">II", 2, 2) + frame_salt + b"\x00" * 8)
            f.write(new_page2)

    write_wal(src + "-wal", h_salt)
    dst = os.path.join(tmp, "m.plain.db")
    n, bad, merged = wxdb.decrypt_db(src, KEY, dst)
    got = open(dst, "rb").read()
    check("WAL 帧被合并进明文库（第二页换成了新内容）",
          merged == 1 and got[wxdb.PAGE_SZ:wxdb.PAGE_SZ + 32] == b"B" * 32,
          "merged=%d" % merged)
    check("主库页数/坏页数正常", n == 2 and bad == 0, "n=%d bad=%d" % (n, bad))

    write_wal(src + "-wal", bytes([0xBB] * 8))       # salt 不一致：属于旧周期
    dst2 = os.path.join(tmp, "m2.plain.db")
    n2, _, merged2 = wxdb.decrypt_db(src, KEY, dst2)
    check("salt 对不上的 WAL 帧不合并", merged2 == 0, "merged=%d" % merged2)
    check("不合并时原页保持不动",
          open(dst2, "rb").read()[wxdb.PAGE_SZ:wxdb.PAGE_SZ + 32] == b"A" * 32)


# ---------------------------------------------------------------------------
# 3. 金标准：真 SQLCipher 库 → 我们的解密器 → 标准库 sqlite3
# ---------------------------------------------------------------------------
def test_gold_decrypt(tmp, py):
    acc, table = gold_layout(tmp, py)
    files = wxdb.db_files(acc)
    check("库文件定位（四类都认）",
          set(files) == {"contact.db", "session/session.db",
                         "message/message_0.db", "message/message_resource.db"},
          sorted(files))
    page1 = open(files["contact.db"], "rb").read(wxdb.PAGE_SZ)
    check("HMAC 认真 SQLCipher 造的钥匙", wxdb.verify_key(KEY, page1))
    dst = os.path.join(tmp, "contact.plain.db")
    n, bad, merged = wxdb.decrypt_db(files["contact.db"], KEY, dst)
    check("真库解密无坏页", bad == 0 and n >= 1, "n=%d bad=%d" % (n, bad))
    check("页 1 明文头正确",
          open(dst, "rb").read(16) == b"SQLite format 3\x00")
    c = sqlite3.connect(dst)
    got = c.execute("SELECT remark FROM contact WHERE username='wxid_friend'").fetchone()
    c.close()
    check("解密结果标准库 sqlite3 能查", got and got[0] == "小林同学", str(got))


def test_reader_gold(tmp, py):
    acc, table = gold_layout(tmp, py)
    files = wxdb.db_files(acc)
    cache = os.path.join(tmp, "cache", "db")
    os.makedirs(cache, exist_ok=True)
    with open(os.path.join(cache, ".keys.json"), "w", encoding="utf-8") as f:
        json.dump({rel: {"key": KEY.hex()} for rel in files}, f)
    r = wxdb.Reader(acc, cache, log=lambda *_: None)
    check("Reader 从缓存拿齐密钥", len(r.keys) == len(files),
          "%d/%d" % (len(r.keys), len(files)))
    sessions = r.session_list(limit=10)
    check("会话表读得到，且带上备注名",
          sessions and sessions[0]["username"] == "wxid_friend"
          and sessions[0]["name"] == "小林同学", str(sessions[:1]))
    msgs = r.messages("wxid_friend", limit=10)
    check("消息读得到三条", len(msgs) == 3, str([m["text"] for m in msgs]))
    if len(msgs) == 3:
        check("发送方判对（对方/自己/对方）",
              [m["sender"] for m in msgs] == ["them", "me", "them"],
              str([(m["text"], m["sender"]) for m in msgs]))
        check("图片消息给占位符而不是二进制噪声",
              msgs[2]["text"] == "[图片]", repr(msgs[2]["text"]))
        check("时间戳带出来了", bool(msgs[0]["ts_utc"]), str(msgs[0].get("ts_utc")))
    r.close()


def test_collect_gold(tmp, py):
    """整套 collect：首次只定锚点，来新消息才写出去。"""
    acc, table = gold_layout(tmp, py)
    files = wxdb.db_files(acc)
    store = os.path.join(tmp, "store")
    cache = os.path.join(store, "cache", "db")
    os.makedirs(cache, exist_ok=True)
    with open(os.path.join(cache, ".keys.json"), "w", encoding="utf-8") as f:
        json.dump({rel: {"key": KEY.hex()} for rel in files}, f)
    old = os.environ.get("WXREPLY_WECHAT_DIR")
    os.environ["WXREPLY_WECHAT_DIR"] = tmp
    try:
        rows, info = wxdb.collect(store, log=lambda *_: None)
        check("首次不往 messages.jsonl 倒灌历史", rows == [], str(rows))
        rows2, info2 = wxdb.collect(store, log=lambda *_: None)
        check("连读两次：第二次按最小间隔跳过（不重复开库）",
              rows2 == [] and info2.get("skipped") == "interval", str(info2))
        check("首次把锚点定在最新一条",
              wxdb.load_anchors(store).get("wxid_friend", {}).get("local_id") == 3,
              str(wxdb.load_anchors(store)))
        # 来一条新消息
        r = wxdb.Reader(acc, cache)
        r.close()
        make_gold_db(py, files["message/message_0.db"], KEY, [
            "CREATE TABLE %s(local_id INTEGER PRIMARY KEY, local_type INT, "
            "real_sender_id INT, create_time INT, message_content BLOB, "
            "sort_seq INT)" % table,
            "INSERT INTO %s VALUES (1,1,1,1760000100,'在吗',100)" % table,
            "INSERT INTO %s VALUES (2,1,2,1760000200,'在的',101)" % table,
            "INSERT INTO %s VALUES (3,3,1,1760000300,zeroblob(8),102)" % table,
            "INSERT INTO %s VALUES (4,1,1,1760000600,'又来了',103)" % table])
        rows, info = wxdb.collect(store, min_interval=0, log=lambda *_: None)
        check("新消息被读出来（而且只有新的那条）",
              [m["text"] for m in rows] == ["又来了"],
              str([(m["text"], m["sender"]) for m in rows]))
        if rows:
            check("新行 sender 是对方", rows[0]["sender"] == "them", rows[0]["sender"])
        check("锚点推进到新行",
              wxdb.load_anchors(store)["wxid_friend"]["local_id"] == 4,
              str(wxdb.load_anchors(store)))
    finally:
        if old is None:
            os.environ.pop("WXREPLY_WECHAT_DIR", None)
        else:
            os.environ["WXREPLY_WECHAT_DIR"] = old


# ---------------------------------------------------------------------------
# 4. 读取路线开关
# ---------------------------------------------------------------------------
def test_stamp(tmp, py):
    """缓存戳：只动 WAL 时主库那几段必须保持不变（那是走快路的判据）。"""
    acc, table = gold_layout(tmp, py)
    files = wxdb.db_files(acc)
    src = files["message/message_0.db"]
    a = wxdb._db_stamp(src, KEY)
    b = wxdb._db_stamp(src, KEY)
    check("同一状态戳稳定", a == b, "%s vs %s" % (a, b))
    with open(src + "-wal", "wb") as f:
        f.write(b"\x00" * 64)
    c = wxdb._db_stamp(src, KEY)
    check("只动 WAL：主库那 4 段不变（可走只合并 WAL 的快路）",
          c.split("|")[:4] == a.split("|")[:4], "%s vs %s" % (a, c))
    check("只动 WAL：整戳变了（知道要合并）", c != a)
    check("换一把钥匙：戳里的 key 指纹跟着变",
          wxdb._db_stamp(src, bytes(32)).split("|")[0] != a.split("|")[0])


def test_readmode(tmp):
    store = os.path.join(tmp, "rm_store")
    os.makedirs(store, exist_ok=True)
    old = os.environ.pop("WXREPLY_READ_MODE", None)
    try:
        check("默认路线是 auto", readmode.resolve(store) == "auto")
        readmode.set_mode(store, "ui")
        check("写进 collect.yaml 后生效", readmode.resolve(store) == "ui")
        check("yaml 里其它行没被吃掉",
              "read_mode: ui" in open(readmode.config_path(store),
                                      encoding="utf-8").read())
        readmode.set_mode(store, "db")
        check("改第二次是替换不是追加",
              open(readmode.config_path(store), encoding="utf-8").read().count(
                  "read_mode") == 1)
        os.environ["WXREPLY_READ_MODE"] = "db"
        check("环境变量压过配置文件", readmode.resolve(store) == "db")
        os.environ["WXREPLY_READ_MODE"] = "乱写的"
        check("非法值回落默认", readmode.resolve(store) == "auto")
        os.environ.pop("WXREPLY_READ_MODE", None)
        try:
            readmode.set_mode(store, "nope")
            check("set_mode 拒非法值", False)
        except ValueError:
            check("set_mode 拒非法值", True)
    finally:
        os.environ.pop("WXREPLY_READ_MODE", None)
        if old is not None:
            os.environ["WXREPLY_READ_MODE"] = old
    readmode.record(store, mode="auto", via="db", detail={"account": "wxid_x"})
    st = readmode.last_state(store)
    check("state 记下实际走的路线", st.get("via") == "db" and st.get("detail"),
          str(st))


def main():
    tmp = tempfile.mkdtemp(prefix="wxdb-test-")
    py = gold_python()
    try:
        test_aes_kat()
        test_aes_roundtrip()
        test_page_inverse()
        test_wal_mechanics(os.path.join(tmp, "wal"))
        test_readmode(tmp)
        if py is None:
            for n in ("真 SQLCipher 库解密", "Reader 全链路（真库）",
                      "collect 锚点（真库）", "缓存戳（真库）"):
                skip(n, "没找到带 sqlcipher3 的解释器")
        else:
            print("金标准解释器：%s" % py)
            test_gold_decrypt(os.path.join(tmp, "gold1"), py)
            test_reader_gold(os.path.join(tmp, "gold2"), py)
            test_collect_gold(os.path.join(tmp, "gold3"), py)
            test_stamp(os.path.join(tmp, "gold4"), py)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d 通过 / %d 失败 / %d 跳过" % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
