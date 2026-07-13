"""ICRIS 反自动化：stub disable-devtool + 页内拦截外站跳转（不 abort 导航）"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DISABLE_DEVTOOL_MARKER = "disable-devtool"

PRELOAD_SCRIPT = """
(() => {
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  window.chrome = window.chrome || { runtime: {} };
  const _now = performance.now.bind(performance);
  performance.now = function() { return _now(); };

  const noopDevtool = function() {
    return { success: true, reason: 'ok' };
  };
  noopDevtool.isRunning = false;
  noopDevtool.isSuspend = true;
  window.DisableDevtool = noopDevtool;
  window.disableDevtool = noopDevtool;

  const shouldBlock = (url) => {
    if (!url || typeof url !== 'string') return false;
    const u = url.toLowerCase();
    return u.includes('cr.gov.hk') && !u.includes('e-services.cr.gov.hk');
  };

  const wrap = (obj, key, factory) => {
    const orig = obj[key];
    if (!orig) return;
    obj[key] = factory(orig);
  };

  wrap(window.location, 'assign', (orig) => function(url) {
    if (shouldBlock(String(url))) return;
    return orig.call(window.location, url);
  });
  wrap(window.location, 'replace', (orig) => function(url) {
    if (shouldBlock(String(url))) return;
    return orig.call(window.location, url);
  });

  try {
    const desc = Object.getOwnPropertyDescriptor(window.location.__proto__, 'href');
    if (desc && desc.set) {
      Object.defineProperty(window.location, 'href', {
        get: desc.get,
        set(v) {
          if (shouldBlock(String(v))) return;
          desc.set.call(window.location, v);
        },
        configurable: true,
      });
    }
  } catch (e) {}
})();
"""

_BLOCKED_HOST_FRAGMENTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "hotjar.com",
)


async def setup_stealth_context(context: Any) -> None:
    """为 BrowserContext 注入反检测脚本与路由拦截"""
    await context.add_init_script(PRELOAD_SCRIPT)

    async def route_handler(route):
        url = route.request.url.lower()
        if DISABLE_DEVTOOL_MARKER in url:
            await route.fulfill(
                status=200,
                content_type="application/javascript",
                body="/* disable-devtool stubbed in preload */",
            )
            return
        if route.request.resource_type == "media":
            await route.abort()
            return
        if any(host in url for host in _BLOCKED_HOST_FRAGMENTS):
            await route.abort()
            return
        await route.continue_()

    await context.route("**/*", route_handler)
    logger.info("已启用 ICRIS 反跳转保护")
