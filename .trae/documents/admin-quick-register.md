# 管理后台「快速注册」模块：表单填字段 + 上传证件 → 跑真实数据注册

## Summary（摘要）

在管理后台（`python main.py admin`，React SPA + `/admin/api/*`）新增一个「快速注册」页面：
运营人员直接在表单填写公司名/注册资本/经营范围/注册地址/董事+股东/身份证号/住址（中英），并上传证件图片（身份证正反面/手持 或 护照页），点「跑注册」后用这些**真实数据**触发 ICRIS 账号注册（`step_icris_register`），**dry_run 填表不提交**。

- 不依赖企微群/roomid，不依赖 ICRIS Worker 进程（admin 进程内后台线程直接跑）。
- 复用已就绪的 `aggregate_company_data`（姓名中英拆分）与 S03 真实地址填表逻辑。
- 单任务槽（串行，避免浏览器冲突）+ 内存状态轮询。

## Current State Analysis（现状）

- [main.py:416-436](file:///d:/projects/finance-ai/main.py#L416-L436) `cmd_admin` → `AdminWebServer(port=8082).start(blocking=True)`，**独立进程**，不跑 ICRIS Worker。
- [admin_server.py](file:///d:/projects/finance-ai/src/web/admin_server.py) 基于 `http.server.ThreadingHTTPServer`：托管 `static/admin/` SPA + `/admin/api/*`（需登录 session）；`_read_body` 限 1MB（L68-72）。
- [admin_api.py:33-86](file:///d:/projects/finance-ai/src/web/admin_api.py#L33-L86) `handle_admin_api(method, path, store, icris_worker)` 路由 `/admin/api/*`，现有 overview/sessions/jobs/quality；`icris_worker` 在 admin 传 `None`。
- [steps.py:119-158](file:///d:/projects/finance-ai/src/workflow/steps.py#L119-L158) `step_icris_register(ctx, *, dry_run, allow_submit, force_isolated_browser)` **直接用 `ctx.company_data`**，无 company_data 则回退 mock；函数内 `from src.browser.icris_registration import IcrisRegistrationBot`。
- [registration_agent.py](file:///d:/projects/finance-ai/src/agent/registration_agent.py) `RegistrationAgent().workflow` 即 `Workflow`，可调 `step_icris_register`。
- [aggregator.py:102](file:///d:/projects/finance-ai/src/materials/aggregator.py#L102) `aggregate_company_data(materials)` 把 `dict[key→{field_value, file_path}]` 聚合为 company_data；`_get_files` 读 `FILE_FIELD_KEYS` 行的 `file_path`；applicant 姓名/地址已做中英拆分（衔接 S03）。
- [checklist.py:44-51](file:///d:/projects/finance-ai/src/materials/checklist.py#L44-L51) `FILE_FIELD_KEYS = {id_card_front, id_card_back, id_card_handheld, address_proof, passport}`；文本字段 key：`company_name_cn/en, registered_capital, business_desc, registered_office_cn/en, director_name, id_number, director_address_cn/en, id_type`。
- [file_store.py:19-57](file:///d:/projects/finance-ai/src/storage/file_store.py#L19-L57) `materials_root()`、`safe_dirname()`、`room_dir(roomid, folder_label=)`。
- [icris_registration.py:3240+](file:///d:/projects/finance-ai/src/browser/icris_registration.py#L3240) `_fill_identity_proof_step` 读 `data["identity_proof"]["document_files"]` 上传；**空列表则跳过 S04，dry_run 不崩**。
- 前端：[App.tsx](file:///d:/projects/finance-ai/web/admin/src/App.tsx) React Router（basename `/admin`）；[Layout.tsx](file:///d:/projects/finance-ai/web/admin/src/components/Layout.tsx) 侧边导航；[api.ts](file:///d:/projects/finance-ai/web/admin/src/api.ts) 封装 fetch（`credentials:"include"`）；[JobsPage.tsx](file:///d:/projects/finance-ai/web/admin/src/pages/JobsPage.tsx) 可参考表单/轮询/按钮交互模式。

## Proposed Changes（改动）

### 1. 后端：`src/web/admin_runner.py`（新增模块）

独立的「快速注册」运行器（避免污染 admin_api）。模块级单例 + 锁：

```python
"""管理后台「快速注册」运行器：表单数据 → aggregate → step_icris_register（后台线程）"""
import base64, json, re, threading, time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import settings
from src.storage.file_store import materials_root, safe_dirname

# 表单字段 → materials key（文本）
TEXT_FIELD_MAP = {
    "company_name_cn": "company_name_cn",
    "company_name_en": "company_name_en",
    "registered_capital": "registered_capital",
    "business_desc": "business_desc",
    "registered_office_cn": "registered_office_cn",
    "registered_office_en": "registered_office_en",
    "director_name": "director_name",
    "id_number": "id_number",
    "director_address_cn": "director_address_cn",
    "director_address_en": "director_address_en",
}
# 证件文件字段（按 id_type 决定哪几个必填）
FILE_FIELD_KEYS = ("id_card_front", "id_card_back", "id_card_handheld", "passport")

@dataclass
class RunnerState:
    status: str = "idle"  # idle|running|succeeded|failed
    started_at: str = ""
    finished_at: str = ""
    messages: list[str] = field(default_factory=list)
    error: str = ""
    company_name: str = ""
    case_id: str = ""
    dry_run: bool = True

    def to_dict(self) -> dict[str, Any]: ...
    def is_running(self) -> bool: return self.status == "running"

_lock = threading.Lock()
_state = RunnerState()

def _utc_now() -> str: ...

def _decode_data_url(data_url: str) -> tuple[bytes, str]:
    """data:image/png;base64,xxxx → (bytes, ext)"""

def _save_uploaded_files(files: dict[str, dict], case_dir: Path) -> dict[str, str]:
    """{field: {name, data_url}} → {field: abs_path}"""

def _build_materials(fields: dict[str, str], file_paths: dict[str, str]) -> dict[str, dict]:
    """构造 materials dict（aggregator 输入格式）"""

def submit(fields: dict[str, str], files: dict[str, dict]) -> tuple[dict, int]:
    """表单提交入口。返回 (json, code)。
    - 校验必填（公司英文名、董事名、身份证号至少一项地址）
    - 生成 case_id = admin-quick-<yyyymmdd-HHMMSS>
    - 存文件到 materials_root()/case_id/
    - aggregate_company_data → company_data
    - 单任务槽：若 _state.is_running() 返回 409
    - 起后台线程跑 _run(ctx)
    """

def _run(company_data: dict, case_id: str) -> None:
    """后台线程：RegistrationAgent().workflow.step_icris_register(ctx, dry_run=True, force_isolated_browser=True)
    捕获异常 → 更新 _state；成功 → _state.status='succeeded'，messages=ctx.messages"""

def status() -> dict[str, Any]:
    """返回 _state.to_dict()"""
```

关键实现点：
- **case_id**：`admin-quick-<yyyymmdd-HHMMSS>`，用作 `materials_root()` 下子目录名（`safe_dirname` 清洗）。
- **文件存储**：`materials_root()/case_id/<field>.<ext>`；前端传 data_url（base64），后端解码落盘。
- **materials 构造**：文本字段 `{field_key, field_value}`；文件字段 `{field_key, field_value:文件名, file_path:绝对路径, status:"ok"}`。
- **id_type**：表单选「内地身份证」→`PRC_ID`、「香港身份证」→`HKID`、「护照」→`PASSPORT`；默认 `PRC_ID`。写入 `materials["id_type"]`。
- **跑注册**：`from src.agent.registration_agent import RegistrationAgent`；`ctx = WorkflowContext(company_data=company_data)`；`agent.workflow.step_icris_register(ctx, dry_run=True, allow_submit=False, force_isolated_browser=True)`。
- **dry_run=True**（用户确认不提交）；`force_isolated_browser=True`（避免与已开 Chrome 冲突）。
- **单任务槽**：`_state.is_running()` 时 `submit` 返回 409 `{"ok":False,"error":"已有注册任务在运行"}`。

### 2. 后端：`src/web/admin_api.py` — 路由分发

在 `handle_admin_api` 的 try 块内（L50-81 之间）增加：
```python
if method == "POST" and rel == "register-runner/submit":
    return _handle_runner_submit(store, body)  # body 由 admin_server 传入
if method == "GET" and rel == "register-runner/status":
    from src.web.admin_runner import status as runner_status
    return _ok(**runner_status())
```
- `handle_admin_api` 签名增加 `body: dict | None = None` 参数（POST 请求体，由 admin_server 解析后传入）。
- `_handle_runner_submit` 调 `admin_runner.submit(fields=body.get("fields",{}), files=body.get("files",{}))`，返回其 `(json, code)`。

### 3. 后端：`src/web/admin_server.py` — 放宽 body + 传 body

- `do_POST`（L175-196）：对 `/admin/api/register-runner/submit` 端点单独读大 body（上限调到 30MB，支持 3-5 张证件 base64），解析 JSON 后作为 `body=` 传给 `handle_admin_api`。
- 其它 POST 保持 1MB 限制。
- `_read_body` 增加可选 `max_bytes` 参数。

### 4. 前端：`web/admin/src/pages/RegisterPage.tsx`（新增）

参考 JobsPage 模式（useState + 轮询 + toast）。表单字段：
- 文本输入：公司中文名、公司英文名、注册资本（默认「1万港币」）、经营范围、注册地址中文、注册地址英文、董事+股东姓名、身份证号码、住址中文、住址英文。
- 下拉：身份证明类型（内地身份证 PRC_ID 默认 / 香港身份证 HKID / 护照 PASSPORT）。
- 文件上传：根据 id_type 动态显示
  - PRC_ID/HKID：身份证正面、反面、手持身份证（3 个 `<input type="file" accept="image/*">`）
  - PASSPORT：护照页（1 个）
- 「跑注册」按钮：禁用条件=必填未填 或 有任务运行中；点击 → `api.registerRunner.submit(fields, files)` → 成功后开始轮询 `status()`（每 2s）。
- 状态区：显示 status 徽标（running/succeeded/failed）、公司名、messages 列表（实时追加）、error。
- 必填校验：公司英文名、董事+股东姓名、身份证号、至少一个地址；证件按 id_type 校验。

### 5. 前端：`web/admin/src/api.ts` — 加方法 + 类型

```typescript
export type RunnerStatus = {
  status: "idle" | "running" | "succeeded" | "failed";
  started_at?: string; finished_at?: string;
  messages?: string[]; error?: string;
  company_name?: string; case_id?: string; dry_run?: boolean;
};
export const api = {
  // ...existing...
  registerRunner: {
    submit: (fields: Record<string,string>, files: Record<string,{name:string;data_url:string}>) =>
      request<ApiOk<{ case_id: string; company_name: string }>>("/admin/api/register-runner/submit", {
        method: "POST", body: JSON.stringify({ fields, files }),
      }),
    status: () => request<ApiOk<RunnerStatus>>("/admin/api/register-runner/status"),
  },
};
```
前端文件→data_url：`FileReader.readAsDataURL(file)` → `await` 拿 `result`。

### 6. 前端：`web/admin/src/App.tsx` + `components/Layout.tsx` — 路由与导航

- App.tsx：`<Route path="register" element={<RegisterPage ... />} />`（加在 jobs 路由后）。
- Layout.tsx：`TITLES` 加 `"/register": "快速注册"`；侧边 `<NavLink to="/register">快速注册</NavLink>`（放「注册任务」前）。

## Assumptions & Decisions（假设与决策）

- **dry_run 不提交**（用户确认）：`dry_run=True, allow_submit=False`；浏览器填完 S01-S04 不点最终提交。
- **证件文件上传**（用户确认）：前端 base64 data_url 随 JSON 提交；后端解码存 `materials_root()/admin-quick-<ts>/`。body 上限 30MB（admin_server 该端点单独放宽）。
- **admin 进程内跑**：不进 ICRIS Worker 队列、不依赖 roomid；`step_icris_register` 直接用 `ctx.company_data`。理由：admin 与 worker 分进程，即时触发体验最好。
- **单任务槽**：同一时刻只允许一个注册任务（ICRIS 注册串行 + 浏览器冲突）；运行中再提交返回 409。
- **状态仅内存**：`RunnerState` 模块级单例，进程重启清空；不写 registration_jobs 表（避免 worker 误认领）。历史记录后续可接入。
- **id_type 默认 PRC_ID**；证件字段按 id_type 动态切换（PRC_ID/HKID→正反+手持；PASSPORT→护照页）。
- **不填字段**：contact_email/phone 用 settings 默认（`MATERIALS_DEFAULT_CONTACT_EMAIL/PHONE`，aggregator 已处理）；邮箱 S03 用 `MATERIALS_DEFAULT_CONTACT_EMAIL`（已实现）。
- **force_isolated_browser=True**：避免与用户已开 Chrome 冲突。
- **不动**：aggregator.py、icris_registration.py、checklist.py、main.py、worker。

## Verification（验证）

1. `py_compile` admin_runner.py、admin_api.py、admin_server.py。
2. 纯逻辑冒烟（不连浏览器）：
   - `_decode_data_url("data:image/png;base64,iVBOR=...")` → (bytes, "png")。
   - `_build_materials({...}, {...})` 产出含 file_path 的 dict；`aggregate_company_data` → `applicant.name_cn="姚曉佳"`、`directors[0].address_en` 非空、`identity_proof.document_files` 含上传文件路径。
   - `submit` 在 `status=="running"` 时返回 409。
3. 前端构建：`cd web/admin && npm run build` → 产物到 `static/admin/`。
4. 端到端（本地，dry_run）：
   - `python main.py admin` → 登录 → 「快速注册」页填用户案例字段 + 上传 3 张证件图 → 「跑注册」。
   - 状态轮询显示 running→succeeded；messages 含 S01-S04 填写日志。
   - 浏览器可见 ICRIS 表单：S03 中文姓名填入、地址非香港+街道/區拆分+國家=中国、邮箱=MATERIALS_DEFAULT_CONTACT_EMAIL、電子查冊未勾选；S04 证件已上传。
   - 运行中再次提交 → 409。
