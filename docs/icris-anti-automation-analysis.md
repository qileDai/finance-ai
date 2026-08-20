# ICRIS 门户反自动化分析与注册页 Loading 问题修复计划

> **文档更新（2026-08-19）**：经实测，原方案 A/B/C 已落地，成功解决 HTTP 层重定向问题（CDP 绕过门户重定向、systemclock 正常提取）。但暴露出更深层的 **F5 TSbd JS 挑战**作为新阻断点（注册页 body 为空、Vue 不挂载）。新增 2.5 节分析 TSbd 挑战、方案 D 应对 TSbd，并更新五/六/七节反映实施与验证结果。

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

> **2026-08-19 实测补充**：使用 CDP 连接真实 Chrome 后，门户能加载、systemclock 能提取（HTTP 层重定向已解决），但注册页仍 `bodyLen=0`、Vue 未挂载，诊断确认首脚本为 F5 TSbd 挑战脚本（见 2.5）。即问题从 HTTP 层重定向转移到 JS 层 TSbd 挑战。

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

### 2.4 根本原因总结（已验证更新）

经 2026-08-19 实测，原假设的 HTTP 层重定向已被 CDP 方案解决，但暴露出**更深层的 F5 TSbd JS 挑战**作为新阻断点。

#### 2.4.1 原假设：HTTP 层重定向 — 已解决

**原假设**：ICRIS 门户在 HTTP 层检测到 Playwright 自动化浏览器，直接 302 重定向到公开网站。

**实测验证**：使用 CDP 连接真实 Chrome 后，门户首页能正常加载，URL 保持在 `e-services.cr.gov.hk/ICRIS3EP/system/home.do?systemclock=...`，未被重定向到 `www.cr.gov.hk`，systemclock 也能正常提取。**此问题已被 CDP 方案解决。**

#### 2.4.2 实际当前根因：F5 TSbd JS 挑战 — 未解决

注册页 URL 能打开，但页面 body 内容为空（`bodyLen=0`），Vue 应用未渲染。诊断显示页面首个脚本为 F5 的 TSbd 挑战脚本，该脚本阻断后续渲染。

具体流程：
1. `page.goto(PORTAL_URL)` → CDP 连接的真实 Chrome 绕过 HTTP 层检测 ✓
2. 门户 `home.do` 加载成功，提取有效 `systemclock` ✓
3. `page.goto(注册页 URL)` → 服务器返回 TSbd 挑战页（非真实注册页）
4. TSbd 挑战脚本执行后，body 保持为空（`bodyLen=0`），Vue 不挂载
5. 代码探测 `checkCode=False, checkbox=False, bodySample=''`，判定 Vue 未挂载
6. 重试 2 次仍失败，刷新门户会话时连门户也超时（`Page.goto: Timeout 45000ms`），说明 F5 在多次挑战失败后进一步收紧访问

### 2.5 F5 TSbd 挑战机制分析（新增）

实测诊断数据（2026-08-19）：
```
[Vue未挂载-1诊断] url=.../ICRIS3EF/system/registration/s01.do?systemclock=...
title= bodyLen=0 vue=False forms=0 inputs=0 body=
cookies=_ga=GA1.1.551310959.1787109201; lang=z
scripts=[
  'https://www.e-services.cr.gov.hk/TSbd/0859fe0094ab2000a11f462bcc459ab8cec416923e',  ← F5 挑战脚本
  'https://www.e-services.cr.gov.hk/ICRIS3EF/js/disable-devtool.min.js',
  'https://www.e-services.cr.gov.hk/ICRIS3EF/js/native-override.js',
  'https://www.e-services.cr.gov.hk/ICRIS3EF/js/modernizr-2.6.2.min.js',
  'https://www.e-services.cr.gov.hk/ICRIS3EF/js/axios.js'
]
```

**F5 TSbd 挑战特征**：
| 特征 | 说明 |
|------|------|
| 脚本路径 | `/TSbd/<hash>`（动态 hash，每次会话不同） |
| 渲染行为 | 挑战脚本执行期间 body 为空，真实 Vue 应用不挂载 |
| 检测层级 | JS 层（在 HTTP 层检测通过后触发） |
| 失败后果 | 多次挑战失败后，连门户首页也被收紧访问（45s 超时） |
| 已尝试绕过手段 | CDP+指纹、patchright、playwright-stealth、Playwright 内置 Chromium、headless 模式 — **全部失败** |

**TSbd 挑战与原 stealth 对策的关系**：
- 原 `stealth.py` 的 `PRELOAD_SCRIPT` 伪装 `navigator.webdriver`、plugins、WebGL 等，对旧版检测有效
- 但 TSbd 挑战在更早阶段（脚本加载即触发）阻断渲染，且检测手段更复杂，现有 JS 注入来不及/不足以通过

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

> **实施状态（2026-08-19）**：方案 A/B/C 均已在代码中实现。
> - CDP 自动启动 → 成功绕过 HTTP 层重定向 ✓
> - stealth 增强（plugins/mimeTypes/WebGL/chrome）→ 已注入但不足以通过 F5 TSbd ✓
> - 门户重定向检测（`_is_cr_public_site`）+ 诊断（`_dump_page_diagnostics`）→ 有效 ✓
>
> **结论**：方案 A/B/C 解决了 HTTP 层重定向问题，但**无法解决 F5 TSbd JS 挑战**（见 2.5）。需引入方案 D。

### 方案 D：应对 F5 TSbd JS 挑战（新增，针对当前实际阻断点）

方案 A/B/C 已证明无法绕过 F5 TSbd 挑战。针对此新阻断点，提出以下应对方案，按可靠性排序：

#### 方案 D1：人工接管浏览器（最可靠，推荐立即落地）

**原理**：F5 TSbd 挑战设计上需要人工交互或真实用户行为通过。让用户手动打开 Chrome、通过 F5 挑战建立有效会话后，程序通过 CDP 连接接管，复用已通过挑战的会话进行填表。

**步骤**：
1. 用户设置 `.env`：`CHROME_USE_EXISTING=true`
2. 用户手动启动 Chrome 并带 remote debugging port：
   ```
   chrome --remote-debugging-port=9222 --user-data-dir=<持久目录>
   ```
3. 用户在 Chrome 中手动访问 ICRIS 门户并完成 F5 TSbd 挑战（页面正常渲染）
4. 运行 `python main.py --step register`，程序通过 CDP 连接该 Chrome，复用已通过挑战的会话
5. 程序在已验证的会话中执行注册填表

**优点**：最可靠，能绕过任何 JS 层挑战（因会话已人工通过）。
**缺点**：需人工介入，无法完全自动化；不适合 Worker 队列无人值守场景。

**代码现状**：`launcher.py` 已支持 `CHROME_USE_EXISTING=true` 的 CDP 连接逻辑，`create_browser_context` 已在 CDP 模式下注入 stealth。落地此方案仅需文档化操作流程 + 在注册流程检测到 TSbd 挑战时给出明确提示。

#### 方案 D2：会话持久化复用（半自动）

**原理**：人工通过 F5 挑战一次后，持久化 Chrome 用户数据目录（含 Cookie/LocalStorage/会话 token），后续自动化复用该目录，跳过挑战。

**步骤**：
1. 使用固定的 `--user-data-dir`（已在 `_try_launch_cdp_chrome` 中使用 `icris-chrome-cdp-profile`）
2. 人工首次通过 F5 挑战后，不关闭 Chrome，会话 token 保存在 user-data-dir
3. 后续自动化连接同一 user-data-dir 的 Chrome，复用会话

**代码现状**：`_try_launch_cdp_chrome` 已使用持久化 profile 目录，具备基础。需验证 F5 会话 token 的有效期与复用可行性。

**风险**：F5 会话 token 可能短期失效，需定期人工刷新；多次自动化访问可能触发 F5 重新挑战。

#### 方案 D3：TSbd 挑战脚本逆向分析（探索性，不保证成功）

**原理**：抓取 `/TSbd/<hash>` 脚本内容，分析其检测逻辑（可能检测 CDP 特征、debugger、时间差等），针对性绕过。

**步骤**：
1. 用真实浏览器（非自动化）访问注册页，抓取 `/TSbd/<hash>` 脚本源码
2. 分析脚本检测的自动化特征（CDP Runtime 注入、`Runtime.enable` 调用、debugger 语句等）
3. 针对性 patch：如检测 `Runtime.enable`，则用 `Runtime.evaluate` 替代；如检测 debugger 时间差，则 patch `Date`/`performance`

**风险**：F5 TSbd 脚本通常混淆且动态更新，逆向成本高；即便绕过一轮，F5 可能升级检测。记忆显示 patchright（专门反 CDP 检测的 Playwright fork）也已失败，说明 TSbd 检测手段较深。

#### 方案 D4：等待 F5 规则放宽（被动）

**原理**：F5 规则会周期性调整，当前收紧可能是临时性。暂不处理，定期重试。

**适用场景**：非紧急任务，可等待。

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

### 已完成（原 P0/P1/P2，2026-08-19 验证）

1. ~~**P0（必须）**：修复门户重定向检测~~ — ✅ 已实现 `_is_cr_public_site`，能正确识别重定向
2. ~~**P0（必须）**：增强 CDP 连接模式~~ — ✅ 已实现 `_try_launch_cdp_chrome` 自动启动 + 连接，成功绕过 HTTP 重定向
3. ~~**P1（重要）**：增强 stealth 脚本~~ — ✅ 已实现 plugins/mimeTypes/WebGL/chrome 完整属性伪装
4. ~~**P2（改进）**：添加完整的诊断日志~~ — ✅ 已实现 `_dump_page_diagnostics`，输出 url/title/bodyLen/vue/forms/cookies/scripts

### 新增（针对 F5 TSbd 挑战）

5. **P0（必须）**：落地方案 D1（人工接管）文档化操作流程 + 检测到 TSbd 挑战时给出明确人工接管提示
6. **P1（重要）**：验证方案 D2（会话持久化复用）的 F5 token 有效期
7. **P2（探索）**：方案 D3 TSbd 脚本逆向分析（不保证成功）
8. **P3（被动）**：方案 D4 等待 F5 规则放宽

## 六、验证步骤

### 已验证（2026-08-19）

1. ✅ 运行 `python main.py --step register`（使用 venv: `.venv\Scripts\python.exe`）
2. ✅ CDP 自动启动 Chrome 成功（日志：`自动启动 Chrome CDP 成功（绕过 TLS 指纹检测）`）
3. ✅ 门户页加载成功，URL 保持在 `e-services.cr.gov.hk/ICRIS3EP/system/home.do?systemclock=...`（未被重定向到 cr.gov.hk）
4. ✅ systemclock 正常提取（`systemclock=1787123395949`）
5. ❌ 注册页 Vue 未挂载（2 次尝试均 `bodyLen=0`，探测 `checkCode=False, checkbox=False`）
6. ❌ 诊断确认阻断点为 F5 TSbd 挑战脚本（`/TSbd/0859fe0094ab...`）
7. ❌ 重试刷新门户会话时连门户也超时（`Page.goto: Timeout 45000ms`）

### 待验证（方案 D1 落地后）

1. 用户手动启动 Chrome + 通过 F5 挑战后，程序 CDP 连接接管
2. 验证已通过挑战的会话中，注册页 Vue 能否正常挂载
3. 验证验证码识别流程是否正常

## 七、假设与决策

### 已修正假设

- ~~**原假设**：ICRIS 门户的反自动化主要基于 TLS/HTTP 指纹，而非高级 JS 检测~~
- **修正后**：ICRIS 门户采用**多层反自动化**。HTTP 层重定向已被 CDP 解决，但 F5 TSbd JS 挑战是更深层的阻断点，CDP + stealth 均无法绕过。
- **证据**：WebFetch 被重定向（HTTP 层）；CDP 连接真实 Chrome 后门户能加载但注册页 body 为空且首脚本为 `/TSbd/<hash>`（JS 层）

### 决策

- **决策**：优先使用 CDP 连接真实 Chrome 作为主要方案，增强 Playwright stealth 作为 fallback — 已落地，对 HTTP 层有效
- **决策**：不引入额外的 TLS 伪装库（如 `curl_cffi`）— 仍成立，CDP 已解决 HTTP 层，TLS 库对 JS 层 TSbd 无用
- **新决策**：针对 F5 TSbd，优先落地方案 D1（人工接管），因其最可靠且代码基础已具备
- **新决策**：不投入方案 D3（TSbd 逆向）作为主要路径，因 patchright（专门反 CDP 检测）已失败，说明逆向成本高且 F5 会持续升级
