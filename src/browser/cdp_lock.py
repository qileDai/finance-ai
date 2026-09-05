"""CDP Chrome 心跳锁：注册与 NNC1 互斥，活着的持有者禁止被杀 9222。"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from config.settings import PROJECT_ROOT

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 10.0
STALE_AFTER_S = 90.0

_LOCK_DIR = PROJECT_ROOT / "data"
_DEFAULT_MUTEX = _LOCK_DIR / "icris-cdp.lock"
_DEFAULT_LEASE = _LOCK_DIR / "icris-cdp.lease.json"

_thread_local = threading.local()
_process_mutex = threading.Lock()
_default_lock: "CdpHeartbeatLock | None" = None


def pid_is_alive(pid: int) -> bool:
    """lease 持有进程是否还在。pid 无效视为已死。"""
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_i <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid_i
        )
        if not handle:
            # 无权限查询时当成还活着，避免误抢健康锁。
            err = ctypes.windll.kernel32.GetLastError()
            return err == 5  # ERROR_ACCESS_DENIED
        code = wintypes.DWORD()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and int(code.value) == STILL_ACTIVE
    try:
        os.kill(pid_i, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _utc_ts() -> float:
    return time.time()


@contextmanager
def _os_file_mutex(path: Path) -> Iterator[None]:
    """短临界区互斥，只保护 lease 读写。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            if fh.read(1) == b"":
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


class CdpLockHold:
    def __init__(self, lock: "CdpHeartbeatLock", owner: str) -> None:
        self._lock = lock
        self.owner = owner
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._lock._release_hold(self.owner)

    def __enter__(self) -> "CdpLockHold":
        return self

    def __exit__(self, *args: Any) -> None:
        self.release()


class CdpHeartbeatLock:
    def __init__(
        self,
        mutex_path: Path | None = None,
        lease_path: Path | None = None,
        *,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        stale_after_s: float = STALE_AFTER_S,
    ) -> None:
        self.mutex_path = Path(mutex_path or _DEFAULT_MUTEX)
        self.lease_path = Path(lease_path or _DEFAULT_LEASE)
        self.heartbeat_interval_s = float(heartbeat_interval_s)
        self.stale_after_s = float(stale_after_s)
        self._stop_hb = threading.Event()
        self._hb_thread: threading.Thread | None = None
        self._owner = ""

    def held_in_this_thread(self) -> bool:
        return bool(getattr(_thread_local, "cdp_lock_held", False)) and (
            getattr(_thread_local, "cdp_lock_id", None) is self
        )

    def lease_is_alive(self) -> bool:
        data = self._read_lease()
        return self._lease_held_by_live_owner(data)

    def acquire(self, owner: str, *, poll_s: float = 0.5) -> CdpLockHold:
        owner = (owner or "cdp").strip() or "cdp"
        while True:
            stole = False
            taken = False
            with _process_mutex:
                with _os_file_mutex(self.mutex_path):
                    data = self._read_lease_unlocked()
                    if not self._lease_held_by_live_owner(data):
                        if data:
                            stole = True
                            try:
                                age = _utc_ts() - float(data.get("heartbeat") or 0)
                            except (TypeError, ValueError):
                                age = -1.0
                            try:
                                old_pid = int(data.get("pid") or 0)
                            except (TypeError, ValueError):
                                old_pid = 0
                            logger.warning(
                                "CDP 锁回收 owner=%s pid=%s heartbeat_age=%.0fs alive=%s",
                                data.get("owner"),
                                data.get("pid"),
                                age,
                                pid_is_alive(old_pid),
                            )
                        self._write_lease_unlocked(owner)
                        self._begin_hold(owner)
                        taken = True
            if taken:
                if stole:
                    self._kill_stale_chrome()
                logger.info("已获取 CDP 锁 owner=%s", owner)
                return CdpLockHold(self, owner)
            time.sleep(max(0.05, poll_s))

    def _begin_hold(self, owner: str) -> None:
        _thread_local.cdp_lock_held = True
        _thread_local.cdp_lock_id = self
        self._owner = owner
        self._stop_hb.clear()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"cdp-hb-{owner}",
            daemon=True,
        )
        self._hb_thread.start()

    def _release_hold(self, owner: str) -> None:
        self._stop_hb.set()
        if self._hb_thread and self._hb_thread.is_alive():
            self._hb_thread.join(timeout=2.0)
        self._hb_thread = None
        with _process_mutex:
            with _os_file_mutex(self.mutex_path):
                data = self._read_lease_unlocked()
                if str(data.get("owner") or "") == owner and int(
                    data.get("pid") or 0
                ) == os.getpid():
                    self._clear_lease_unlocked()
        _thread_local.cdp_lock_held = False
        _thread_local.cdp_lock_id = None
        self._owner = ""
        logger.info("已释放 CDP 锁 owner=%s", owner)

    def _heartbeat_loop(self) -> None:
        while not self._stop_hb.wait(self.heartbeat_interval_s):
            try:
                with _process_mutex:
                    with _os_file_mutex(self.mutex_path):
                        data = self._read_lease_unlocked()
                        if int(data.get("pid") or 0) != os.getpid():
                            continue
                        if str(data.get("owner") or "") != self._owner:
                            continue
                        data["heartbeat"] = _utc_ts()
                        self.lease_path.write_text(
                            json.dumps(data), encoding="utf-8"
                        )
            except Exception:
                logger.debug("CDP 心跳写入失败", exc_info=True)

    def _read_lease(self) -> dict[str, Any]:
        with _process_mutex:
            with _os_file_mutex(self.mutex_path):
                return self._read_lease_unlocked()

    def _read_lease_unlocked(self) -> dict[str, Any]:
        if not self.lease_path.is_file():
            return {}
        try:
            raw = json.loads(self.lease_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_lease_unlocked(self, owner: str) -> None:
        self.lease_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "owner": owner,
            "pid": os.getpid(),
            "heartbeat": _utc_ts(),
        }
        self.lease_path.write_text(json.dumps(payload), encoding="utf-8")

    def _clear_lease_unlocked(self) -> None:
        try:
            if self.lease_path.is_file():
                self.lease_path.unlink()
        except OSError:
            try:
                self.lease_path.write_text("{}", encoding="utf-8")
            except OSError:
                pass

    def _lease_fresh(self, data: dict[str, Any]) -> bool:
        if not data:
            return False
        try:
            hb = float(data.get("heartbeat") or 0)
        except (TypeError, ValueError):
            return False
        if hb <= 0:
            return False
        return (_utc_ts() - hb) < self.stale_after_s

    def _lease_held_by_live_owner(self, data: dict[str, Any]) -> bool:
        """心跳仍新且持有进程还在，才算活锁。"""
        if not self._lease_fresh(data):
            return False
        try:
            pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            return False
        return pid_is_alive(pid)

    def _kill_stale_chrome(self) -> None:
        try:
            from src.browser.launcher import shutdown_cdp_chrome

            shutdown_cdp_chrome()
        except Exception:
            logger.debug("回收过期 CDP Chrome 失败", exc_info=True)


def get_cdp_lock() -> CdpHeartbeatLock:
    global _default_lock
    if _default_lock is None:
        _default_lock = CdpHeartbeatLock()
    return _default_lock


def set_cdp_lock_for_tests(lock: CdpHeartbeatLock | None) -> None:
    global _default_lock
    _default_lock = lock


def cdp_lock_held_here() -> bool:
    return get_cdp_lock().held_in_this_thread()


def cdp_lease_alive() -> bool:
    return get_cdp_lock().lease_is_alive()


def should_skip_kill_cdp_chrome() -> bool:
    """其他会话仍活着、且本线程未持锁时，禁止杀 9222。"""
    lock = get_cdp_lock()
    if lock.held_in_this_thread():
        return False
    return lock.lease_is_alive()
