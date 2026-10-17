"""微信 4.x 本地库直读：SQLCipher 4 解密 + 消息查询（只读）。

背景：微信 4.0.5 起聊天区改成自绘（Qt Quick），UIAutomation 只能看到一个空壳窗口，
采集器那条路依赖"窗口可见、桌面没锁屏"。这条路不碰界面：直接从本地加密库里读，
锁屏、最小化、窗口被盖都照样能读。

只读三条底线：
  * 只读微信进程内存里已有的密钥（ReadProcessMemory），不注入、不挂钩、不改内存；
  * 只读本地 .db 文件，解密副本落在自己的 store/cache 下，绝不动原文件；
  * 不改产品定位——只给建议，不自动发送。

与 UI 路线的关系：两条路都往 store/messages.jsonl 写同一种行（session/sender/text/fp），
下游 watch/generate/panel 完全不用改。选哪条路由 collector/readmode.py 决定。
"""
import atexit
import base64
from contextlib import suppress
import ctypes
import hashlib
import hmac as hmac_mod
import json
import os
import re
import sqlite3
import struct
import time
from ctypes import wintypes
from datetime import datetime, timezone

try:                       # 直接跑（collector 目录在 sys.path 上）
    import wxaes
except ImportError:        # 被当包导入时（tests / 面板）
    from collector import wxaes  # type: ignore

try:                       # 直接跑（collector 目录在 sys.path 上）
    import storelock
except ImportError:        # 被当包导入时
    from collector import storelock  # type: ignore

# 进程退出时把本轮落过盘的明文副本删掉。解密副本不再每轮删（删了 WAL 快路就
# 永远走不到，每轮都要整库 AES），改成「进程活着期间保留、退出时清」：
# 明文只在采集器运行时存在，和它本来就有窗口是同一个暴露面。
_PLAIN_TO_PURGE = set()


def _purge_plain():
    for p in list(_PLAIN_TO_PURGE):
        for suffix in ("", "-wal", "-shm", ".part", ".stamp"):
            with suppress(OSError):
                os.remove(p + suffix)
    _PLAIN_TO_PURGE.clear()


atexit.register(_purge_plain)

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"      # 长文本压缩帧（不引第三方解压库，先占位）
DLL_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None

PAGE_SZ = 4096
RESERVE_SZ = 80                      # IV(16) + HMAC-SHA512(64)
KDF_ITER = 256000                    # 微信魔改 WCDB 的库密钥派生迭代数
CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"

# 主密钥 cfg 提取的版本锚点（微信 4.1.10.31+/4.1.15.13 实测；换大版本要重采）
MASTER_DLL_PATTERN = bytes.fromhex(
    "83ec404889d64889cb0f57c00f1142100f11024c8bb1c8020000"
    "4883b9d0020000107209488b9bb8020000eb074881c3b8020000"
    "4d85f60f880a0200004983fe10736d4c89761048c746180f0000"
    "000f10030f110648b8")
MASTER_DLL_VERIFY = (b"488944242048b8", b"488944242848b8", b"488944243048b8")
CFG_LANDMARK = b"global_config"
CFG_PTR_BACK = 0x138
CFG_OFFSET = 0x68
CFG_DWORD_OFF = 0x40
CFG_WXID_OFF = 0x48
CFG_CIPHER_OFF = 0x2B8

# 消息类型 -> 占位文本（对齐 UI 路线读到的样子）
LOCAL_TYPE_TEXT = {
    3: "[图片]", 34: "[语音]", 43: "[视频]", 47: "[动画表情]",
    48: "[位置]", 49: "[链接/文件/卡片]", 50: "[通话]",
    10000: "[系统消息]", 11000: "[动画表情]", 8594229559345: "[红包]",
}
SELF_SENDER_IDS = {2}                # 实测多数分片里自己就是这个 id；解析不到名字时兜底


class Unavailable(Exception):
    """这条路当前走不通（没找到账号/拿不到密钥/不是 Windows），调用方应回落 UI 路线。"""


# ---------------------------------------------------------------------------
# SQLCipher 4 页解密
# ---------------------------------------------------------------------------
def _db_stamp(src, key):
    """缓存戳：key 指纹 | 主库大小+头尾摘要 | WAL 大小+摘要。

    对主库只摘要头尾（128KB）：整库摘要要在每次轮询里读几十上百 MB，而"页数变了"
    （checkpoint 把 WAL 并回主库）一定体现在头部与大小上。头尾都没动、只有 WAL 变了，
    就只合并 WAL —— 这是常走的快路。**代价说清楚**：主库中段被原地改写而头尾不动
    的情形我们不会察觉；真机上验收时专门验"新消息能不能读出来"。
    """
    parts = [wxaes.fingerprint_of_key(key), str(os.path.getsize(src))]
    parts.append(_digest(src, 0, 131072))
    sz = os.path.getsize(src)
    parts.append(_digest(src, max(0, sz - 131072), 131072))
    wal = src + "-wal"
    if os.path.exists(wal):
        parts.append(str(os.path.getsize(wal)))
        parts.append(_digest(wal, 0, 131072))
    return "|".join(parts)


def _digest(path, offset, length):
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            h.update(f.read(length))
    except OSError:
        return "na"
    return h.hexdigest()[:12]


def _pbkdf2(passwd, salt, iters):
    return hashlib.pbkdf2_hmac("sha512", passwd, salt, iters, dklen=32)


def _mac_key(enc_key, salt):
    return _pbkdf2(enc_key, bytes(b ^ 0x3A for b in salt), 2)


def split_key(key):
    """32 字节 = 裸 key（salt 取文件头前 16 字节）；48 字节 = key(32)+salt(16)。"""
    if len(key) == 48:
        return key[:32], key[32:]
    return key, None


def page_ok(enc_key, page, pgno, salt=None):
    """页 HMAC 校验：过不了就说明这把钥匙不对，也可能是页被写坏。"""
    if len(page) < PAGE_SZ:
        return False
    if len(enc_key) == 48 and salt is None:
        enc_key, salt = split_key(enc_key)
    if salt is None:
        if pgno != 1:
            # 只有第 1 页的头部 16 字节才是 salt；别的页拿它当 salt 会算出错的
            # MAC（页 2 起头部是密文）。这个坑第一次就是这么踩出来的，必须显式传。
            return False
        salt = page[:16]
    # 起算位置分页讨论：第 1 页跳过头部 16 字节 salt，其余页从 0 起算。
    # 这条是拿真 SQLCipher 造的库测出来的 —— 拍脑袋写成"所有页都跳过 16 字节"
    # 会在第 2 页起全部 HMAC 不过（自造夹具若用同一假设，测试反而会"通过"）。
    start = 16 if pgno == 1 else 0
    data = page[start:PAGE_SZ - RESERVE_SZ + 16]
    stored = page[PAGE_SZ - 64:PAGE_SZ]
    mac = hmac_mod.new(_mac_key(enc_key, salt), data, hashlib.sha512)
    mac.update(struct.pack("<I", pgno))
    return mac.digest() == stored


def verify_key(key, page1):
    """这把钥匙能开这个库吗（只看第 1 页 HMAC，不落盘）。"""
    try:
        return page_ok(key, page1, 1)
    except Exception:                                        # noqa: BLE001
        return False


def decrypt_page(enc_key, page, pgno):
    """解密一页，返回 4096 字节明文页（reserve 段补 0）。"""
    iv = page[PAGE_SZ - RESERVE_SZ:PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        enc = page[16:PAGE_SZ - RESERVE_SZ]
        head = page[:16] if len(enc_key) == 48 else b"SQLite format 3\x00"
        return head + wxaes.cbc_decrypt(enc_key[:32], iv, enc) + b"\x00" * RESERVE_SZ
    enc = page[:PAGE_SZ - RESERVE_SZ]
    return wxaes.cbc_decrypt(enc_key[:32], iv, enc) + b"\x00" * RESERVE_SZ


def decrypt_db(src, key, dst, with_wal=True):
    """把加密库解密成明文 sqlite 写到 dst；返回 (页数, 跳过的坏页数)。

    坏页只跳过不报错：微信在跑，最后一页可能正被写（HMAC 对不上），
    为了一页把整库读不了不值当 —— 但要计数，调用方能看见。
    """
    enc_key, salt = split_key(key)
    n, bad = 0, 0
    with open(src, "rb") as fi:
        if os.path.getsize(src) < PAGE_SZ * 2:
            raise Unavailable("库里只有不到两页，还没写过消息")
        first = fi.read(PAGE_SZ)
        fi.seek(0)
        if salt is None:
            salt = first[:16]          # 裸 key：文件头 16 字节就是 salt（全库共用）
        tmp = dst + ".part"
        # 半成品也是明文聊天库（复核 R2-2）：任何异常路径都要把它删掉，不留驻。
        try:
            # 权限收紧在 Windows 上只能落到 os.chmod（只读位，复核 F-21），本来
            # 就不是真保护，别为它叠加第二套失败模式（复核 R3-1：os.fchmod 是
            # Unix-only，Windows 上抛 AttributeError 而非 OSError，except 接不住）。
            with open(tmp, "wb") as fo:
                _try_chmod(tmp, 0o600)
                pgno = 1
                first_bad = False
                while True:
                    page = fi.read(PAGE_SZ)
                    if len(page) < PAGE_SZ:
                        break
                    if page_ok(enc_key, page, pgno, salt):
                        fo.write(decrypt_page(enc_key, page, pgno))
                        n += 1
                    else:
                        bad += 1
                        if pgno == 1:
                            first_bad = True
                        fo.write(b"\x00" * PAGE_SZ)       # 坏页占位，保住页号对齐
                    pgno += 1
                fo.flush()
                os.fsync(fo.fileno())
            # 第 1 页对不上 = 密钥错或文件被写坏，后面每一页都只会是零洞：
            # 这种副本必须当失败处理，不能让调用方当成「库里没消息」
            if first_bad or not n:
                raise Unavailable("第 1 页 HMAC 不过（密钥不对或库被写坏）")
            # WAL 合并：最新消息多半还在 WAL 里，不合并就会"读不到刚来的一句"
            merged = _merge_wal(src, tmp, enc_key, salt) if with_wal else 0
            os.replace(tmp, dst)
            _PLAIN_TO_PURGE.add(dst)
        except BaseException:
            with suppress(OSError):
                os.remove(tmp)
            raise
    return n, bad, merged


def _merge_wal(db_path, dst, enc_key, salt):
    """把 -wal 里已提交的帧盖回明文库；返回合并的帧数。

    帧格式：24 字节头(pgno 大端 + 提交后页数 + salt1 + salt2 + 校验) + 4096 加密页。
    只认 salt 与 WAL 头一致的帧，再逐帧过 HMAC —— 半截帧/旧周期残留会被挡掉。
    """
    wal = db_path + "-wal"
    if not os.path.exists(wal):
        return 0
    try:
        with open(wal, "rb") as f:
            head = f.read(32)
            if len(head) < 32:
                return 0
            magic = struct.unpack(">I", head[0:4])[0]
            psz = struct.unpack(">I", head[8:12])[0]
            # 只认自己认识的那种 WAL：页大小不同或文件头坏了就整轮放弃，
            # 硬按 4096 帧去解会得到一堆错位的页（比不合并更糟）
            if magic not in (0x377F0682, 0x377F0683) or psz != PAGE_SZ:
                return 0
            h_salt = head[16:24]
            frames, pending, db_pages_after = {}, [], 0
            while True:
                fh = f.read(24)
                if len(fh) < 24:
                    break
                page = f.read(PAGE_SZ)
                if len(page) < PAGE_SZ:
                    break
                if fh[8:16] != h_salt:
                    continue
                pgno = struct.unpack(">I", fh[0:4])[0]
                # 提交帧 = 帧头 4-8 字节「提交后页数」> 0（WAL 规范）。未提交事务
                # 里的帧虽然 salt/HMAC 都合法，但 SQLite 从未把它们对外可见过
                #（复核 R2-4）：攒着不结算，遇到提交帧才一起放行，行尾没提交的丢弃。
                commit_after = struct.unpack(">I", fh[4:8])[0]
                if pgno < 1 or not page_ok(enc_key, page, pgno, salt):
                    continue
                if commit_after:
                    for p, pl in pending:
                        frames[p] = pl
                    pending = []
                    frames[pgno] = decrypt_page(enc_key, page, pgno)
                    db_pages_after = commit_after
                else:
                    pending.append((pgno, decrypt_page(enc_key, page, pgno)))
    except OSError:
        return 0
    # 读到 WAL 尾也没等到提交帧的 pending：属于未提交事务，整体丢弃（R2-4）
    pending = None
    if not frames:
        return 0
    cur_pages = os.path.getsize(dst) // PAGE_SZ
    ordered = sorted(frames.items())          # 同页后写覆盖先写，按页号升序落
    # 页号必须连续：WAL 里只提交了一部分新页时中间会留零洞，sqlite 打开就是
    # "database disk image is malformed"。有洞就整轮放弃，下一轮走整库解密。
    need = cur_pages
    for pgno, _plain in ordered:
        if pgno > need + 1:
            return 0
        need = max(need, pgno)
    merged = 0
    with open(dst, "r+b") as f:
        for pgno, plain in ordered:
            f.seek((pgno - 1) * PAGE_SZ)
            f.write(plain)
            merged += 1
        # 提交帧里的「提交后页数」比当前小 = 库被裁过（VACUUM 等），尾巴要截掉
        if db_pages_after and os.path.getsize(dst) > db_pages_after * PAGE_SZ:
            f.truncate(db_pages_after * PAGE_SZ)
    return merged


# ---------------------------------------------------------------------------
# 库文件定位
# ---------------------------------------------------------------------------
def _wechat_data_roots():
    """微信 4.x 数据根目录候选。"""
    out = []
    for env in ("WXREPLY_WECHAT_DIR", "WECHAT_DATA_DIR"):
        p = os.environ.get(env)
        if p:
            out.append(p)
    for drive in ("C:", "D:", "E:"):
        for rel in (r"Users\{u}\Documents\xwechat_files",
                    r"Users\{u}\AppData\Roaming\Tencent\xwechat_files",
                    r"Documents\xwechat_files"):
            u = os.environ.get("USERNAME") or os.environ.get("USER") or ""
            out.append(os.path.join(drive + os.sep, rel.format(u=u)))
    return [p for p in out if os.path.isdir(p)]


def find_accounts():
    """返回含 db_storage 的账号目录列表（wxid 就是目录名）。"""
    found = []
    for root in _wechat_data_roots():
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            p = os.path.join(root, name, "db_storage")
            if os.path.isdir(p):
                found.append(os.path.join(root, name))
    return found


def db_files(account_dir):
    """账号目录 -> {相对名: 绝对路径}。只收消息/会话/联系人相关的库。"""
    base = os.path.join(account_dir, "db_storage")
    out = {}
    for rel in ("contact.db", os.path.join("session", "session.db"),
                os.path.join("message", "message_resource.db")):
        p = os.path.join(base, rel)
        if os.path.exists(p):
            out[rel.replace(os.sep, "/")] = p
    msg_dir = os.path.join(base, "message")
    if os.path.isdir(msg_dir):
        for f in sorted(os.listdir(msg_dir)):
            if re.match(r"^message_\d+\.db$", f):
                out["message/" + f] = os.path.join(msg_dir, f)
    return out


# ---------------------------------------------------------------------------
# 密钥：主密钥 -> 逐库密钥（Windows 内存只读提取）
# ---------------------------------------------------------------------------
def weixin_pid():
    """找 Weixin.exe 的 pid（按可执行文件名）。"""
    if os.name != "nt":
        raise Unavailable("只在 Windows 上找微信进程")
    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "weixin.exe":
            try:
                return int(parts[1])
            except ValueError:
                continue
    return 0


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("GlblcntUsage", wintypes.DWORD),
                ("ProccntUsage", wintypes.DWORD),
                ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wintypes.DWORD),
                ("hModule", wintypes.HMODULE),
                ("szModule", ctypes.c_wchar * 256),
                ("szExePath", ctypes.c_wchar * 260)]


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", ctypes.c_ulong),
                ("__a1", ctypes.c_ulong),
                ("RegionSize", ctypes.c_size_t),
                ("State", ctypes.c_ulong), ("Protect", ctypes.c_ulong),
                ("Type", ctypes.c_ulong), ("__a2", ctypes.c_ulong)]


def _find_weixin_module(pid):
    """(基址, 大小, 模块路径)；微信 4.x 主模块是 weixin.dll。"""
    if os.name != "nt":
        raise Unavailable("密钥提取只在 Windows 上可用")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k32.CreateToolhelp32Snapshot(0x08 | 0x10, pid)
    if not snap or snap in (-1, (1 << 64) - 1):
        return None
    try:
        me = MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(me)
        ok = k32.Module32FirstW(snap, ctypes.byref(me))
        while ok:
            if me.szModule and me.szModule.lower() == "weixin.dll":
                return (me.modBaseAddr or 0), me.modBaseSize, me.szExePath
            me.dwSize = ctypes.sizeof(me)
            ok = k32.Module32NextW(snap, ctypes.byref(me))
    finally:
        k32.CloseHandle(snap)
    return None


def _movabs_xor_material(dll_path):
    """从 weixin.dll 特征码后的 4×movabs 立即数拼出 32 字节 XOR 材料。"""
    try:
        with open(dll_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    hit = data.find(MASTER_DLL_PATTERN)
    if hit < 0:
        return None
    hex_txt = data[hit:hit + 220].hex()[len(MASTER_DLL_PATTERN) * 2:]
    key = ""
    for vf in MASTER_DLL_VERIFY:
        if len(hex_txt) < 30 or hex_txt[16:30] != vf.decode():
            return None
        key += hex_txt[0:16]
        hex_txt = hex_txt[30:]
    if len(hex_txt) < 16:
        return None
    key += hex_txt[0:16]
    try:
        return bytes.fromhex(key)
    except ValueError:
        return None


def extract_master_key(pid):
    """从进程里取出 (主密钥 hex, cfgDword, wxid)；取不到返回 None。

    cfg 对象 = MSVC std::string 布局的 "global_config" 地标，附近是指针链；
    dbKey 密文与 DLL 里的 movabs 材料异或即主密钥。锚点是版本敏感的，
    换微信大版本要重采 —— 取不到就走 Config.Cipher 内存扫描那条兜底路。
    """
    if os.name != "nt":
        raise Unavailable("密钥提取只在 Windows 上可用")
    base, mod_size, dll_path = _find_weixin_module(pid) or (0, 0, "")
    if not base or not dll_path or mod_size <= 0 or mod_size >= 0x40000000:
        return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.OpenProcess(0x0010 | 0x0400, False, pid)
    if not h:
        return None

    def read(addr, n):
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t(0)
        if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n,
                                 ctypes.byref(got)) and got.value:
            return buf.raw[:got.value]
        return None

    def read_str(addr):
        b = read(addr + 16, 8)
        if not b:
            return ""
        size = struct.unpack_from("<Q", b)[0]
        if size <= 0 or size > 0x7FFFFFFF:
            return ""
        if size <= 15:                       # SSO：内容内联在对象里
            data = read(addr, size)
        else:
            pb = read(addr, 8)
            data = read(struct.unpack_from("<Q", pb)[0], size) if pb else None
        return (data or b"")[:size].decode("utf-8", "replace")

    def read_buf(addr):
        b = read(addr + 16, 8)
        if not b:
            return None
        size = struct.unpack_from("<Q", b)[0]
        if size <= 0 or size > 0x400:
            return None
        capb = read(addr + 24, 4)
        cap = struct.unpack_from("<I", capb)[0] if capb else 0
        if (cap | 0xF) == 0xF:
            data = read(addr, size)
        else:
            pb = read(addr, 8)
            data = read(struct.unpack_from("<Q", pb)[0], size) if pb else None
        return data[:size] if data and len(data) == size else None

    try:
        image = read(base, mod_size)
        if not image or len(image) != mod_size:
            return None
        pos = -1
        for i in range(len(image) - 8, 0, -8):
            if struct.unpack_from("<I", image, i)[0] == len(CFG_LANDMARK):
                cap = struct.unpack_from("<I", image, i + 8)[0]
                if cap and (cap | 0xF) == 0xF and i - 16 >= 0 and \
                        image[i - 16:i - 16 + len(CFG_LANDMARK)] == CFG_LANDMARK:
                    pos = i
                    break
        if pos < 0:
            return None
        vb = read(base + pos - CFG_PTR_BACK, 8)
        if not vb:
            return None
        v18 = struct.unpack_from("<Q", vb)[0]
        cb = read(v18 + CFG_OFFSET, 8)
        if not cb:
            return None
        cfg = struct.unpack_from("<Q", cb)[0]
        if not 0x10000 <= cfg < 0x800000000000:
            return None
        dwb = read(cfg + CFG_DWORD_OFF, 4)
        cfg_dword = struct.unpack_from("<I", dwb)[0] if dwb else 0
        wxid = read_str(cfg + CFG_WXID_OFF)
        cipher = read_buf(cfg + CFG_CIPHER_OFF)
        if not cipher:
            return None
        material = _movabs_xor_material(dll_path)
        if not material or len(material) != len(cipher):
            return None
        master = bytes(a ^ b for a, b in zip(cipher, material))
        return master.hex(), cfg_dword, wxid
    finally:
        k32.CloseHandle(h)


def derive_keys(master_hex, files):
    """主密钥 + 每库 salt -> 逐库密钥。返回 {相对名: key}，只留 HMAC 验过的。"""
    try:
        master = bytes.fromhex(master_hex)
    except ValueError:
        return {}
    out = {}
    for rel, path in files.items():
        try:
            with open(path, "rb") as f:
                page1 = f.read(PAGE_SZ)
        except OSError:
            continue
        if len(page1) < PAGE_SZ:
            continue
        salt = page1[:16]
        key = _pbkdf2(master, salt, KDF_ITER)
        if verify_key(key, page1):
            out[rel] = key
    return out


def _try_chmod(path, mode):
    """尽力收紧权限。Windows 上 chmod 只映射只读位（审计 F-21），真正的保护是
    %APPDATA% 的用户 profile ACL；POSIX 上就是 0600。失败不挡主流程。"""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


_DPAPI_ENTROPY = b"wx-reply-assistant/keys-v1"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_bytes(data, protect):
    """Windows DPAPI 加/解密一段字节（CryptProtectData / CryptUnprotectData）。

    加密结果只有当前用户（+本机）解得开：.keys.json 被拷走、被云盘同步、
    被别的账户读到，都拿不到钥匙。解不开时由调用方当缓存失效兜底。
    """
    if os.name != "nt":
        raise Unavailable("DPAPI 只在 Windows 上可用")
    if not data:
        raise ValueError("空数据不做 DPAPI")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ent = _DPAPI_ENTROPY
    blob_in = _DataBlob(len(data), ctypes.cast(
        ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    blob_ent = _DataBlob(len(ent), ctypes.cast(
        ctypes.create_string_buffer(ent, len(ent)), ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None, 0,
            ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI 调用失败（GetLastError=%d）" % ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def scan_config_cipher(pid, files, log=None):
    """内存扫 com.Tencent.WCDB.Config.Cipher，把附近候选当密钥逐个 HMAC 验。

    这是 4.1+ 的主路径之一（cfg 锚点失效时的兜底）。慢（要遍历提交内存），
    所以只在主路径失败时调用，且只在还没拿到密钥时。
    """
    if os.name != "nt":
        raise Unavailable("密钥提取只在 Windows 上可用")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.OpenProcess(0x0010 | 0x0400, False, pid)
    if not h:
        return {}
    pages = {}
    for rel, path in files.items():
        try:
            with open(path, "rb") as f:
                pages[rel] = (f.read(PAGE_SZ), f.read(16))
        except OSError:
            continue
    hits = {}
    scanned = 0
    try:
        addr = 0
        mbi = MBI()
        while k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                 ctypes.sizeof(mbi)):
            size = int(mbi.RegionSize or 0)
            if size <= 0:
                break
            # 只扫「已提交 + 可读 + 非 GUARD」的区域。以前这里写反成
            # 「不在可读集合里才读」：常规数据页全被跳过、去读不可读页，
            # ReadProcessMemory 全部失败，兜底扫描等于不存在（审计 F-07）。
            prot = int(mbi.Protect or 0)
            if (int(mbi.State or 0) == 0x1000
                    and (prot & 0xFF) in (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)
                    and not (prot & 0x100)
                    and size <= (1 << 30)):    # 超大区域不读，避免一次性分配上 GB
                scanned += 1
                buf = ctypes.create_string_buffer(size)
                got = ctypes.c_size_t(0)
                if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size,
                                         ctypes.byref(got)) and got.value:
                    data = buf.raw[:got.value]
                    base = addr
                    off = data.find(CONFIG_CIPHER_NAME)
                    if off < 0:
                        off = data.find(CONFIG_CIPHER_NAME[4:])
                    while off >= 0:
                        for cand in _candidate_keys(data, off, base):
                            for rel, (page1, salt) in pages.items():
                                if rel in hits:
                                    continue
                                if verify_key(cand, page1):
                                    hits[rel] = cand[:32] if len(cand) != 48 \
                                        else cand[:32]
                        off = data.find(CONFIG_CIPHER_NAME, off + 1)
            addr += size
    finally:
        k32.CloseHandle(h)
    if log:
        log("  内存扫描：扫过 %d 个区域，命中 %d 个库" % (scanned, len(hits)))
    return hits


def _candidate_keys(data, off, base):
    """地标附近 64 字节里，每 4 字节对齐地取 32/48 字节当候选。

    候选多但代价低：每把都用 HMAC 验一次，验过才算数 —— 宁多验几个，
    不能靠"看着像"就把钥匙定下来。
    """
    out = []
    for start in range(max(0, off - 64), min(len(data), off + 64), 4):
        for n in (32, 48):
            blob = data[start:start + n]
            if len(blob) == n and any(blob):
                out.append(blob)
    return out


class KeyStore:
    """密钥缓存：store/cache/db/.keys.json。

    会缓存钥匙（不然每次都要从微信进程内存重新提取）。Windows 上钥匙经
    DPAPI 加密后才落盘 —— 只有当前用户能解开，文件被拷走/同步/别的账户
    读到都拿不到钥匙；其它平台没有等价物，落明文 + 0600（威胁模型见
    docs/security-fixes-2026-10.md）。旧格式（明文 hex）仍可读。
    """

    def __init__(self, cache_dir, log=None):
        self.cache_dir = cache_dir
        self.log = log
        self.path = os.path.join(cache_dir, ".keys.json")
        self.data = {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def get(self, rel, page1):
        item = self.data.get(rel)
        if not item:
            return None
        raw = item.get("key", "")
        try:
            if raw.startswith("dpapi:"):
                raw = _dpapi_bytes(base64.b64decode(raw[6:]), False).decode("ascii")
            key = bytes.fromhex(raw)
        except Exception:
            # 解不开（换用户/换机器/文件坏了）＝缓存失效，回内存重新提取，不算错
            return None
        return key if key and verify_key(key, page1) else None

    def put(self, rel, key):
        stored = key.hex()
        if os.name == "nt":
            try:
                stored = "dpapi:" + base64.b64encode(
                    _dpapi_bytes(key.hex().encode("ascii"), True)).decode("ascii")
            except Exception as e:
                # 加密失败就不落盘（复核 F-02）：宁可这轮不缓存，也不把明文钥匙写进文件。
                # 密钥留在内存里本轮照样能用；下次运行重新从微信进程内存提取。
                if self.log:
                    self.log("  ⚠️ DPAPI 加密密钥失败（%s: %s），本轮不缓存密钥（不写明文）"
                             % (type(e).__name__, e))
                return
        self.data[rel] = {"key": stored,
                          "fp": wxaes.fingerprint_of_key(key),
                          "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.save()

    def save(self):
        os.makedirs(self.cache_dir, exist_ok=True)
        tmp = self.path + ".part"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            _try_chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            _try_chmod(self.path, 0o600)
        except BaseException:
            with suppress(OSError):
                os.remove(tmp)
            raise


def acquire_keys(account_dir, cache_dir, log=print):
    """拿到该账号各库的密钥（缓存优先，其次内存提取，最后扫描）。"""
    files = db_files(account_dir)
    if not files:
        raise Unavailable("账号目录里没有可读的库")
    ks = KeyStore(cache_dir, log=log)
    keys = {}
    for rel, path in files.items():
        try:
            with open(path, "rb") as f:
                page1 = f.read(PAGE_SZ)
        except OSError:
            continue
        k = ks.get(rel, page1)
        if k:
            keys[rel] = k
    missing = {r: p for r, p in files.items() if r not in keys}
    if not missing:
        return keys, files
    # 缓存不全是常态（微信会新建 message_N.db 分片，salt 不一样）。这里绝不能
    # 用「命中过半就收工」：那样新分片永远拿不到钥匙，而 messages() 是按分片
    # 吞错的，表现就是新分片的会话静默不进库（还没有任何报错）。
    pid = weixin_pid()
    if not pid:
        if keys:
            log("  ⚠️ 缺 %d 个库的钥匙（微信没开，内存取不了）：%s"
                % (len(missing), ", ".join(sorted(missing))))
            return keys, files
        raise Unavailable("没找到 Weixin.exe 进程（关着？）")
    log("  微信 pid=%d，尝试从内存取密钥" % pid)
    got = extract_master_key(pid)
    if got:
        master_hex, _dword, wxid = got
        log("  主密钥到手（wxid=%s），派生逐库密钥" % (wxid or "?"))
        for rel, key in derive_keys(master_hex, missing).items():
            keys[rel] = key
            ks.put(rel, key)
    still = {r: p for r, p in files.items() if r not in keys}
    if still:
        log("  主密钥路径不够，转内存扫描（%d 个库待解）" % len(still))
        for rel, key in scan_config_cipher(pid, still, log=log).items():
            keys[rel] = key
            ks.put(rel, key)
    if not keys:
        raise Unavailable("拿不到任何库密钥（微信版本变了？先跑 dbcheck 看报告）")
    still = {r: p for r, p in files.items() if r not in keys}
    if still:
        # 拿不全就明说：调用方要把这行带进 info，别让它表现成「一直没有新消息」
        log("  ⚠️ %d 个库仍然没有密钥，本轮只读其余库：%s"
            % (len(still), ", ".join(sorted(still))))
    return keys, files


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
class Reader:
    """一个账号的解密读句柄。所有查询都走解密副本，原库只读。"""

    def __init__(self, account_dir, cache_dir, log=print):
        self.account_dir = account_dir
        self.wxid = os.path.basename(account_dir.rstrip(os.sep))
        self.cache_dir = cache_dir
        self.log = log
        self.keys, self.files = acquire_keys(account_dir, cache_dir, log)
        os.makedirs(cache_dir, exist_ok=True)
        self._conns = {}
        # 按分片吞掉的错误记在这里：collect 用完要报出来，并且别推进锚点
        self.shard_errors = {}
        self.missing_keys = sorted(set(self.files) - set(self.keys))

    def plain_path(self, rel):
        """解密副本路径（只含 ASCII，避免中文路径在 sqlite3 上的坑）。"""
        return os.path.join(self.cache_dir, self.wxid + "__" + rel.replace("/", "_"))

    def _close_one(self, rel):
        c = self._conns.pop(rel, None)
        if c is not None:
            with suppress(Exception):
                c.close()

    def conn(self, rel):
        """打开解密副本；stamp 变了才重新解密（原库 size/头尾/密钥指纹）。

        每次调用都重算 stamp —— Reader 现在会在进程里活很久，缓存了连接却不
        重算的话，新消息永远读不到（评审 B4）。
        """
        src = self.files[rel]
        key = self.keys.get(rel)
        if not key:
            raise Unavailable("没有 %s 的密钥" % rel)
        dst = self.plain_path(rel)
        stamp_p = dst + ".stamp"
        stamp = _db_stamp(src, key)
        old = ""
        if os.path.exists(stamp_p):
            try:
                with open(stamp_p, "r", encoding="utf-8") as f:
                    old = f.read().strip()
            except OSError:
                old = ""
        if old == stamp and os.path.exists(dst):
            if rel in self._conns:
                return self._conns[rel]
        else:
            # 换副本前先关掉旧连接：Windows 上文件仍被 sqlite 打开着，os.replace 会失败
            self._close_one(rel)
        # 主库正文没变（只动了 WAL）就只合并 WAL，别整库重解 —— 微信每写一句都会
        # 动 WAL，整库 AES 解密几十上百 MB 谁也扛不住；WAL 帧合并是幂等的。
        # 戳的前 4 段（key 指纹 / 大小 / 头摘要 / 尾摘要）= 主库身份；后 2 段是 WAL。
        full = (not os.path.exists(dst) or not old
                or old.split("|")[:4] != stamp.split("|")[:4])
        if old != stamp or not os.path.exists(dst):
            if full:
                n, bad, merged = decrypt_db(src, key, dst)
                if bad:
                    self.log("  %s 解密 %d 页（%d 页 HMAC 不过已跳过），WAL 合并 %d 帧"
                             % (rel, n, bad, merged))
            else:
                enc_key, salt = split_key(key)
                if salt is None:
                    with open(src, "rb") as fh:
                        salt = fh.read(16)
                merged = _merge_wal(src, dst, enc_key, salt)
                if not merged:
                    # 合并没成功（WAL 头不认识 / 页号有洞 / 没有可提交的帧）：退回
                    # 整库解密。绝不能把「没合并」当成新戳写下去，否则新消息会
                    # 一直读不到，直到主库自己变了（评审 B16）。
                    n, bad, merged = decrypt_db(src, key, dst)
            with open(stamp_p, "w", encoding="utf-8") as f:
                f.write(stamp)
            _try_chmod(stamp_p, 0o600)
        _try_chmod(dst, 0o600)
        c = sqlite3.connect("file:%s?mode=ro" % dst.replace("?", "%3f"),
                            uri=True)
        c.text_factory = _text_factory
        c.row_factory = sqlite3.Row       # 查询里按列名取值，不靠下标顺序
        # sqlite3.connect 是惰性的：真正的「解不开」要到第一条语句才抛。这里先
        # 探活，好让调用方能区分「副本坏了」和「真的没有新消息」（评审 B18）。
        try:
            c.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.Error as e:
            with suppress(Exception):
                c.close()
            for suffix in ("", "-wal", "-shm", ".stamp"):
                with suppress(OSError):
                    os.remove(dst + suffix)
            _PLAIN_TO_PURGE.discard(dst)
            raise Unavailable("%s 的解密副本打不开：%s" % (rel, e)) from e
        self._conns[rel] = c
        return c

    def close(self, purge=False):
        """关连接。purge=True 连明文副本一起删（手动 dbcheck 用，别留驻）。"""
        for rel in list(self._conns):
            self._close_one(rel)
        self._conns = {}
        if purge:
            for rel in self.files:
                dst = self.plain_path(rel)
                for suffix in ("", "-wal", "-shm", ".part", ".stamp"):
                    with suppress(OSError):
                        os.remove(dst + suffix)
                _PLAIN_TO_PURGE.discard(dst)

    # -- 元数据 ------------------------------------------------------------
    def contacts(self):
        """username -> 显示名（优先备注）。"""
        out = {}
        try:
            for r in self.conn("contact.db").execute(
                    "SELECT username, nick_name, remark FROM contact"):
                user = r["username"] or ""
                if user:
                    out[user] = (r["remark"] or r["nick_name"] or user)
        except (sqlite3.Error, Unavailable, KeyError):
            pass
        return out

    def session_list(self, limit=50):
        """最近会话：[{'username','name','unread','preview','ts'}]，按时间倒序。

        读不出来就抛 Unavailable，绝不返回空表 —— 空表会被上层当成「没有会话」，
        和解密失败长得一模一样，auto 模式就再也不会回落界面树（评审 B18）。
        """
        rows = []
        try:
            cur = self.conn("session/session.db").execute(
                "SELECT username, unread_count, summary, last_timestamp "
                "FROM SessionTable WHERE is_hidden=0 ORDER BY last_timestamp DESC "
                "LIMIT ?", (limit,))
            for r in cur:
                rows.append({"username": r["username"], "unread": r["unread_count"],
                             "preview": r["summary"], "ts": r["last_timestamp"]})
        except Unavailable:
            raise
        except (sqlite3.Error, KeyError) as e:
            raise Unavailable("会话表读不出来：%s" % e) from e
        names = self.contacts()
        for it in rows:
            it["name"] = names.get(it["username"], it["username"])
        return rows

    # -- 消息 --------------------------------------------------------------
    def _sender_names(self):
        """sender id -> username。message_resource.db.SenderName2Id 是权威表。"""
        out = {}
        for rel in ("message/message_resource.db",) + tuple(
                r for r in self.files if re.match(r"^message/message_\d+\.db$", r)):
            try:
                for r in self.conn(rel).execute(
                        "SELECT rowid, user_name FROM SenderName2Id"):
                    out.setdefault(r["rowid"], r["user_name"])
            except (sqlite3.Error, Unavailable, KeyError):
                continue
            try:
                for r in self.conn(rel).execute(
                        "SELECT rowid, user_name FROM Name2Id"):
                    out.setdefault(r["rowid"], r["user_name"])
            except (sqlite3.Error, Unavailable, KeyError):
                continue
        return out

    def _msg_tables(self):
        """每个分片里存在哪些 Msg_<md5> 表。"""
        out = {}
        for rel in self.files:
            if not re.match(r"^message/message_\d+\.db$", rel):
                continue
            try:
                cur = self.conn(rel).execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'Msg_%'")
                for r in cur:
                    out.setdefault(r["name"], []).append(rel)
            except (sqlite3.Error, Unavailable) as e:
                self.shard_errors[rel] = "%s: %s" % (type(e).__name__, e)
                continue
        return out

    def messages(self, username, limit=50, since_ts=0):
        """读某会话最近的 limit 条（跨分片合并，按时间升序返回）。

        行的 sender 已解析成 me / them / unknown —— 判不准的一律 unknown，
        下游只会把它晾着（watch 只对 them 触发），不会拿它当对方新消息。
        """
        table = "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()
        shards = self._msg_tables().get(table)
        if not shards:
            return []
        names = self._sender_names()
        cols = ("local_id, local_type, real_sender_id, create_time, "
                "message_content, sort_seq")
        rows = []
        for rel in shards:
            try:
                cur = self.conn(rel).execute(
                    "SELECT %s FROM %s WHERE create_time >= ? "
                    "ORDER BY create_time DESC, sort_seq DESC LIMIT ?"
                    % (cols, table), (since_ts, limit))
                for r in cur:
                    rows.append(dict(r))
            except (sqlite3.Error, Unavailable) as e:
                self.shard_errors[rel] = "%s: %s" % (type(e).__name__, e)
                continue
        rows.sort(key=lambda r: (r.get("create_time") or 0,
                                 r.get("sort_seq") or 0,
                                 r.get("local_id") or 0))
        return [self._row(username, r, names) for r in rows[-limit:]]

    def messages_after(self, username, after, limit=200):
        """按 (create_time, sort_seq, local_id) 严格大于 after 的顺序取一页。

        跨分片合并后按同一顺序排序，只返回最小的 limit 条。调用方拿返回的最后
        一条当游标接着读，积压多少都能读完（评审 B2：以前只取最后 20 条，
        中间那段被锚点直接跳过，永久丢）。
        """
        table = "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()
        shards = self._msg_tables().get(table)
        if not shards:
            return []
        names = self._sender_names()
        ats = int(after[0] or 0)
        aseq = after[1]
        alid = int(after[2] or 0)
        cols = ("local_id, local_type, real_sender_id, create_time, "
                "message_content, sort_seq")
        if aseq is None:
            # 老锚点没有 sort_seq：只能按 (时间, local_id) 比较，否则锚点那一行
            # 会因为它自己的 sort_seq 更大而被重复读出来
            where = ("WHERE create_time > ? "
                     "   OR (create_time = ? AND local_id > ?) ")
            args = (ats, ats, alid)
        else:
            aseq = int(aseq or 0)
            where = ("WHERE create_time > ? "
                     "   OR (create_time = ? AND sort_seq > ?) "
                     "   OR (create_time = ? AND sort_seq = ? AND local_id > ?) ")
            args = (ats, ats, aseq, ats, aseq, alid)
        rows = []
        for rel in shards:
            try:
                cur = self.conn(rel).execute(
                    "SELECT %s FROM %s %s"
                    "ORDER BY create_time ASC, sort_seq ASC, local_id ASC LIMIT ?"
                    % (cols, table, where), args + (limit,))
                for r in cur:
                    rows.append(dict(r))
            except (sqlite3.Error, Unavailable) as e:
                self.shard_errors[rel] = "%s: %s" % (type(e).__name__, e)
                continue
        rows.sort(key=lambda r: (r.get("create_time") or 0,
                                 r.get("sort_seq") or 0,
                                 r.get("local_id") or 0))
        return [self._row(username, r, names) for r in rows[:limit]]

    def shard_failures(self, username):
        """该会话所在分片这一轮读失败的清单（非空就别推进锚点）。"""
        table = "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()
        return [r for r in self._msg_tables().get(table, [])
                if r in self.shard_errors]

    def _row(self, username, r, names):
        text, kind = _content_text(r.get("message_content"), r.get("local_type"))
        sid = r.get("real_sender_id") or 0
        user = names.get(sid)
        if user:
            sender = "me" if user == self.wxid else "them"
        elif sid in SELF_SENDER_IDS:
            sender = "me"
        else:
            sender = "unknown"
        ts = int(r.get("create_time") or 0)
        return {"session": username, "sender": sender, "text": text,
                "kind": kind, "local_id": r.get("local_id"),
                "sort_seq": r.get("sort_seq") or 0,
                "sender_id": sid, "sender_user": user,
                "ts_unix": ts,
                "ts_utc": datetime.fromtimestamp(ts, timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ") if ts else ""}


def _looks_readable(text):
    """像人能读的文本吗：控制字符占比不能高。

    二进制内容（图片、语音的原始字节）用 utf-8 硬解常常"能解出来"，
    解出来是一串 \x00；这类东西绝不能进建议里，宁可只留占位符。
    """
    if not text:
        return False
    bad = sum(1 for ch in text if ord(ch) < 32 and ch not in "\n\r\t")
    return bad <= max(1, len(text) // 10)


def _text_factory(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data


def _content_text(content, local_type):
    """message_content -> (文本, kind)。

    纯文本直接还原；压缩/容器/二进制一律给占位符 —— 宁可不显示，
    也不能把二进制噪声当用户发的字塞进建议里。
    """
    kind = LOCAL_TYPE_TEXT.get(local_type)
    if isinstance(content, bytes):
        if content.startswith(ZSTD_MAGIC):
            return "[长文本（压缩内容未展开）]", kind or "text"
        if content.startswith(b"<"):
            return _xml_preview(content), kind or "link"
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return kind or "[非文本消息]", kind or "unknown"
    elif isinstance(content, str):
        text = content
    else:
        return kind or "", kind or "unknown"
    if not _looks_readable(text):
        return kind or "[非文本消息]", kind or "unknown"
    if kind and not text.strip():
        return kind, kind
    return text, kind or "text"


def _xml_preview(content):
    """XML 卡片（链接/引用/系统消息）取一个能看的短预览。"""
    try:
        text = content.decode("utf-8", "replace")
    except Exception:                                        # noqa: BLE001
        return "[链接/卡片]"
    m = re.search(r"<title>(.*?)</title>", text, re.S)
    if m and m.group(1).strip():
        return "[链接] " + m.group(1).strip()[:80]
    if "revoke" in text or "撤回" in text:
        return "[撤回了一条消息]"
    return "[链接/卡片]"


# ---------------------------------------------------------------------------
# 对外入口：给 wx_collector 调
# ---------------------------------------------------------------------------
def anchors_path(store_dir):
    return os.path.join(store_dir, ".db_anchor.json")


def load_anchors(store_dir):
    try:
        with open(anchors_path(store_dir), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_anchors(store_dir, anchors):
    p = anchors_path(store_dir)
    os.makedirs(store_dir, exist_ok=True)
    tmp = p + ".part"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(anchors, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except BaseException:
        with suppress(OSError):
            os.remove(tmp)
        raise


def collect(store_dir, limit_per_session=20, sessions=8, include_history_min=0,
            min_interval=15.0, log=print):
    """跑一轮 DB 读取，返回 (rows, info)。

    rows 是 messages.jsonl 口径的行（还差 fp，由调用方补 —— 指纹只有一份定义，
    在 wx_collector.fingerprint 里）。info 里有本轮走到哪一步、锚点怎么动的。

    首次见到某个会话：只把锚点定在最新一条，不倒灌历史 —— messages.jsonl 是
    "实时窗口"，历史属于 history.db，这条界线不能破。

    min_interval：两次真正读库之间的最小间隔（秒）。sync 是 4 秒一轮、面板自检 30 秒
    一轮，而一次读取要开库、解密/合并 WAL，不能每 4 秒来一遍。跳过不等于丢消息：
    锚点不动，下一轮照样从锚点之后读起。
    """
    cache_dir = os.path.join(store_dir, "cache", "db")
    # sync 子进程和面板 check_round 子进程都会走到这里（复核 R2-3）：不互斥就会
    # 并发解密同一个明文副本、并发写同一批固定名 .part 临时文件。锁拿不到就让
    # 这一轮失败，不能静默降级成无锁执行。
    with storelock.StoreLock(cache_dir):
        accounts = find_accounts()
        if not accounts:
            raise Unavailable("没找到微信数据目录（xwechat_files/*/db_storage）")
        now = int(time.time())
        if min_interval and now - _last_pass(store_dir) < min_interval:
            return [], {"account": "", "skipped": "interval",
                        "since_last": now - _last_pass(store_dir),
                        "min_interval": min_interval, "new": 0, "sessions": 0}
        # 有多个账号时选会话表最大的那个（多开/多账号场景）
        ranked = []
        try:
            for acc in accounts:
                try:
                    r = Reader(acc, cache_dir, log)
                except Unavailable as e:
                    log("  账号 %s 跳过：%s" % (os.path.basename(acc), e))
                    continue
                try:
                    cnt = len(r.session_list(limit=200))
                except Exception:
                    r.close()
                    raise
                ranked.append((cnt, r))
        except Exception:
            # 排名中途出错：已建的 Reader 全部关掉 —— 每个都带着解密副本（复核 F-02）
            for _n, r in ranked:
                r.close()
            raise
        if not ranked:
            raise Unavailable("所有账号都拿不到密钥")
        ranked.sort(key=lambda t: t[0], reverse=True)
        reader = ranked[0][1]
        # 落选账号同样建过 Reader（解密副本已落盘）：立刻关掉，别把明文库留在盘上（复核 F-02）
        for _n, r in ranked[1:]:
            r.close()
        try:
            anchors = load_anchors(store_dir)
            sessions_now = reader.session_list(limit=sessions)
            rows, fresh = [], []
            for it in sessions_now:
                user = it["username"]
                if user.endswith("@openim") or user == "filehelper":
                    continue
                a = anchors.get(user)
                if not a:
                    # 第一次见到它：锚点定在"最新那一条"，默认不倒灌历史。
                    # 注意这里不能拿 include_history_min 当条件去查 —— 默认 0 会让
                    # since=now，一条都查不到，锚点也就定不下来（下一轮还是第一次）。
                    since = max(0, now - include_history_min * 60) if include_history_min else 0
                    latest = reader.messages(user, limit=limit_per_session if include_history_min
                                             else 1, since_ts=since)
                    if latest:
                        anchors[user] = {"ts": latest[-1]["ts_unix"],
                                         "local_id": latest[-1]["local_id"],
                                         "sort_seq": latest[-1].get("sort_seq") or 0,
                                         "name": it.get("name") or user,
                                         "at": now}
                        if include_history_min:
                            rows.extend(latest)
                    continue
                # 从锚点之后一页页读，直到追平。以前只取「最后 20 条」，积压超过
                # 20 条时中间那段会被新锚点直接跳过，永久丢（评审 B2）。
                cursor = (a["ts"], a.get("sort_seq"), a.get("local_id") or 0)
                got, cap = [], limit_per_session * 25
                while True:
                    page = reader.messages_after(user, cursor, limit=limit_per_session)
                    if not page:
                        break
                    got.extend(page)
                    last = page[-1]
                    cursor = (last["ts_unix"], last.get("sort_seq") or 0,
                              last.get("local_id") or 0)
                    if len(page) < limit_per_session or len(got) >= cap:
                        break
                if got:
                    rows.extend(got)
                    failed = reader.shard_failures(user)
                    if failed:
                        # 有分片没读成功：锚点这轮不能动，不然漏掉的那段再也回不来
                        log("  %s 有分片读失败（%s），锚点本轮不推进"
                            % (user, ", ".join(failed)))
                    else:
                        anchors[user] = {"ts": got[-1]["ts_unix"],
                                         "local_id": got[-1]["local_id"],
                                         "sort_seq": got[-1].get("sort_seq") or 0,
                                         "name": it.get("name") or user, "at": now}
            if rows:
                fresh = [m["session"] for m in rows]
            save_anchors(store_dir, anchors)
            _mark_pass(store_dir)
            info = {"account": reader.wxid, "sessions": len(sessions_now),
                    "new": len(rows), "sessions_with_new": sorted(set(fresh)),
                    "aes": wxaes.status()["backend"], "keys": len(reader.keys),
                    # 缺钥匙/分片读失败都要浮到 info 上：否则「采不到」会被当成
                    # 「没新消息」，面板和日志都看不见（评审 B17 / B18）
                    "keys_missing": list(reader.missing_keys),
                    "shards_failed": sorted(reader.shard_errors)}
            return rows, info
        finally:
            reader.close()

def _pass_file(store_dir):
    return os.path.join(store_dir, "cache", "db", ".last_pass.json")


def _last_pass(store_dir):
    try:
        with open(_pass_file(store_dir), "r", encoding="utf-8") as f:
            return float(json.load(f).get("ts") or 0)
    except (OSError, ValueError, TypeError):
        return 0.0


def _mark_pass(store_dir):
    p = _pass_file(store_dir)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time()}, f)
    except OSError:
        pass


def dbcheck():
    """自检：环境/账号/密钥/解密/最近消息，逐项说清卡在哪。"""
    rep = {"platform": os.name, "aes": wxaes.status(), "accounts": [],
           "weixin_pid": 0}
    try:
        rep["weixin_pid"] = weixin_pid()
    except Unavailable as e:
        rep["pid_error"] = str(e)
    roots = _wechat_data_roots()
    rep["roots"] = roots
    cache_dir = os.path.join(os.environ.get("WXREPLY_STORE", "."), "cache", "db")
    for acc in find_accounts():
        item = {"dir": os.path.basename(acc), "files": len(db_files(acc))}
        r = None
        try:
            # 与 collect 同一把缓存锁（复核 R3-3）：dbcheck 常在 sync 常驻时手动跑，
            # 无锁就会并发解密同一 dst、互踩同一批固定名 .part —— 等于把 R2-2/R2-3
            # 修掉的竞争原样带回来。锁拿不到让该项报错，不静默降级。
            with storelock.StoreLock(cache_dir):
                try:
                    r = Reader(acc, cache_dir)
                    item["keys"] = len(r.keys)
                    item["sessions"] = len(r.session_list(limit=5))
                    item["keys_missing"] = list(r.missing_keys)
                finally:
                    # 关连接 + 删明文副本必须在锁内做：锁外删会把别的进程
                    # 正在用的 dst/.part 一并抹掉（评审 B-1193）
                    if r is not None:
                        r.close(purge=True)
            item["ok"] = item["keys"] > 0
        except Unavailable as e:
            item["ok"] = False
            item["error"] = str(e)
        rep["accounts"].append(item)
    return rep
