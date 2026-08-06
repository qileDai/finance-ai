# ICRIS 门户反自动化分析与注册页 Loading 问题修复计划

## 一、问题现象

执行 `python main.py --step register` 时，ICRIS 注册页一直 loading，无法加载 Vue 表单。

运行日志关键信息：
```
[门户加载] URL: https://www.e-services.cr.gov.hk/ICRIS3EP/system/home.do?systemclock=...
打开注册页 (尝试 1/2): https://www.e-services.cr.gov.hk/ICRIS3EF/system/registration/s01.do?systemclock=...
注册页 Vue 未挂载 (尝试 1/2) probe={'spinning': False, 'checkCode': False, 'checkbox': False, 'bodySample': ''}
注册页 Vue 未挂载 (尝试 2/2) probe={'spinning': False, 'checkCode': False, 'checkbox': False, 'bodySample': ''}
systemclock 直链未加载条款页，刷新门户会话后重试
```

**核心问题**：注册页 URL 能打开，但页面 body 内容为空（`bodySample=''`），Vue 应用未渲染。

## 二、反自动化机制分析

### 2.1 门户首页反自动化检测

通过浏览器实测发现，直接访问 `https://www.e-services.cr.gov.hk/` 时：
- **普通浏览器**：正常显示 e-Services Portal 登录/注册页
- **自动化浏览器（含集成浏览器）**：被 **302 重定向** 到 `https://www.cr.gov.hk/sc/home/index.htm`（公司注册处公开网站）

这说明 **e-services.cr.gov.hk 门户本身就做了自动化检测**，在 HTTP 层或 JS 层识别到 Playwright/自动化特征后直接重定向。

### 2.2 注册页面的会话校验

从代码注释中可见（[icris_registration.py](file:///c:/Users/EDY/.trae-cn/worktrees/finance-ai/analyze-project-WNWs9H/src/browser/icris_registration.py#L39)）：
```python
# systemclock 必须来自门户会话，自行伪造会被重定向到 cr.gov.hk 首页
```

注册 URL 需要 `systemclock` 参数，且必须来自有效的门户会话。WebFetch 直接访问注册 URL 确认被重定向到了 `www.cr.gov.hk`。

### 2.3 ICRIS 网站使用的反自动化技术

综合代码和实测分析，ICRIS 使用了以下多层反自动化机制：

| 层级 | 机制 | 代码中的对策 | 效果 |
|------|------|------------|------|
| **HTTP 层** | 服务端检测 User-Agent / TLS 指纹 / 请求头，返回 302 重定向 | 设置 `USER_AGENT` 伪装 Chrome 131 | **不够**：TLS 指纹仍暴露 Playwright |
| **JS 层** | `disable-devtool.min.js` 检测开发者工具/自动化 | `PRELOAD_SCRIPT` stub 掉 `DisableDevtool` | 对门户首页无效，因为首页根本不加载 |
| **JS 层** | `navigator.webdriver` 属性检测 | `Object.defineProperty` 返回 `undefined` | **不够**：现代检测手段更多 |
| **JS 层** | 页面跳转到 `cr.gov.hk` 非 e-services 子域 | `shouldBlock` 拦截非 e-services 的跳转 | 部分有效，但 HTTP 302 在路由层之前 |
| **会话层** | `systemclock` 参数绑定门户 session | 从门户 URL 提取 systemclock | 正确，但前提是门户页能正常加载 |

### 2.4 根本原因总结

**根本原因：ICRIS 门户在 HTTP 层就检测到 Playwright 自动化浏览器，直接 302 重定向到公开网站。**

当前代码的 `stealth.py` 反检测措施仅在 JS 注入层工作（`add_init_script`），但门户的反自动化检测发生在 JS 执行之前（HTTP 响应阶段），JS 注入根本来不及生效。

具体流程：
1. `page.goto(PORTAL_URL)` → Playwright 发送 HTTP 请求
2. 服务器检测到 TLS 指纹 / HTTP2 指纹 / 缺少特定 Cookie 等自动化特征
3. 服务器返回 302 重定向到 `www.cr.gov.hk`
4. 代码中 `route_handler` 的 `shouldBlock` 试图拦截跳转，但 **HTTP 302 重定向是浏览器内部行为，Playwright 的 route 拦截不到**
5. 最终页面落在 `www.cr.gov.hk`，代码误认为门户加载成功（因为 URL 仍含 `cr.gov.hk`）
6. 提取的 `systemclock` 无效或为空
7. 用无效的 systemclock 访问注册页 → 服务器返回空页面或重定向 → Vue 不渲染 → `bodySample=''`

## 三、修复方案

### 方案 A：使用 CDP 连接真实 Chrome（推荐）

**原理**：让 Playwright 连接到用户手动启动的 Chrome 浏览器，而不是用 Playwright 自己的 Chromium。真实 Chrome 的 TLS 指纹、HTTP2 行为完全正常，不会被检测。

**步骤**：
1. 启动参数已支持 CDP 连接（`_try_launch_cdp_chrome`），但当前 CDP 模式下 stealth 注入被跳过
2. 修改 CDP 连接逻辑：先启动 Chrome，等待用户手动通过门户验证（或自动处理），再注入 stealth 脚本
3. 关键修改点：
   - [launcher.py](file:///c:/Users/EDY/.trae-cn/worktrees/finance-ai/analyze-project-WNWs9H/src/browser/launcher.py#L46-L85)：`_try_launch_cdp_chrome` 启动的 Chrome 需要先导航到门户并等待会话建立
   - [launcher.py](file:///c:/Users/EDY/.trae-cn/worktrees/finance-ai/analyze-project-WNWs9H/src/browser/launcher.py#L175-L215)：`create_browser_context` 在 CDP 模式下需要正确注入 stealth

### 方案 B：增强 Playwright 反检测（补充方案）

在 Playwright 直接启动模式下，增强反检测措施：

1. **修补 TLS 指纹**：使用 `playwright-stealth` 或 `curl-impersonate` 类工具伪装 TLS 指纹
2. **增加更多 navigator 属性伪装**：
   - `navigator.plugins`（非空数组）
   - `navigator.languages`
   - `WebGL renderer/vendor`
   - `chrome.runtime` 完整属性
3. **使用持久化用户数据目录**：让浏览器保持之前的 Cookie 和会话状态
4. **添加更真实的浏览器行为**：鼠标移动轨迹、随机延迟等

### 方案 C：混合方案（推荐落地实现）

结合 A 和 B：
1. **首选 CDP 连接真实 Chrome**：这是最可靠的方式
2. **CDP 不可用时增强 Playwright 反检测**：作为 fallback
3. **添加门户加载验证**：导航后检查是否被重定向到公开网站，如果是则报错而非继续

## 四、具体修改清单

### 4.1 [launcher.py](file:///c:/Users/EDY/.trae-cn/worktrees/finance-ai/analyze-project-WNWs9H/src/browser/launcher.py)

1. **`_try_launch_cdp_chrome`** (L46-85)：
   - 增加启动参数 `--disable-blink-features=AutomationControlled`
   - 增加伪装 User-Agent 的参数
   - 增加更真实的 viewport 设置

2. **`launch_browser`** (L95-172)：
   - CDP 模式优先级提高：如果检测到 CHROME_CDP_URL，先尝试 CDP 连接
   - 增加 `--user-data-dir` 持久化选项，保留会话 Cookie

3. **`create_browser_context`** (L175-215)：
   - CDP 模式下不再跳过 stealth 注入，而是尝试注入
   - 增加更完整的 navigator 伪装脚本

### 4.2 [stealth.py](file:///c:/Users/EDY/.trae-cn/worktrees/finance-ai/analyze-project-WNWs9H/src/browser/stealth.py)

1. **增强 `PRELOAD_SCRIPT`**：
   - 添加 `navigator.plugins` 伪装（非空 PluginArray）
   - 添加 `navigator.mimeTypes` 伪装
   - 添加 `WebGL` renderer/vendor 伪装
   - 添加 `Notification.permission` 伪装
   - 添加 `chrome.app` / `chrome.csi` / `chrome.loadTimes` 完整属性

2. **增强 `route_handler`**：
   - 检测 302 重定向到 `www.cr.gov.hk`（非 e-services）时，拦截并阻止
   - 对首页导航请求，添加更真实的 HTTP 请求头（Sec-CH-UA 等 Client Hints）

### 4.3 [icris_registration.py](file:///c:/Users/EDY/.trae-cn/worktrees/finance-ai/analyze-project-WNWs9H/src/browser/icris_registration.py)

1. **`_navigate_to_registration`** (L814-877)：
   - 访问门户后增加重定向检测：如果 URL 变为 `www.cr.gov.hk`（非 e-services），立即报错并提示
   - 增加重试逻辑：被重定向时，尝试使用 CDP 模式或等待更长时间

2. **`_wait_portal_session`** (L295-314)：
   - 增加对 `www.cr.gov.hk` 公开网站重定向的检测和警告

3. **新增诊断工具**：
   - 添加 `_dump_page_diagnostics` 方法，在 Vue 未挂载时输出页面 HTML 源码、Cookie、URL 链等完整诊断信息

### 4.4 [settings.py](file:///c:/Users/EDY/.trae-cn/worktrees/finance-ai/analyze-project-WNWs9H/config/settings.py)

1. 增加配置项 `BROWSER_USE_PERSISTENT_CONTEXT`：是否使用持久化用户数据目录
2. 增加配置项 `BROWSER_CDP_AUTO_LAUNCH`：是否自动启动 Chrome CDP

## 五、实施优先级

1. **P0（必须）**：修复门户重定向检测 — 让程序能正确识别被重定向，而不是在错误页面上空转
2. **P0（必须）**：增强 CDP 连接模式 — 让程序默认使用真实 Chrome
3. **P1（重要）**：增强 stealth 脚本 — 添加更多浏览器指纹伪装
4. **P2（改进）**：添加完整的诊断日志 — 方便后续排查类似问题

## 六、验证步骤

1. 修改后运行 `python main.py --step register`
2. 检查日志是否正确识别门户重定向
3. CDP 模式下验证能否正常进入注册页
4. 验证注册页 Vue 是否正常挂载（`checkCode=True` 或 `checkbox=True`）
5. 验证验证码识别流程是否正常

## 七、假设与决策

- **假设**：ICRIS 门户的反自动化主要基于 TLS/HTTP 指纹，而非高级 JS 检测（因为 WebFetch 也被重定向，说明是 HTTP 层检测）
- **决策**：优先使用 CDP 连接真实 Chrome 作为主要方案，增强 Playwright stealth 作为 fallback
- **决策**：不引入额外的 TLS 伪装库（如 `curl_cffi`），因为复杂度高且 CDP 方案更可靠
