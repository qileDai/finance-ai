# 案例材料清单 — 撼世全球有限公司 / Humsienk Global Limited

> 本文档基于实际注册案例，逐项映射到系统 `field_key`，标注「已提供 / 待收集」状态。
> 敏感信息（身份证号）以掩码展示。生成日期：2026-08-07。

---

## 一、已提供信息

| 系统字段 (field_key) | 标签 | 案例数据 | 状态 |
|---|---|---|---|
| `company_name_cn` | 拟用公司中文名 | 撼世全球有限公司 | ✅ 已提供 |
| `company_name_en` | 拟用公司英文名 | Humsienk Global Limited | ✅ 已提供 |
| `registered_capital` | 注册资本（港币） | 10000 | ✅ 已提供 |
| `business_desc` | 业务性质描述 | 新能源產品、電子元器件銷售，電子商務，國際貿易 | ✅ 已提供 |
| `registered_office` | 公司注册地址（香港） | 香港新界葵涌葵喜街1-11号达利国际中心9楼909O | ✅ 已提供 |
| `directors` | 董事资料 | 姚曉佳 | ✅ 已提供 |
| `founder_members` | 股东/创办成员资料 | 姚曉佳 | ✅ 已提供 |
| `id_type` | 身份证明类型 | PRC_ID（内地身份证） | ✅ 已提供 |
| `id_number` | 身份证明号码 | 440514200003184\*\*\*X | ✅ 已提供 |
| `director_address_cn` | 董事住址（中文） | 广东省深圳市南山区西丽南路8号110室 | ✅ 已提供 |
| `director_address_en` | 董事住址（英文） | Room 110, No. 8, Xili South Road, Nanshan District, Shenzhen City, Guangdong Province | ✅ 已提供 |

---

## 二、待收集材料

### 必填（身份证明文件，按证件类型提供）

本案例为内地身份证 (PRC_ID)，需 3 张：

| 系统字段 (field_key) | 标签 | 说明 | 状态 |
|---|---|---|---|
| `id_card_front` | 身份证明（正面） | 身份证正面照片 | ⬜ 待上传 |
| `id_card_back` | 身份证明（反面） | 身份证反面照片 | ⬜ 待上传 |
| `id_card_handheld` | 手持身份证明照片 | 手持身份证拍照照片 | ⬜ 待上传 |

### 证件材料说明

| 证件类型 | 需要收集的文件 |
|---|---|
| 内地身份证 (PRC_ID) | 正面 + 反面 + 手持拍照 |
| 香港身份证 (HKID) | 正面 + 反面 + 手持拍照（同内地身份证） |
| 护照 (PASSPORT) | 护照页 |

> 本案例董事姚曉佳使用内地身份证，需收集：正面、反面、手持拍照共 3 张照片。
> 证件类型 (`id_type`) 由上传证件时系统视觉识别自动判定，无需单独提供。

### 可选（按需提供，不收集也可进入注册）

| 系统字段 (field_key) | 标签 | 说明 |
|---|---|---|
| `contact_email` | 公司联络邮箱 | 用于公司联络 |
| `contact_phone` | 公司联络电话 | 香港电话号码 |
| `applicant_name` | ICRIS 申请人姓名 | ICRIS 账号由系统自动生成，可不提供 |
| `applicant_email` | ICRIS 申请人电邮 | 同上 |
| `applicant_phone` | ICRIS 申请人电话 | 同上 |
| `company_secretary` | 公司秘书资料 | 可为自然人或持牌秘书公司 |
| `address_proof` | 地址证明 | 注册地址证明 |
| `br_certificate_years` | 商业登记证有效期 | 1 或 3 年，默认 1 年 |

---

## 三、ICRIS 账号自动生成规则

系统在聚合材料时自动生成 ICRIS 注册账号凭证（无需客户提供）：

| 项目 | 规则 | 示例 |
|---|---|---|
| 用户名 (username) | `Yingtai` + 当前时间戳后4位 | `Yingtai8492` |
| 密码 (password) | 用户名 + `@1` | `Yingtai8492@1` |

**配置项**（`config/settings.py`）：

```python
icris_credential_mode: str = "yingtai"       # "yingtai" 新规则 | "legacy" 旧规则
icris_username_prefix: str = "Yingtai"       # 用户名前缀
icris_username_timestamp_digits: int = 4     # 时间戳取后 N 位
icris_password_suffix: str = "@1"            # 密码后缀
```

**密码合规性**：`Yingtai8492@1` 满足 ICRIS 密码规则（≥10位、首字母大写、含字母+数字）。

**一致性保证**：同一注册流程内，`derive_icris_credentials` 通过 `_icris_session` 缓存保证用户名/密码一致，不会因多次调用而变化。

---

## 四、收集进度追踪

系统自动追踪材料收集进度，客户可通过以下方式查看：

- 发送 `/进度` 或「进度」→ 显示已收 N/总数 M、剩余必填项
- 发送 `/资料` → 显示完整材料清单
- 连续发消息未补材料 → 系统按优先级主动提醒最缺失的 3 项（优化12）
- 上传手持照片 → 文件名含「手持」→ 自动归类 `id_card_handheld` → 进度 +1

**本案例进度**：已提供 11 项 / 总计 23 项；必填共 13 项（10 个文本字段 + 3 张身份证照片），必填剩余 3 项（身份证正面 / 反面 / 手持）。补齐 3 张证件照片后即可进入确认注册。
