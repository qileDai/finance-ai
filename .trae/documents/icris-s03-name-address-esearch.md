# ICRIS 注册：S03 姓名/地址智能化 + 跳过電子查冊 + 注册邮箱

## Summary（摘要）

针对 `python main.py --step register` 执行 ICRIS 账号注册，需做四件事：
1. **不勾选電子查冊**，且**不选主要账户**（用户确认两者都不选）。
2. **S03 电邮字段**改用 `MATERIALS_DEFAULT_CONTACT_EMAIL` 设置值（固定注册邮箱，非客户邮箱）。
3. **S03 姓名**：英文姓名→填英文姓氏+英文名字；纯中文→只填中文姓名；中英都有→都填。
4. **S03 地址**：用真实董事住址（非 mock）；自动判断香港/非香港→勾选本地/非香港地址；**不填**「室／樓／座」与「大廈」；按「末 2 段=區/市/省」拆分填「街道」与「區/市/省/州/郵遞區號」（英文优先、无英文用中文，中文亦拆分）；非香港→國家／地區选「中国」。

## Current State Analysis（现状）

- [main.py:156-167](file:///d:/projects/finance-ai/main.py#L156-L167) `--step register` → `StepName.ICRIS_REGISTER` → `agent.workflow.run_step`，company_data 来自 `load_company_data_from_roomid` 或 `load_mock_data`。
- [icris_registration.py:2990-3162](file:///d:/projects/finance-ai/src/browser/icris_registration.py#L2990-L3162) `_fill_user_info_step`（S03）：
  - L3012 `given, surname = split_applicant_english_name(applicant.get("name_en",""))` — 只处理英文姓名，中文姓名「留空不填」（L3031 注释）。
  - L3015 `email = applicant.get("email","")` — 取 applicant.email。
  - L3017 `addr = derive_mock_china_address(applicant)` — **用 mock 地址**，且 applicant 无 address 字段，永远落到 L115-120 硬编码 mock。
  - L3058-3065 地址 radio：先试「本地地址」再回退「非香港地址」——**未按地址判断**，顺序错误。
  - L3079-3087 填「室/楼/座」「大厦」「街道」三字段。
  - L3089-3097 区/市/省→选「香港仔」（HK 下拉），非 HK 文本场景未处理。
  - L3099-3108 国家/地区→「中国」（条件 `.ant-select>1`，较脆）。
- [icris_registration.py:95-120](file:///d:/projects/finance-ai/src/browser/icris_registration.py#L95-L120) `split_applicant_english_name`、`derive_mock_china_address`。
- 電子查冊 + 主要账户耦合：
  - [L1841-1855](file:///d:/projects/finance-ai/src/browser/icris_registration.py#L1841-L1855) `_fill_account_profile_native`：`_select_principal_account_after_search` 先勾電子查冊(value=search)再选主要账户(serviceType=principal)。
  - [L1825-1839](file:///d:/projects/finance-ai/src/browser/icris_registration.py#L1825-L1839) `_select_principal_account_after_search`；[L1798-1823](file:///d:/projects/finance-ai/src/browser/icris_registration.py#L1798-L1823) `_wait_for_principal_account_enabled` 要求 `search.checked`。
  - [L2720-2736](file:///d:/projects/finance-ai/src/browser/icris_registration.py#L2720-L2736) Ant 回退路径 `checkbox_steps` 含「电子查册」，并调 `_select_primary_account_radio`。
- [aggregator.py:110,178-182](file:///d:/projects/finance-ai/src/materials/aggregator.py#L110-L182) `applicant.name_en = applicant_name or person`、`name_cn = person`——person 为中文时被错误塞入 name_en。
- [aggregator.py:162-168](file:///d:/projects/finance-ai/src/materials/aggregator.py#L162-L168) `directors[0].address_cn/en` 来自 `director_address_cn/en` 材料（真实地址已就绪）。
- [settings.py:143-145](file:///d:/projects/finance-ai/config/settings.py#L143-L145) `materials_default_contact_email`（env `MATERIALS_DEFAULT_CONTACT_EMAIL`）已存在。

## Proposed Changes（改动）

### 1. `src/browser/icris_registration.py` — 新增 3 个辅助函数（L120 附近）
```python
def split_cjk_latin_name(name: str) -> tuple[str, str]:
    """姓名 → (中文部分, 英文部分)。
    纯中文→(name,"")；纯英文→("",name)；混合→(CJK片段, 拉丁片段)。"""

def detect_hk_address(addr_en: str, addr_cn: str) -> bool:
    """含 香港/Hong Kong/Kowloon/九龍/新界/New Territories → True。"""

def split_address_street_region(address: str) -> tuple[str, str]:
    """地址 → (街道, 区/市/省)。
    含拉丁字母：按逗号分段，末2段→区/市/省，其余→街道（<3段时末1段→区/市/省）。
    纯中文：正则提取 省/自治区+市 为区/市/省，其余为街道；无省则取市。
    兜底：全部归街道。"""
```

### 2. `src/browser/icris_registration.py` — `_fill_user_info_step`（S03）重写姓名/邮箱/地址段
- **姓名**（L3011-3017, L3031-3039）：
  - `name_en = applicant.get("name_en","")`；`name_cn = applicant.get("name_cn","")`
  - `given, surname = split_applicant_english_name(name_en)` 仅当 name_en 非空
  - 填充循环增加 `(r"中文姓名|中文姓名|中文", name_cn, "中文姓名")`；`if not val: continue` 跳过空值
- **邮箱**（L3015）：`email = (getattr(settings,"materials_default_contact_email","") or "").strip() or applicant.get("email","")`
- **地址**（L3017, L3058-3108）：
  - `director = (data.get("directors") or [{}])[0] or (data.get("founder_members") or [{}])[0]`
  - `addr_en = director.get("address_en","")`；`addr_cn = director.get("address_cn","")`；`addr_text = addr_en or addr_cn`
  - `is_hk = detect_hk_address(addr_en, addr_cn)`；`street, region = split_address_street_region(addr_text)`
  - radio：`is_hk`→勾「本地地址」；否则→勾「非香港地址」
  - **删除**「室/楼/座」「大厦」填充（仅保留「街道」填 `street`）
  - 区/市/省：`is_hk`→保持选 HK 下拉（现状「香港仔」）；非 HK→用 `_fill_by_placeholder(r"區.*市.*省|區市省|州|郵遞", region)` 填文本（回退 label）
  - 国家/地区：仅 `not is_hk` 时选「中国」（保持现状选择逻辑）
- **Vue id_map 兜底**（L3137-3144）增加 `("#nameCh, input[name*='nameCh' i], input[id*='chineseName' i]", name_cn, "text")`

### 3. `src/browser/icris_registration.py` — 跳过電子查冊 + 主要账户
- [L1841-1855](file:///d:/projects/finance-ai/src/browser/icris_registration.py#L1841-L1855) `_fill_account_profile_native`：保留「用户类别=个人」「电子提交(filing)」；用 `if not settings.icris_skip_esearch_principal:` 包裹 `_select_principal_account_after_search` 调用（默认跳过）。
- [L2720-2736](file:///d:/projects/finance-ai/src/browser/icris_registration.py#L2720-L2736) Ant 回退：`checkbox_steps` 仅保留「电子提交」；用同一开关包裹 `_select_primary_account_radio`。

### 4. `config/settings.py` — 新增开关
- `icris_skip_esearch_principal: bool = True`（默认跳过電子查冊+主要账户；False 回退旧行为）

### 5. `src/materials/aggregator.py` — 修正 applicant 中英姓名
- [L110-111,178-181](file:///d:/projects/finance-ai/src/materials/aggregator.py#L110-L181)：
  - `person = _director_name(materials)`；`person_cn, person_en = split_cjk_latin_name(person)`（从 icris_registration 导入或内置同款实现）
  - `applicant_name_raw = _get_val(materials,"applicant_name")`；若提供则 `am_cn, am_en = split_cjk_latin_name(applicant_name_raw)` 合并
  - `"applicant": {"name_en": person_en or am_en, "name_cn": person_cn or am_cn, ...}`（其余 email/phone 不变）
- founder_members/directors 的 `name_en=person` 保持不变（属后续 s04+ 范围，本次不动）

## Assumptions & Decisions（假设与决策）

- **電子查冊 + 主要账户都不选**（用户确认）；保留「电子提交(filing)」与「用户类别=个人」。开关默认开，可回退。
- **注册邮箱**固定用 `MATERIALS_DEFAULT_CONTACT_EMAIL`（设置空时回退 applicant.email），仅改 S03，不动 aggregator 邮箱链。
- **姓名三态**：纯中→只填中文姓名；纯英→只填英文姓氏+英文名字；混合→都填。靠 CJK/拉丁字符检测。
- **地址拆分「末 2 段=區/市/省」**：英文按逗号；中文按 省+市 提取为區/市/省，其余为街道（与英文「城市+省份→区域、含区→街道」对齐）。用户案例：街道="Room 110, No. 8, Xili South Road, Nanshan District"，區/市/省="Shenzhen City, Guangdong Province"。
- **室/樓/座 + 大廈 不填**（用户要求），仅填街道与區/市/省。
- **HK 检测**：含香港/Hong Kong/Kowloon/九龍/新界 → 本地地址；否则非香港+國家=中国。用户案例为非 HK。
- **HK 分支区/市/省**暂保持选「香港仔」（用户案例非 HK，不重点改；后续可按注册地址提取区域）。
- **不动**：main.py、checklist.py、material_handler.py、captcha_ollama.py。

## Verification（验证）

1. `py_compile` icris_registration.py、aggregator.py、settings.py。
2. 纯逻辑冒烟（`.venv`，不连浏览器）：
   - `split_cjk_latin_name("姚曉佳")`→("姚曉佳","")；`("Yau Siu Ka")`→("","Yau Siu Ka")；`("姚曉佳 Yau Siu Ka")`→("姚曉佳","Yau Siu Ka")。
   - `detect_hk_address("Room 110...Shenzhen","广东省深圳市")`→False；`("...Hong Kong","香港新界葵涌")`→True。
   - `split_address_street_region("Room 110, No. 8, Xili South Road, Nanshan District, Shenzhen City, Guangdong Province")`→街道="Room 110, No. 8, Xili South Road, Nanshan District"、區="Shenzhen City, Guangdong Province"。
   - `split_address_street_region("广东省深圳市南山区西丽南路8号110室")`→區含"广东省深圳市"、街道含"南山区西丽南路8号110室"。
   - aggregator：directors="姚曉佳" → applicant.name_cn="姚曉佳"、name_en=""。
3. `_fill_account_profile_native`：用桩 page 验证 `icris_skip_esearch_principal=True` 时不调 `_check_native_checkbox_by_value("search")`、不选 serviceType=principal；False 时恢复。
4. 端到端（需浏览器，可选/dry_run）：`python main.py run --step register`，确认 S03 中文姓名填入、地址非香港+街道/區拆分+國家=中国、電子查冊与主要账户未勾选、邮箱=MATERIALS_DEFAULT_CONTACT_EMAIL。
