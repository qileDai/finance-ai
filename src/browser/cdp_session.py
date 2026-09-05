"""CDP 会话：持锁 + 看门狗。卡住则杀 Chrome 放锁。"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from config.settings import PROJECT_ROOT, settings
from src.browser.cdp_lock import CdpLockHold, get_cdp_lock

logger = logging.getLogger(__name__)

WATCHDOG_SHOT_TIMEOUT_S = 8.0


def capture_cdp_watchdog_screenshot(
    job_id: int = 0, timeout_s: float = WATCHDOG_SHOT_TIMEOUT_S
) -> str:
    """看门狗线程里用 sync CDP 尽量截一张图。失败返回空串，不抛给调用方。"""
    dest = ""

    def _run() -> None:
        nonlocal dest
        try:
            from playwright.sync_api import sync_playwright

            shot_dir = PROJECT_ROOT / "data" / "icris_failures"
            shot_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            name = (
                f"watchdog_{int(job_id)}_{stamp}.png"
                if int(job_id or 0)
                else f"watchdog_{stamp}.png"
            )
            path = shot_dir / name
            cdp_url = str(getattr(settings, "chrome_cdp_url", "") or "http://127.0.0.1:9222")
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(cdp_url)
                try:
                    pages = []
                    for ctx in browser.contexts:
                        pages.extend(list(ctx.pages or []))
                    page = pages[-1] if pages else None
                    if page is None:
                        logger.warning("看门狗截图：CDP 无打开的页面")
                        return
                    page.screenshot(path=str(path))
                    dest = str(path)
                    logger.info("看门狗截图: %s", dest)
                finally:
                    # CDP connect 的 close() 只断开会话，不关独立激活浏览器。
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception:
            logger.warning("看门狗截图失败", exc_info=True)

    t = threading.Thread(target=_run, daemon=True, name="cdp-watchdog-shot")
    t.start()
    t.join(timeout=max(1.0, float(timeout_s)))
    if t.is_alive():
        logger.warning("看门狗截图超时 %.0fs，继续结束 Chrome", timeout_s)
    return dest


class SessionWatchdog:
    """填表墙钟超时；awaiting_review 期间暂停计时。"""

    def __init__(
        self,
        timeout_s: float,
        *,
        job_id: int | None = None,
        store=None,
        pause_status: str = "awaiting_review",
    ) -> None:
        self.timeout_s = max(0.05, float(timeout_s))
        self.job_id = int(job_id or 0)
        self.store = store
        self.pause_status = pause_status
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.fired = False

    def start(self) -> None:
        self._stop.clear()
        self.fired = False
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="cdp-session-watchdog"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _loop(self) -> None:
        elapsed = 0.0
        step = min(1.0, max(0.05, self.timeout_s / 4.0))
        while not self._stop.wait(step):
            if self._is_paused():
                continue
            elapsed += step
            if elapsed < self.timeout_s:
                continue
            self.fired = True
            logger.error(
                "CDP 会话看门狗超时 %.0fs job=%s，先截图再结束 Chrome",
                self.timeout_s,
                self.job_id or "-",
            )
            try:
                capture_cdp_watchdog_screenshot(self.job_id)
            except Exception:
                logger.debug("看门狗截图调用失败", exc_info=True)
            try:
                from src.browser.launcher import shutdown_cdp_chrome

                shutdown_cdp_chrome()
            except Exception:
                logger.exception("看门狗结束 CDP Chrome 失败")
            return

    def _is_paused(self) -> bool:
        if not self.job_id or self.store is None:
            return False
        try:
            status = str(self.store.get_job_status(self.job_id) or "")
        except Exception:
            return False
        return status == self.pause_status


@contextmanager
def hold_cdp_lock(owner: str) -> Iterator[CdpLockHold]:
    """获取 CDP 锁；退出时关 Chrome 并放锁（未开浏览器时关一次也无妨）。"""
    lock = get_cdp_lock()
    hold = lock.acquire(owner)
    try:
        yield hold
    finally:
        try:
            from src.browser.launcher import shutdown_cdp_chrome

            shutdown_cdp_chrome()
        except Exception:
            logger.debug("会话结束关闭 CDP Chrome 失败", exc_info=True)
        hold.release()


@contextmanager
def cdp_exclusive_session(
    owner: str,
    *,
    job_id: int | None = None,
    store=None,
    timeout_s: float | None = None,
    watchdog: bool = True,
) -> Iterator[CdpLockHold]:
    """获取 CDP 锁；退出时关 Chrome 并放锁。"""
    lock = get_cdp_lock()
    hold = lock.acquire(owner)
    fill_timeout = (
        float(timeout_s)
        if timeout_s is not None
        else float(getattr(settings, "icris_cdp_session_timeout_seconds", 1500) or 1500)
    )
    keep = float(getattr(settings, "browser_keep_open_seconds", 15) or 15)
    wd: SessionWatchdog | None = None
    if watchdog:
        wd = SessionWatchdog(
            fill_timeout + keep + 30.0,
            job_id=job_id,
            store=store,
        )
        wd.start()
    try:
        yield hold
    finally:
        if wd:
            wd.stop()
        try:
            from src.browser.launcher import shutdown_cdp_chrome

            shutdown_cdp_chrome()
        except Exception:
            logger.debug("会话结束关闭 CDP Chrome 失败", exc_info=True)
        hold.release()
