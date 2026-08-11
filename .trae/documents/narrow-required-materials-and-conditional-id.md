# 收窄必填材料 & 条件化身份证明文档

## Context（背景）

用户针对「撼世全球有限公司 / Humsienk Global Limited」注册案例，要求把材料收集的**必填项**收窄到案例实际需要的字段，其余改为可选；身份证明文件按**证件类型**条件化要求；并更新进度跟踪逻辑与对客模板/案例文档。

> 重要核查结论：上一会话曾声称完成本变更，但实测 `src/materials/checklist.py` 仍为**原始版本**（无 `_is_field_required`、`total_required` 仍为静态 `len(REQUIRED_FIELD_KEYS)`、`FIELD_PRIORITY` 为旧版），变更**未落盘**。本次按案例实际实施。

ICRIS 账号凭证（用户名=`Yingtai`+时间戳后4位，密码=用户名+`@1`）已由 `aggregator._generate_icris_credentials()` + `settings.icris_credential_mode="yingtai"` 实现，**无需向用户收集、无需改动**。

## 必填字段定义

**10 个文本必填：**
1. `company_name_en`（英文名 Humsienk Global Limited）
2. `company_name_cn`（中文名 撼世全球有限公司）
3. `registered_office`（注册地址）
4. `registered_capital`（注册资本，1 万港币）
5. `business_desc`（业务性质：新能源產品、電子元器件銷售，電子商務，國際貿易）
6. `directors`（董事：姚曉佳）
7. `founder_members`（股东/创办成员：姚曉佳）
8. `id_number`（身份证明号码：44051420000318492X）
9. `director_address_cn`（董事住址中文）
10. `director_address_en`（董事住址英文）

**条件化文档（依 `id_type`）：**
- `id_type ∈ {"", "PRC_ID", "HKID"}` → `id_card_front` + `id_card_back` + `id_card_handheld`（正/反/手持 3 张）
- `id_type == "PASSPORT"` → `passport`（护照页 1 张）
- `id_type` 本身**可选**，由上传证件时视觉识别自动写入（`material_handler` 已支持）

**改为可选：** `contact_email`、`contact_phone`、`company_secretary`、`applicant_name/email/phone`、`br_certificate_years`、`address_proof`

## 改动清单

### 1. `src/materials/checklist.py`（核心）
- 调整 `MATERIAL_FIELDS` 的 `required` 标志：
  - 改为必填：`company_name_cn`、`id_number`、`registered_capital`、`director_address_cn`、`director_address_en`
  - 改为可选：`contact_email`、`contact_phone`、`company_secretary`、`applicant_name`、`applicant_email`、`applicant_phone`
  - 文档字段 `id_card_front/back/handheld`、`passport` 静态 `required=False`，交给动态判定
- 新增 `_is_field_required(field, materials)`：文档字段按 `id_type` 判定（复用已有 `_field_value`），其余取 `field.required`
- `progress_summary`：`total_required` 与 `missing` 改用 `_is_field_required`（替换第 143-147 行静态逻辑）
- `prioritized_missing`：过滤条件由 `if not f.required` 改为 `if not _is_field_required(f, materials)`
- `FIELD_PRIORITY` 更新为新必填顺序（company_name_en→cn→registered_office→directors→founder_members→id_number→business_desc→registered_capital→director_address_cn→en→id_card_front→back→handheld→passport）；`CRITICAL_FIELD_KEYS` 保留 `{"company_name_en","registered_office","directors"}`

### 2. `templates/material_checklist.md`（对客清单）
- 重构为「必填」「可选」两段
- 身份证明按证件类型分述（内地/香港身份证=正面+反面+手持；护照=护照页）
- 加注：ICRIS 账号由系统自动生成（用户名/密码无需提供）

### 3. `docs/case_material_checklist.md`（案例文档）
- 按新必填集更新「待收集」项，联系/秘书/申请人等标为可选
- 写入案例具体数据（中英文名、注册资本、经营范围、注册地址、董事+股东姚曉佳、身份证号、住址中英文）
- 条件文档说明

### 4. `src/materials/form_parser.py`
- `parse_registration_form` 的 `required` 列表对齐新必填集（移除 `contact_email`），避免 H5 表单在可选字段上误报缺失

## 不改动（已符合需求 / 自动联动）
- `src/materials/aggregator.py`：`_generate_icris_credentials` 已符合；`is_ready_for_confirm` 经 `progress_summary` 自动联动新必填集
- `src/wework/group_state_machine.py`：复用 checklist 函数，进度/复核/主动提醒自动生效
- `src/wework/material_handler.py`：分类规则与视觉识别已支持 passport/id_card，会自动写入 `id_type`
- `config/settings.py`：`icris_*` 已配置

## 验证
1. `py_compile` 改动文件（checklist.py、form_parser.py）
2. 纯逻辑冒烟（`.venv` Python）：
   - `progress_summary({})` → `missing_labels` 含 10 文本 + 3 个 id_card_*（默认 PRC_ID），`total_required==13`
   - 填入 `id_type=PASSPORT` → missing 含 `passport`，不含 `id_card_*`
   - 全部必填收齐 + `id_type=PRC_ID` + 3 张证件 → `complete==True`，`is_ready_for_confirm==True`
3. `prioritized_missing` 在不同 `id_type` 下返回正确缺失项
4. `parse_registration_form` 在缺 `contact_email` 时不再报缺失
