# 台湾身份证识别失败修复

## 目标

修复台湾身份证（TW_ID）识别不出来的问题，从 prompt、正则、ok 逻辑三方面修复。

## 根因分析

### 根因 1：Prompt 没描述卡面外观（最严重）

[id_document_vision.py:772-774](file:///d:/projects/finance-ai/src/materials/id_document_vision.py#L772-L774) TW_ID prompt 只写了格式规则：
```
"- TW_ID: name_cn=姓名（原文，禁繁简转换，不可留空）；"
"id_number=字母+[1或2]+8位数字；address_cn=户籍地/住址（多行拼一行）；"
```

**没有描述卡面外观**，LLM 不知道台湾身份证长什么样 → 可能误判为 unknown / HKID / SCREENSHOT。

台湾身份证外观（来自搜索）：
- 粉紫色渐层背景，横式
- 正面标题「中華民國國民身分證」
- 正面有：姓名、身分證字號（字母+9位数字）、出生年月日（民国纪年）、性别、相片
- 背面有：配偶姓名、父母姓名
- 繁体中文

### 根因 2：正则只允许性别码 1/2

[id_document_vision.py:51](file:///d:/projects/finance-ai/src/materials/id_document_vision.py#L51)：
```python
_TW_ID_RE = re.compile(r"^[A-Z][12]\d{8}$", re.I)
```

台湾身份证第 2 位（性别码）可以是：
- 1=男性, 2=女性
- 6=取得国籍之外国人, 7=无户籍国民, 8=港澳居民, 9=大陆地区人民

当前正则不支持 6/7/8/9，外籍居民台证号会被判 `number_ok=False`。

虽然有 salvage 兜底 `(len>=8 and isalnum())`，但 `ok` 逻辑在 salvage 之前已判 False。

### 根因 3：ok 逻辑过于严格

[id_document_vision.py:685-686](file:///d:/projects/finance-ai/src/materials/id_document_vision.py#L685-L686)：
```python
if raw_type in (ID_TYPE_HKID, ID_TYPE_PRC, ID_TYPE_TW) and side != "back" and not number_ok:
    ok = False
```

TW_ID 号码校验不过时直接 `ok=False`，但台证号码有 checksum 验证，LLM 可能识别错一个数字就导致校验失败。此时姓名、住址都有值，却被判失败。

---

## 改动方案

### 改动 1：Prompt 加台湾身份证卡面描述

**文件**：[src/materials/id_document_vision.py](file:///d:/projects/finance-ai/src/materials/id_document_vision.py)

TW_ID prompt 部分改为：
```
"- TW_ID: 台湾国民身份证（粉紫色卡面，标题「中華民國國民身分證」，横式，繁体中文）。\n"
"  name_cn=姓名（原文，禁繁简转换，不可留空）；\n"
"  id_number=身分證字號（1字母+9位数字，共10位，如A123456789）；\n"
"  address_cn=户籍地/住址（多行拼一行）；\n"
"  若有 address_cn 同时输出 address_en=英文住址\n"
```

### 改动 2：放宽 _TW_ID_RE 正则

**文件**：[src/materials/id_document_vision.py](file:///d:/projects/finance-ai/src/materials/id_document_vision.py) 第 51 行

```python
# 原
_TW_ID_RE = re.compile(r"^[A-Z][12]\d{8}$", re.I)
# 改为：支持性别码 1/2/6/7/8/9
_TW_ID_RE = re.compile(r"^[A-Z][126789]\d{8}$", re.I)
```

同步修改 `normalize_id_number` 中 TW 分支的搜索正则（第 340 行）：
```python
# 原
m = re.search(r"[A-Z][12]\d{8}", cand)
# 改为
m = re.search(r"[A-Z][126789]\d{8}", cand)
```

同步修改 `id_extract.py` 中台证号码宽松保留正则（第 131、643、655 行等）：
```python
# 原 _re.match(r"^[A-Z][12]\d{8}$", ...)
# 改为 _re.match(r"^[A-Z][126789]\d{8}$", ...)
```

### 改动 3：TW_ID ok 逻辑放宽

**文件**：[src/materials/id_document_vision.py](file:///d:/projects/finance-ai/src/materials/id_document_vision.py) 第 685-686 行

```python
# 原：号码不过直接 False
if raw_type in (ID_TYPE_HKID, ID_TYPE_PRC, ID_TYPE_TW) and side != "back" and not number_ok:
    ok = False

# 改为：TW_ID 有姓名或住址时不算失败（号码可后补）
if raw_type in (ID_TYPE_HKID, ID_TYPE_PRC) and side != "back" and not number_ok:
    ok = False
if raw_type == ID_TYPE_TW and side != "back" and not number_ok:
    # 台证号码校验严（有 checksum），号码不过但姓名/住址有值时仍返回结果
    ok = bool(conf_ok and (name_cn or address_cn))
```

### 改动 4：Prompt 示例更新

JSON 示例保持 TW_ID 开头，但补一个更完整的示例让 LLM 理解格式。

---

## 改动文件清单

| 文件 | 改动 |
|---|---|
| [id_document_vision.py](file:///d:/projects/finance-ai/src/materials/id_document_vision.py) | prompt 加卡面描述 + 放宽正则 + 放宽 ok 逻辑 |
| [id_extract.py](file:///d:/projects/finance-ai/src/materials/id_extract.py) | 同步正则放宽 |

## 验证步骤

### 1. py_compile
```powershell
.venv\Scripts\python.exe -m py_compile src\materials\id_document_vision.py src\materials\id_extract.py
```

### 2. 逻辑验证
- 上传台湾身份证 → 应识别为 TW_ID，提取姓名、号码、住址
- 号码校验不过时，仍有姓名/住址返回（不直接失败）
- 外籍居民台证号（性别码 8/9）不被正则拒绝
