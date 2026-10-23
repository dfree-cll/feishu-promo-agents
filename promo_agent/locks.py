# -*- coding: utf-8 -*-
"""跨进程文件锁（Windows 用 msvcrt，POSIX 用 fcntl）。

state/ 下的 JSON 状态文件（事件游标、记忆、指标）会被**多个进程**同时读改写：
listen 长连接主进程、submit 后起的后台聚合线程、以及每次 CLI 命令。
threading.Lock 只在本进程内互斥，跨进程时后写的会整体覆盖先写的 → 丢更新
（实测后果：事件游标丢事件 → 历史事件被反复消费 → 旧日报被重复推送）。

用法：
    from promo_agent.locks import file_lock
    with file_lock(PROCESSED_PATH):
        data = read()
        data.append(...)
        write(data)
"""
import os
import time
from contextlib import contextmanager

try:                                    # Windows
    import msvcrt

    def _try_lock(fd) -> bool:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd):
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

except ImportError:                     # Linux / macOS
    import fcntl

    def _try_lock(fd) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


@contextmanager
def file_lock(path, timeout: float = 10.0):
    """对 path 加跨进程排他锁（锁文件为 path + '.lock'）。

    超时抛 TimeoutError，避免进程卡死；锁在 with 结束时释放。
    """
    lock_path = str(path) + ".lock"
    parent = os.path.dirname(lock_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")         # msvcrt 需要文件非空才能锁字节
            os.lseek(fd, 0, os.SEEK_SET)
        deadline = time.time() + timeout
        while not _try_lock(fd):
            if time.time() > deadline:
                raise TimeoutError(f"获取文件锁超时（{timeout}s）：{lock_path}")
            time.sleep(0.02)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
