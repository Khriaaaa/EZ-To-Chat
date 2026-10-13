"""store 读改写跨进程互斥（文件锁）。

面板（web/server.py 的 add_message）和采集器（wx_collector.py 的 parse_and_store /
apply_sender_fixes）都会「读整库 -> 判新 -> 追加 / 全量重写」store/messages.jsonl。
全量重写是「读全量 -> 写 tmp -> os.replace」：另一个进程在它读完之后、replace
之前追加的行，会被旧快照整个盖掉 —— 静默丢行（审计 F-04）。

两边必须拿同一把锁：同一个锁文件（store/.store.lock）、同样的锁定字节范围。
所以实现收口在这个模块，谁改这里的路径/范围，等于同时改两边的锁协议。
同一套实现/协议也被 wxdb.collect 复用为第二个锁文件 cache/db/.store.lock
（复核 R3-3）：那把锁保护明文副本解密，路径不同于本文件，但协议一致。
拿不到锁必须让这一轮失败，不能静默降级成「无锁执行」。
"""
import os


class StoreLock:
    """跨进程互斥（文件锁）。用法：with StoreLock(store_dir): ..."""

    def __init__(self, store_dir):
        self.store_dir = str(store_dir)
        self.f = None

    def __enter__(self):
        self.f = None
        try:
            os.makedirs(self.store_dir, exist_ok=True)
            self.f = open(os.path.join(self.store_dir, ".store.lock"), "a+")
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.f.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
        except Exception as e:
            # 拿不到锁必须让这一轮失败，不能静默降级成「无锁执行」：
            # 两个进程同时无锁做「读库 -> 判新 -> 全量重写」会互相抹行，
            # 而这正是这把锁存在的唯一理由。
            self.f = None
            raise RuntimeError(f"拿不到 store 锁：{type(e).__name__}: {e}") from e
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
