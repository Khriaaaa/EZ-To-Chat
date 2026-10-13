"""AES-256-CBC 解密（只解密，不做加密）。

为什么不用第三方库：安装包只带 pillow / numpy / pyyaml（见 requirements.txt），
Python 是 3.11 embeddable，加一个带编译扩展的 wheel 要动打包链路。
需要的能力只有两样，标准库都有：HMAC-SHA512（hashlib/hmac）和 PBKDF2（hashlib）。
缺的只有 AES 本身，于是这里自己提供，并给快路：

  1. 纯 Python 参考实现（一定可用，慢：4096 字节一页约 30ms）；
  2. 快路 A：Linux/容器里用 ctypes 调 libcrypto（EVP_aes_256_cbc）；
  3. 快路 B：Windows 里用 ctypes 调 bcrypt.dll（AES-256-CBC，走 AES-NI）。

三条路都解同一件事，所以**第一次使用时用 NIST 已知答案自检**：任何快路自检
不过就永久降级到纯 Python 参考实现。这样"没在真机上跑过的分支"不会悄悄给出
错数据 —— 宁可慢，不可错。
"""
import ctypes
import ctypes.util
import hashlib
import os

# NIST SP 800-38A F.2.6（AES-256-CBC）已知答案：key/iv/密文 → 明文
_KAT_KEY = bytes.fromhex("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4")
_KAT_IV = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
_KAT_CT = bytes.fromhex("f58c4c04d6e5f1ba779eabfb5f7bfbd6"
                        "9cfc4e967edb808d679f777bc6702c7d")
_KAT_PT = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a"
                        "ae2d8a571e03ac9c9eb76fac45af8e51")


# ---------------------------------------------------------------------------
# 纯 Python 参考实现
# ---------------------------------------------------------------------------
_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16")
_INV_SBOX = bytearray(256)
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_INV_SBOX = bytes(_INV_SBOX)

# 解密用表：逆 S 盒与 GF(2^8) 上乘 9/11/13/14（列混淆的逆）
_MUL = {}
for _c in (9, 11, 13, 14):
    _t = bytearray(256)
    for _x in range(256):
        _a, _b, _p = _x, _c, 0
        for _ in range(8):
            if _b & 1:
                _p ^= _a
            _hi = _a & 0x80
            _a = (_a << 1) & 0xFF
            if _hi:
                _a ^= 0x1B
            _b >>= 1
        _t[_x] = _p
    _MUL[_c] = bytes(_t)


def _xtime(a):
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _expand_key(key):
    """AES-256 轮密钥：60 个 4 字节字（14 轮 + 初始）。"""
    if len(key) != 32:
        raise ValueError("AES-256 需要 32 字节密钥")
    nk, nr = 8, 14
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        elif i % nk == 4:
            t = [_SBOX[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return w


def _decrypt_block(w, block):
    """解密单个 16 字节块（等价逆密码，网络取字序）。"""
    # 状态按列主序摆放：byte i 落在 s[i % 4][i // 4]（FIPS-197 §3.4）
    s = [[block[4 * c + r] for c in range(4)] for r in range(4)]

    def add_round_key(rnd):
        for c in range(4):
            wk = w[4 * rnd + c]
            # w 是 4 字节列，按行展开
            for r in range(4):
                s[r][c] ^= wk[r]

    add_round_key(14)
    for rnd in range(13, 0, -1):
        # InvShiftRows
        for r in range(1, 4):
            s[r] = s[r][-r:] + s[r][:-r]
        # InvSubBytes
        s = [[_INV_SBOX[s[r][c]] for c in range(4)] for r in range(4)]
        add_round_key(rnd)
        # InvMixColumns
        ns = [[0] * 4 for _ in range(4)]
        for c in range(4):
            a0, a1, a2, a3 = s[0][c], s[1][c], s[2][c], s[3][c]
            ns[0][c] = _MUL[14][a0] ^ _MUL[11][a1] ^ _MUL[13][a2] ^ _MUL[9][a3]
            ns[1][c] = _MUL[9][a0] ^ _MUL[14][a1] ^ _MUL[11][a2] ^ _MUL[13][a3]
            ns[2][c] = _MUL[13][a0] ^ _MUL[9][a1] ^ _MUL[14][a2] ^ _MUL[11][a3]
            ns[3][c] = _MUL[11][a0] ^ _MUL[13][a1] ^ _MUL[9][a2] ^ _MUL[14][a3]
        s = ns
    # 最后一轮没有 InvMixColumns
    for r in range(1, 4):
        s[r] = s[r][-r:] + s[r][:-r]
    s = [[_INV_SBOX[s[r][c]] for c in range(4)] for r in range(4)]
    for c in range(4):
        wk = w[c]
        for r in range(4):
            s[r][c] ^= wk[r]
    return bytes(s[r][c] for c in range(4) for r in range(4))


def _pure_cbc_decrypt(key, iv, data):
    if len(data) % 16:
        raise ValueError("密文长度必须是 16 的倍数")
    w = _expand_key(key)
    out = bytearray()
    prev = iv
    for off in range(0, len(data), 16):
        blk = data[off:off + 16]
        dec = _decrypt_block(w, blk)
        out += bytes(a ^ b for a, b in zip(dec, prev))
        prev = blk
    return bytes(out)


# ---------------------------------------------------------------------------
# 快路：libcrypto（Linux/容器）与 bcrypt.dll（Windows）
# ---------------------------------------------------------------------------
def _openssl_cbc_decrypt(key, iv, data):
    lib = _OSSL
    if lib is None:
        raise OSError("libcrypto 不可用")
    out = ctypes.create_string_buffer(len(data) + 16)
    outl = ctypes.c_int(0)
    tail = ctypes.c_int(0)
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise OSError("EVP_CIPHER_CTX_new 失败")
    try:
        if lib.EVP_DecryptInit_ex(ctx, lib.EVP_aes_256_cbc(), None,
                                  key, iv) != 1:
            raise OSError("EVP_DecryptInit_ex 失败")
        if lib.EVP_CIPHER_CTX_set_padding(ctx, 0) != 1:
            raise OSError("set_padding 失败")
        if lib.EVP_DecryptUpdate(ctx, out, ctypes.byref(outl), data,
                                 len(data)) != 1:
            raise OSError("EVP_DecryptUpdate 失败")
        if lib.EVP_DecryptFinal_ex(ctx, ctypes.byref(out, outl.value),
                                   ctypes.byref(tail)) != 1:
            raise OSError("EVP_DecryptFinal_ex 失败")
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)
    return out.raw[: outl.value + tail.value]


def _bcrypt_cbc_decrypt(key, iv, data):
    b = _BCRYPT
    if b is None:
        raise OSError("bcrypt.dll 不可用")
    h_alg = ctypes.c_void_p()
    h_key = ctypes.c_void_p()
    if b.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES",
                                     None, 0) != 0:
        raise OSError("BCryptOpenAlgorithmProvider 失败")
    try:
        if b.BCryptSetProperty(h_alg, "ChainingMode",
                               ctypes.c_wchar_p("ChainingModeCBC"),
                               2 * len("ChainingModeCBC"), 0) != 0:
            raise OSError("BCryptSetProperty 失败")
        if b.BCryptGenerateSymmetricKey(h_alg, ctypes.byref(h_key), None, 0,
                                        key, len(key), 0) != 0:
            raise OSError("BCryptGenerateSymmetricKey 失败")
        iv_buf = ctypes.create_string_buffer(iv, 16)
        out = ctypes.create_string_buffer(len(data) + 16)
        done = ctypes.c_ulong(0)
        if b.BCryptDecrypt(h_key, data, len(data), None, iv_buf, 16,
                           out, len(data), ctypes.byref(done), 0) != 0:
            raise OSError("BCryptDecrypt 失败")
        return out.raw[: done.value]
    finally:
        if h_key:
            b.BCryptDestroyKey(h_key)
        b.BCryptCloseAlgorithmProvider(h_alg, 0)


def _load_openssl():
    names = ["libcrypto.so.3", "libcrypto.so.1.1", "libcrypto.so",
             "libcrypto.dylib", "libcrypto.3.dylib"]
    path = ctypes.util.find_library("crypto")
    if path:
        names.insert(0, path)
    for n in names:
        try:
            lib = ctypes.CDLL(n)
        except OSError:
            continue
        try:
            lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
            lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
            lib.EVP_aes_256_cbc.restype = ctypes.c_void_p
            lib.EVP_DecryptInit_ex.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                ctypes.c_char_p, ctypes.c_char_p]
            lib.EVP_DecryptUpdate.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                ctypes.c_char_p, ctypes.c_int]
            lib.EVP_DecryptFinal_ex.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
            lib.EVP_CIPHER_CTX_set_padding.argtypes = [ctypes.c_void_p,
                                                       ctypes.c_int]
            return lib
        except AttributeError:
            continue
    return None


def _load_bcrypt():
    if os.name != "nt":
        return None
    try:
        b = ctypes.WinDLL("bcrypt")
    except OSError:
        return None
    try:
        for f in ("BCryptOpenAlgorithmProvider", "BCryptGenerateSymmetricKey",
                  "BCryptDecrypt", "BCryptDestroyKey",
                  "BCryptCloseAlgorithmProvider"):
            getattr(b, f)
        return b
    except AttributeError:
        return None


_OSSL = None
_BCRYPT = None
_backend = None            # 实际选中的解密函数
_backend_name = ""         # "pure" | "openssl" | "bcrypt"
_backend_why = ""          # 降级原因，status() 里能看到


def _check(fn):
    """用 NIST 已知答案验证一个后端。"""
    try:
        got = fn(_KAT_KEY, _KAT_IV, _KAT_CT)
    except Exception as e:                                   # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)
    return (got == _KAT_PT), ("结果不符" if got != _KAT_PT else "")


def _pick():
    """选后端：优先快路（自检通过者），否则纯 Python。结果缓存。"""
    global _backend, _backend_name, _backend_why, _OSSL, _BCRYPT
    if _backend is not None:
        return _backend
    force = os.environ.get("WXREPLY_AES_BACKEND", "").strip().lower()
    cands = []
    if force and force != "auto":
        table = {"pure": _pure_cbc_decrypt, "openssl": _openssl_cbc_decrypt,
                 "bcrypt": _bcrypt_cbc_decrypt}
        if force not in table:
            raise ValueError("WXREPLY_AES_BACKEND 只能是 pure/openssl/bcrypt")
        cands = [(force, table[force])]
    else:
        if os.name == "nt":
            _BCRYPT = _load_bcrypt()
            cands.append(("bcrypt", _bcrypt_cbc_decrypt))
        _OSSL = _load_openssl()
        if _OSSL is not None:
            cands.append(("openssl", _openssl_cbc_decrypt))
        cands.append(("pure", _pure_cbc_decrypt))
    why = []
    for name, fn in cands:
        if name != "pure":
            try:
                fn(_KAT_KEY, _KAT_IV, _KAT_CT)
            except Exception as e:                           # noqa: BLE001
                why.append("%s 不可用(%s)" % (name, type(e).__name__))
                continue
        ok, msg = _check(fn)
        if ok:
            _backend, _backend_name = fn, name
            _backend_why = "; ".join(why)
            return fn
        why.append("%s 自检失败(%s)" % (name, msg))
        if name == "pure":
            raise RuntimeError("AES 纯 Python 参考实现自检都没过，代码有问题")
    _backend, _backend_name = _pure_cbc_decrypt, "pure"
    _backend_why = "; ".join(why)
    return _backend


def cbc_decrypt(key, iv, data):
    """AES-256-CBC 解密（无填充）。key 32 字节，iv 16 字节。"""
    if len(iv) != 16:
        raise ValueError("iv 必须是 16 字节")
    if not data:
        return b""
    return _pick()(key, iv, data)


def status():
    """当前后端与降级原因（诊断用，写进日志/日志行）。"""
    try:
        _pick()
    except Exception as e:                                   # noqa: BLE001
        return {"backend": "none", "why": "%s: %s" % (type(e).__name__, e)}
    return {"backend": _backend_name, "why": _backend_why}


def fingerprint_of_key(key):
    """密钥指纹：只暴露前 8 位，够做"是不是同一把钥匙"的比对，不泄露钥匙。"""
    return hashlib.sha256(key).hexdigest()[:8]


def _selftest_vectors():
    """给测试用：一块随机明文 + 纯 Python 加密？不提供加密 —— 这里只回向量。"""
    return _KAT_KEY, _KAT_IV, _KAT_CT, _KAT_PT


if __name__ == "__main__":                                   # pragma: no cover
    import json
    print(json.dumps(status(), ensure_ascii=False))
