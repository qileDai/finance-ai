# 姓名繁简转换逻辑修正

## 目标

确保证件姓名繁简转换规则一致：
- **国内身份证（PRC_ID）**：姓名转繁体
- **香港身份证（HKID）**：识别原文原样，不转
- **台湾身份证（TW_ID）**：识别原文原样，不转
- **截图/普通图片（SCREENSHOT）**：识别原文原样，不转

## 当前状态分析

姓名转繁体逻辑分布在 **3 个文件 3 个层级**，存在冗余和隐患：

### 层级 1：`_parse_vision_payload`（[id_document_vision.py:606-609](file:///d:/projects/finance-ai/src/materials/id_document_vision.py#L606-L609)）

```python
# 仅国内身份证 / 国内护照姓名转繁体；港台及其他护照原样
if name_cn and should_convert_name_to_traditional(raw_type, issuing_country):
    name_cn = to_traditional_name(name_cn)
    full_name = display_name(name_cn, name_en, full_name)
```

**问题**：此处在 Vision 解析阶段就做了繁体转换，但此时**不知道用户指定的 `expected_id_type`**。如果 LLM 把香港身份证误判为 PRC_ID，姓名会被错误转繁体，下游的 guard 无法撤销。

### 层级 2：`id_extract.py`（[id_extract.py:166-174](file:///d:/projects/finance-ai/src/materials/id_extract.py#L166-L174)）

```python
tid = vision.id_type if vision.id_type not in ("", ID_TYPE_UNKNOWN) else expected
if tid in ("HKID", ID_TYPE_TW, ID_TYPE_SCREENSHOT) or expected in ("HKID", ID_TYPE_TW, ID_TYPE_SCREENSHOT):
    pass
elif should_convert_name_to_traditional(tid, vision.issuing_country):
    vision.name_cn = to_traditional_name(vision.name_cn)
```

**状态**：已有 HKID/TW_ID/SCREENSHOT guard，逻辑正确，但与层级 1 冗余（PRC_ID 被转两次，虽 no-op 但浪费）。

### 层级 3：`admin_runner.py`（[admin_runner.py:311-326](file:///d:/projects/finance-ai/src/web/admin_runner.py#L311-L326)）

```python
if vision.id_type in (ID_TYPE_HKID, ID_TYPE_TW) or hint_type in (ID_TYPE_HKID, ID_TYPE_TW):
    pass
elif should_convert_name_to_traditional(vision.id_type, issuing) or should_convert_name_to_traditional(hint_type, issuing):
    vision.name_cn = to_traditional_name(vision.name_cn)
```

**问题**：缺少 SCREENSHOT guard（虽然 `should_convert_name_to_traditional("SCREENSHOT")` 返回 False 不会误转，但不显式）；且未 import `ID_TYPE_SCREENSHOT`。

## 改动方案

**核心思路**：移除层级 1（`_parse_vision_payload`）中的繁体转换，让姓名保持原文；仅在层级 2（`id_extract.py`）和层级 3（`admin_runner.py`）做转换——这两处同时知道 vision 类型和用户指定类型，能正确 guard。

### 改动 1：移除 `_parse_vision_payload` 中的繁体转换

**文件**：[src/materials/id_document_vision.py](file:///d:/projects/finance-ai/src/materials/id_document_vision.py)

删除 606-609 行的转换块：
```python
# 删除以下 3 行
if name_cn and should_convert_name_to_traditional(raw_type, issuing_country):
    name_cn = to_traditional_name(name_cn)
    full_name = display_name(name_cn, name_en, full_name)
```

**原因**：
- 此处无 `expected_id_type` 上下文，LLM 误判时无法 guard
- 转换逻辑由下游 `id_extract.py` / `admin_runner.py` 负责，它们有完整上下文
- 消除冗余的双重转换

### 改动 2：`admin_runner.py` 补 SCREENSHOT guard

**文件**：[src/web/admin_runner.py](file:///d:/projects/finance-ai/src/web/admin_runner.py)

**改动点 1**：import 加 `ID_TYPE_SCREENSHOT`：
```python
from src.materials.id_document_vision import (
    ID_TYPE_HKID,
    ID_TYPE_PASSPORT,
    ID_TYPE_PRC,
    ID_TYPE_SCREENSHOT,   # 新增
    ID_TYPE_TW,
    recognize_id_document,
)
```

**改动点 2**：guard 条件加 SCREENSHOT（311-316 行）：
```python
# 原
if vision.id_type in (ID_TYPE_HKID, ID_TYPE_TW) or hint_type in (
    ID_TYPE_HKID, ID_TYPE_TW,
):
    pass
# 改为
if vision.id_type in (ID_TYPE_HKID, ID_TYPE_TW, ID_TYPE_SCREENSHOT) or hint_type in (
    ID_TYPE_HKID, ID_TYPE_TW, ID_TYPE_SCREENSHOT,
):
    pass
```

### 不改动的部分

- `should_convert_name_to_traditional` 函数：逻辑正确（PRC_ID=True, PASSPORT+CHN=True, 其余=False），不改
- `to_traditional_name` 函数：不改
- `id_extract.py`：已有完整的 HKID/TW_ID/SCREENSHOT guard，不改

## 改动文件清单

| 文件 | 操作 | 改动 |
|---|---|---|
| [id_document_vision.py](file:///d:/projects/finance-ai/src/materials/id_document_vision.py) | 删除 3 行 + 改 1 函数 | 移除 `_parse_vision_payload` 中的繁体转换；`should_convert_name_to_traditional` 加 SCREENSHOT=True |
| [id_document_translate.py](file:///d:/projects/finance-ai/src/materials/id_document_translate.py) | 改 1 行 | `enrich_extracted_fields` 英文名生成条件加 SCREENSHOT |
| [id_extract.py](file:///d:/projects/finance-ai/src/materials/id_extract.py) | 修改 | 移除 SCREENSHOT guard（让繁体转换执行）+ 截图设 id_type 供 enrich 生成英文名 |
| [admin_runner.py](file:///d:/projects/finance-ai/src/web/admin_runner.py) | 修改 | import 加 ID_TYPE_SCREENSHOT + 截图设 id_type 供 enrich 用 |

## 预期效果

| 证件类型 | 改动前 | 改动后 |
|---|---|---|
| PRC_ID | 转繁体（层级1转 + 层级2重复转） | 转繁体（仅层级2/3转一次） |
| HKID | 不转（层级1不转 + 层级2 guard） | 不转（仅层级2/3 guard） |
| TW_ID | 不转（层级1不转 + 层级2 guard） | 不转（仅层级2/3 guard） |
| SCREENSHOT | 不转（层级1不转 + 层级2 guard） | 简体转繁体 + 生成英文姓名 |
| LLM 误判 HK→PRC | **层级1错误转繁，无法撤销** | 层级2 guard 检测 expected=HKID，不转 |

## 验证步骤

### 1. py_compile
```powershell
.venv\Scripts\python.exe -m py_compile src\materials\id_document_vision.py src\web\admin_runner.py src\materials\id_extract.py src\materials\id_document_translate.py
```

### 2. 逻辑验证
- 上传内地身份证 → 姓名应为繁体（如「张三」→「張三」）
- 上传香港身份证 → 姓名保持原文（LLM 输出什么就是什么）
- 上传台湾身份证 → 姓名保持原文
- 上传聊天截图 → 简体姓名转繁体 + 生成英文姓名；繁体姓名不转 + 同样生成英文姓名
