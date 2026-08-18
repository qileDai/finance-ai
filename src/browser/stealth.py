"""ICRIS 反自动化：stub disable-devtool + 页内拦截外站跳转 + 浏览器指纹伪装"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DISABLE_DEVTOOL_MARKER = "disable-devtool"
# native-override.js: ICRIS 反调试脚本，检测 CDP 连接后阻止页面渲染
NATIVE_OVERRIDE_MARKER = "native-override"

PRELOAD_SCRIPT = """
(() => {
  // ---- 1. navigator.webdriver ----
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

  // ---- 2. chrome 完整属性 ----
  if (!window.chrome) {
    window.chrome = {};
  }
  window.chrome.runtime = {
    OnInstalledReason: {
      CHROME_UPDATE: 'chrome_update',
      INSTALL: 'install',
      SHARED_MODULE_UPDATE: 'shared_module_update',
      UPDATE: 'update',
    },
    OnRestartRequiredReason: {
      APP_UPDATE: 'app_update',
      OS_UPDATE: 'os_update',
      PERIODIC: 'periodic',
    },
    PlatformArch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
    PlatformNaclArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
    PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
    RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' },
    connect: function() { return { onDisconnect: { addListener: function(){} }, onMessage: { addListener: function(){} }, postMessage: function(){}, disconnect: function(){} }; },
    sendMessage: function() {},
    id: undefined,
  };
  if (!window.chrome.app) {
    window.chrome.app = {
      isInstalled: false,
      InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
      RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
      getDetails: function() { return null; },
      getIsInstalled: function() { return false; },
    };
  }
  if (!window.chrome.csi) {
    window.chrome.csi = function() { return {}; };
  }
  if (!window.chrome.loadTimes) {
    window.chrome.loadTimes = function() {
      return {
        commitLoadTime: performance.timing.navigationStart / 1000,
        connectionInfo: 'h2',
        finishDocumentLoadTime: performance.timing.domContentLoadedEventEnd / 1000,
        finishLoadTime: performance.timing.loadEventEnd / 1000,
        firstPaintAfterLoadTime: 0,
        firstPaintTime: performance.timing.responseStart / 1000,
        navigationType: 'Other',
        requestTime: performance.timing.navigationStart / 1000,
        startLoadTime: performance.timing.navigationStart / 1000,
        wasAlternateProtocolAvailable: false,
        wasFetchedViaSpdy: true,
        wasNpnNegotiated: true,
      };
    };
  }

  // ---- 3. performance.now 伪装 ----
  const _now = performance.now.bind(performance);
  performance.now = function() { return _now(); };

  // ---- 4. disable-devtool stub ----
  const noopDevtool = function() {
    return { success: true, reason: 'ok' };
  };
  noopDevtool.isRunning = false;
  noopDevtool.isSuspend = true;
  window.DisableDevtool = noopDevtool;
  window.disableDevtool = noopDevtool;

  // ---- 5. navigator.plugins 伪装（非空数组） ----
  const fakePlugins = [
    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format',
      length: 1, 0: { type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format' } },
    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '',
      length: 1, 0: { type: 'application/pdf', suffixes: 'pdf', description: '' } },
    { name: 'Native Client', filename: 'internal-nacl-plugin', description: '',
      length: 2, 0: { type: 'application/x-nacl', suffixes: '', description: 'Native Client Executable' },
               1: { type: 'application/x-pnacl', suffixes: '', description: 'Portable Native Client Executable' } },
  ];
  Object.defineProperty(navigator, 'plugins', {
    get: () => {
      const arr = fakePlugins;
      arr.item = (i) => arr[i] || null;
      arr.namedItem = (name) => arr.find(p => p.name === name) || null;
      arr.refresh = () => {};
      return arr;
    },
  });

  // ---- 6. navigator.mimeTypes 伪装 ----
  const fakeMimeTypes = [
    { type: 'application/pdf', suffixes: 'pdf', description: '', enabledPlugin: fakePlugins[1] },
    { type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: fakePlugins[0] },
    { type: 'application/x-nacl', suffixes: '', description: 'Native Client Executable', enabledPlugin: fakePlugins[2] },
    { type: 'application/x-pnacl', suffixes: '', description: 'Portable Native Client Executable', enabledPlugin: fakePlugins[2] },
  ];
  Object.defineProperty(navigator, 'mimeTypes', {
    get: () => {
      const arr = fakeMimeTypes;
      arr.item = (i) => arr[i] || null;
      arr.namedItem = (name) => arr.find(m => m.type === name) || null;
      return arr;
    },
  });

  // ---- 7. navigator.languages 伪装 ----
  Object.defineProperty(navigator, 'languages', {
    get: () => ['zh-CN', 'zh', 'zh-HK', 'en', 'en-US'],
  });

  // ---- 8. WebGL renderer/vendor 伪装 ----
  const getParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(param) {
    if (param === 37445) return 'Intel Inc.';
    if (param === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.call(this, param);
  };
  if (typeof WebGL2RenderingContext !== 'undefined') {
    const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(param) {
      if (param === 37445) return 'Intel Inc.';
      if (param === 37446) return 'Intel Iris OpenGL Engine';
      return getParameter2.call(this, param);
    };
  }

  // ---- 9. Notification.permission 伪装 ----
  try {
    Object.defineProperty(Notification, 'permission', { get: () => 'default' });
  } catch (e) {}

  // ---- 10. 阻止 JS 层跳转到 cr.gov.hk 公开站 ----
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

  // ---- 11. 隐藏 Playwright 特征 ----
  delete window.__playwright;
  delete window.__pw_manual;
  // 修复 iframe contentWindow 检测
  try {
    const origDesc = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
    if (origDesc) {
      Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
        get: function() {
          const win = origDesc.get.call(this);
          if (win) {
            try { delete win.__playwright; } catch(e) {}
            try { delete win.__pw_manual; } catch(e) {}
          }
          return win;
        },
        configurable: true,
      });
    }
  } catch(e) {}

  // ---- 12. 伪装 Permissions API ----
  const origQuery = window.navigator.permissions.query;
  window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
      Promise.resolve({ state: Notification.permission }) :
      origQuery(parameters)
  );
})();
"""

_BLOCKED_HOST_FRAGMENTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "hotjar.com",
)

# ICRIS e-services 门户域名
_ESERVICES_DOMAIN = "e-services.cr.gov.hk"
# 公司注册处公开网站域名（被反自动化重定向的目标）
_CR_PUBLIC_DOMAIN = "www.cr.gov.hk"


async def setup_stealth_context(context: Any) -> None:
    """为 BrowserContext 注入反检测脚本与路由拦截"""
    await context.add_init_script(PRELOAD_SCRIPT)

    async def route_handler(route):
        url = route.request.url.lower()

        # 拦截 disable-devtool 脚本
        if DISABLE_DEVTOOL_MARKER in url:
            await route.fulfill(
                status=200,
                content_type="application/javascript",
                body="/* disable-devtool stubbed in preload */",
            )
            return

        # 拦截导航请求到 cr.gov.hk 公开站（非 e-services）
        if route.request.is_navigation_request():
            if _CR_PUBLIC_DOMAIN in url and _ESERVICES_DOMAIN not in url:
                logger.warning("拦截导航重定向到公开站: %s", route.request.url[:100])
                await route.abort()
                return
            # 导航请求直接放行，不修改头（让 Chrome 原生指纹通过 F5 检测）
            await route.continue_()
            return

        # 拦截媒体请求
        if route.request.resource_type == "media":
            await route.abort()
            return

        # 拦截追踪/分析脚本
        if any(host in url for host in _BLOCKED_HOST_FRAGMENTS):
            await route.abort()
            return

        await route.continue_()

    await context.route("**/*", route_handler)
    logger.info("已启用 ICRIS 反跳转保护 + 浏览器指纹伪装")
