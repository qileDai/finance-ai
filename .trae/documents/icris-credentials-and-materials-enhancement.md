# ICRIS 账号生成规则 + 材料字段补齐 + 收集追踪 实施计划

## Summary

根据用户提供的香港公司注册案例（撼世全球有限公司 / Humsienk Global Limited，董事姚曉佳），对系统做四项增强：
1. **ICRIS 账号生成规则**：用户名 = `Yingtai` + 时间戳后4位，密码 = 用户名 + `@1`（替换当前"邮箱用户名+随机2字符/姓名派生密码"逻辑）
2. **手持身份证照片字段**：新增 `id_card_handheld` 文件字段，覆盖"正反面+手持"的证件收集需求
3. **缺失字段补齐**：新增注册资本、董事住址（中/英文）字段
4. **材料清单文档 + 收集追踪**：产出本案例的材料清单文档，新字段纳入进度追踪与主动提醒

所有改动 feature-flag 化，默认新规则生效、旧逻辑可回退；不破坏现有 DB schema（材料表为 key-value 结构，`upsert_material` 天然支持新 field_key）。

---

## Current State Analysis

### ICRIS 凭证生成（当前）
- [aggregator.py:91-96](file:///d:/projects/finance-ai/src/materials/aggregator.py#L91-L96)：`icris_account.username` = 申请人邮箱用户名前20字符，`password` = 空字符串
- [icris_registration.py:57-79](file:///d:/projects/finance-ai/src/browser/icris_registration.py#L57-L79) `derive_icris_credentials`：
  - 用户名 = 邮箱用户名 → `append_random_username_suffix(username, length=2)` 追加2位随机字母数字
  - 密码 = `ensure_icris_password(acct.password or password_hint)` — 从姓名派生（如 "Chan2026Pass"）或默认值
- [icris_registration.py:110-136](file:///d:/projects/finance-ai/src/browser/icris_registration.py#L110-L136)：ICRIS 密码规则 = ≥10位、首字母大写、含字母+数字（不限制特殊字符）

### 材料字段（当前）
- [checklist.py:21-41](file:///d:/projects/finance-ai/src/materials/checklist.py#L21-L41) `MATERIAL_FIELDS`：19 个字段，文件类仅 `id_card_front`(必填) / `id_card_back` / `address_proof` / `passport`
- **无** `id_card_handheld`（手持身份证）
- **无** `registered_capital`（注册资本，aggregator 硬编码 10000）
- **无** 董事住址字段
- [checklist.py:252-264](file:///d:/projects/finance-ai/src/materials/checklist.py#L252-L264) `FIELD_PRIORITY`：11 个必填字段的提醒优先级

### 材料分类（当前）
- [material_handler.py:28-33](file:///d:/projects/finance-ai/src/wework/material_handler.py#L28-L33) `CLASSIFY_RULES`：4 条正则（身份证→front、护照→passport、地址→address_proof、背面→back），**无手持**规则

### 材料存储
- [db.py:126-133](file:///d:/projects/finance-ai/src/storage/db.py#L126-L133)：`group_materials` 表为 key-value 结构（`field_key`/`field_value`/`file_path`），新增字段无需 DDL

### 追踪与提醒（已有，需接入新字段）
- `progress_summary()` / `prioritized_missing()` / `format_progress_text()` — 自动遍历 `MATERIAL_FIELDS`，新字段加进去即自动生效
- `_maybe_proactive_reminder()`（优化12）— 调用 `prioritized_missing`，新字段自动纳入

---

## Proposed Changes

### 改动 1：ICRIS 账号生成规则

**目标**：用户名 = `Yingtai` + 时间戳后4位（如 `Yingtai8492`），密码 = 用户名 + `@1`（如 `Yingtai8492@1`）

**文件 A：`config/settings.py`**
- 新增 4 个配置项（均带默认值，零配置即可用）：
```python
icris_credential_mode: str = "yingtai"        # "yingtai" 新规则 | "legacy" 旧规则
icris_username_prefix: str = "Yingtai"        # 用户名前缀
icris_username_timestamp_digits: int = 4      # 时间戳取后 N 位
icris_password_suffix: str = "@1"             # 密码后缀（拼在用户名后）
```

**文件 B：`src/materials/aggregator.py`**
- 新增私有函数 `_generate_icris_credentials() -> tuple[str, str]`：
  - `ts_suffix = str(int(time.time()))[-digits:]`
  - `username = f"{prefix}{ts_suffix}"`
  - `password = f"{username}{pw_suffix}"`
- 在 `aggregate_company_data` 中 `icris_account` 块改为：
  - `mode == "yingtai"` → 调 `_generate_icris_credentials()` 填入 username/password
  - `mode == "legacy"` → 保持原逻辑（邮箱用户名[:20] / 空密码）

**文件 C：`src/browser/icris_registration.py`**
- 修改 `derive_icris_credentials(data)`：
  - 若 `icris_account.username` 和 `icris_account.password` 均非空（即 aggregator 已预生成）→ **直接使用，不追加随机后缀、不重新派生密码**
  - 仅当两者为空时走旧逻辑（邮箱用户名 + `append_random_username_suffix` + `ensure_icris_password`）
- 这样 aggregator 预生成的 `Yingtai8492` / `Yingtai8492@1` 会原样传递到 ICRIS 注册表单

**密码合规验证**：`Yingtai8492@1` → 14位 ≥ 10 ✓ / 首 `'Y'` 大写 ✓ / 含字母 ✓ / 含数字 ✓ → 通过 `_password_meets_rules`，无需改规则。

---

### 改动 2：手持身份证照片字段

**文件 A：`src/materials/checklist.py`**
- `MATERIAL_FIELDS` 新增：
```python
MaterialField("id_card_handheld", "手持身份证明照片", field_type="file", required=False),
```
- 位置：插在 `id_card_back` 之后、`address_proof` 之前

**文件 B：`src/wework/material_handler.py`**
- `CLASSIFY_RULES` 新增一条（注意优先级：放在 `id_card_back` 之后、`address_proof` 之前）：
```python
(r"手持|hand.?held|手持身份证", "id_card_handheld"),
```

**文件 C：`src/materials/aggregator.py`**
- `_get_files()` 自动遍历 `MATERIAL_FIELDS` 的 file 类型字段，新增的 `id_card_handheld` 会自动纳入 `document_files`，无需额外改动

**文件 D：`templates/material_checklist.md`**
- 在"二、股东/创办成员资料"或"三、董事资料"下新增：
```
- [ ] 手持身份证明照片（正面+反面+手持拍照，护照则提供护照页）
```

---

### 改动 3：缺失字段补齐

**文件 A：`src/materials/checklist.py`**
- `MATERIAL_FIELDS` 新增 3 个文本字段（均可选）：
```python
MaterialField("registered_capital", "注册资本（港币）", required=False),
MaterialField("director_address_cn", "董事住址（中文）", required=False),
MaterialField("director_address_en", "董事住址（英文）", required=False),
```

**文件 B：`src/materials/aggregator.py`**
- `share_capital` 块从硬编码改为读材料：
```python
cap_str = _get_val(materials, "registered_capital", "10000")
try:
    cap_int = int(re.sub(r"\D", "", cap_str) or "10000")
except ValueError:
    cap_int = 10000
```
- `directors` 块增加住址：
```python
"directors": [
    {"name_en": ..., "email": ..., "address_cn": _get_val(materials, "director_address_cn"),
     "address_en": _get_val(materials, "director_address_en"), "raw": True}
] if ... else [],
```

**文件 C：`templates/material_checklist.md`**
- 新增"注册资本"到公司基本信息节、新增"董事住址（中英文）"到董事资料节

---

### 改动 4：材料清单文档 + 收集追踪

**文件 A（新建）：`docs/case_material_checklist.md`**
- 产出本案例的标准材料清单文档，分两部分：
  1. **本案例已提供信息映射表**：将用户提供的案例数据逐项映射到系统 field_key，标注"已提供/待收集"
  2. **ICRIS 账号生成规则说明**：用户名/密码格式 + 示例
- 敏感信息（身份证号）在文档中用掩码展示（如 `440514200003184***X`）

**文件 B：`src/materials/checklist.py`**
- `FIELD_PRIORITY` 新增必填/常用可选字段的优先级（确保主动提醒覆盖新字段）：
```python
"id_card_handheld": 12,
"registered_capital": 13,
```
- `progress_summary` / `prioritized_missing` / `format_progress_text` / `_maybe_proactive_reminder` 均自动遍历 `MATERIAL_FIELDS`，新字段加入后无需额外代码改动即被追踪

**追踪与提醒行为**（已有能力，新字段自动接入）：
- 客户发 `/进度` → `format_progress_text` 显示已收 N/总数 M、剩余必填项（含新字段）
- 客户连续发消息未补材料 → `_maybe_proactive_reminder`（优化12）按 `FIELD_PRIORITY` 主动提醒最缺失的 3 项
- 客户上传手持照片 → `material_handler.classify_by_filename` 命中 `手持` → 归类 `id_card_handheld` → `upsert_material` 入库 → 进度 +1

---

## 本案例数据 → 系统 field_key 映射

| 系统字段 | 案例数据 | 状态 |
|---|---|---|
| `company_name_cn` | 撼世全球有限公司 | 已提供 |
| `company_name_en` | Humsienk Global Limited | 已提供 |
| `registered_capital`（新增） | 10000 港币 | 已提供 |
| `business_desc` | 新能源產品、電子元器件銷售，電子商務，國際貿易 | 已提供 |
| `registered_office` | 香港新界葵涌葵喜街1-11号达利国际中心9楼909O | 已提供 |
| `directors` | 姚曉佳 | 已提供 |
| `founder_members` | 姚曉佳 | 已提供 |
| `id_type` | PRC_ID | 已提供 |
| `id_number` | 44051420000318492X | 已提供 |
| `director_address_cn`（新增） | 广东省深圳市南山区西丽南路8号110室 | 已提供 |
| `director_address_en`（新增） | Room 110, No. 8, Xili South Road... | 已提供 |
| `id_card_front` | 身份证正面 | 待收集（需上传） |
| `id_card_back` | 身份证反面 | 待收集（需上传） |
| `id_card_handheld`（新增） | 手持身份证拍照 | 待收集（需上传） |
| `contact_email` | — | 待收集 |
| `contact_phone` | — | 待收集 |
| `applicant_name` | — | 待收集 |
| `applicant_email` | — | 待收集 |
| `applicant_phone` | — | 待收集 |
| `company_secretary` | — | 待收集 |
| ICRIS username（自动生成） | Yingtai + 时间戳后4位 | 自动生成 |
| ICRIS password（自动生成） | username + @1 | 自动生成 |

---

## Assumptions & Decisions

1. **时间戳取后4位**：`str(int(time.time()))[-4:]`（Unix 秒级时间戳末4位）。每次注册调用 aggregator 时生成，同一流程内 `derive_icris_credentials` 的 `_icris_session` 缓存保证一致。
2. **密码含 `@` 特殊字符**：当前 `_password_meets_rules` 不限制特殊字符，`Yingtai8492@1` 合规。若 ICRIS 网站实际拒绝 `@`，回退方案：密码改为 `Yingtai8492A1`（将 `@` 替换为 `A`，仍满足首字母大写+字母+数字）。
3. **凭证模式可回退**：`icris_credential_mode="legacy"` 时完全恢复旧逻辑，零风险上线。
4. **新字段均为可选**：`id_card_handheld`/`registered_capital`/`director_address_cn`/`director_address_en` 设为 `required=False`，不阻塞现有注册流程。
5. **无 DB schema 改动**：材料表 key-value 结构天然支持新 field_key。
6. **PII 处理**：案例文档中身份证号用掩码展示；代码不硬编码案例数据。

---

## Verification Steps

1. **py_compile**：所有修改文件通过 `python -m py_compile`
2. **导入测试**：`from src.materials.checklist import MATERIAL_FIELDS` 确认新字段存在
3. **凭证生成单测**：
   - `mode="yingtai"` → username 以 "Yingtai" 开头、4位数字结尾；password = username + "@1"
   - `mode="legacy"` → username 含随机后缀、password 为姓名派生
   - `derive_icris_credentials` 传入预生成 icris_account → 原样返回不追加随机
4. **材料分类测试**：`classify_by_filename("手持身份证.jpg")` → `"id_card_handheld"`
5. **聚合测试**：`aggregate_company_data` 输出含 `id_card_handheld` 文件路径、`registered_capital` 来自材料值
6. **进度追踪测试**：填入案例已提供字段 → `progress_summary` 正确计数 received/missing，新字段在 missing 列表中
7. **密码合规**：`_password_meets_rules("Yingtai8492@1")` → True

---

## 涉及文件清单

| 文件 | 改动类型 |
|---|---|
| `config/settings.py` | 新增 4 个配置项 |
| `src/materials/checklist.py` | MATERIAL_FIELDS +4 字段、FIELD_PRIORITY +2 |
| `src/materials/aggregator.py` | icris_account 凭证生成 + share_capital 读材料 + director 住址 |
| `src/browser/icris_registration.py` | derive_icris_credentials 尊重预生成凭证 |
| `src/wework/material_handler.py` | CLASSIFY_RULES +1 手持规则 |
| `templates/material_checklist.md` | 清单模板 +3 项 |
| `docs/case_material_checklist.md` | 新建：案例材料清单文档 |
