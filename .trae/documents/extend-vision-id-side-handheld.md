# 扩展 OpenAI 视觉：识别身份证正反面 + 手持拍照

## Summary（摘要）

当前系统对身份证图片仅识别「证件类型 + 号码」，正反面/手持判定完全依赖**文件名**正则（`手持|hand.?held`、`背面|back`）。而视觉识别的 `file_field_key` 对 HKID/PRC_ID 一律返回 `id_card_front`，会**覆盖**文件名判定的 `id_card_handheld`，导致手持照片被错误归为正面。

本方案在**现已读取类型+号码的同一个 OpenAI Vision 调用**中，增加「正反面 + 是否手持」判定，并据此输出真实 `file_field_key`；同时修正 `material_handler` 的覆盖逻辑，让视觉判定优先、文件名仅作兜底。无额外 API 调用、无新依赖、PII 流向不变（图片本就已发给 OpenAI）。

## Current State Analysis（现状分析）

- [id_document_vision.py:147-226](file:///d:/projects/finance-ai/src/materials/id_document_vision.py#L147-L226) `recognize_id_document`：OpenAI Vision prompt 仅问 `id_type`/`id_number`/`confidence`。
- [id_document_vision.py:51-56](file:///d:/projects/finance-ai/src/materials/id_document_vision.py#L51-L56) `file_field_key` 属性：`PASSPORT→passport`、`HKID/PRC_ID→id_card_front`（永远正面）、其余 `unknown`。无正反面/手持区分。
- [material_handler.py:222-227](file:///d:/projects/finance-ai/src/wework/material_handler.py#L222-L227) 覆盖 bug：`is_id` 时 `vision.file_field_key`（恒为 `id_card_front`）覆盖文件名得到的 `id_card_handheld`；仅 `id_card_back` 受 `if field_key not in ("id_card_back",)` 保护。
- [material_handler.py:28-34](file:///d:/projects/finance-ai/src/wework/material_handler.py#L28-L34) `CLASSIFY_RULES`：文件名正则分类（手持/身份证/护照/地址/背面）。
- Ollama 本地视觉仅用于验证码（[captcha_ollama.py](file:///d:/projects/finance-ai/src/browser/captcha_ollama.py)），未用于证件；本次不涉及。

## Proposed Changes（改动）

### 1. `src/materials/id_document_vision.py`（核心）

- `IdDocumentResult` 新增字段：`side: str = ""`（`"front"`|`"back"`|`""`）、`is_handheld: bool = False`。
- 更新 `recognize_id_document` 的 prompt（[L172-179](file:///d:/projects/finance-ai/src/materials/id_document_vision.py#L172-L179)）：在原 `id_type`/`id_number`/`confidence` 基础上，增加：
  - `side`：`front`（有照片/人像面）/ `back`（国徽/反面）/ `""`（护照或无法判断）
  - `is_handheld`：`true`（图中有人手持证件）/ `false`
  - 输出 JSON 示例相应扩展。
- `_parse_vision_payload`（[L117-144](file:///d:/projects/finance-ai/src/materials/id_document_vision.py#L117-L144)）：解析 `side`（小写归一、仅接受 front/back/空）、`is_handheld`（bool 归一）。
- `file_field_key` 属性改为按 `id_type` + `is_handheld` + `side` 计算：
  - `PASSPORT → passport`
  - `HKID/PRC_ID`：`is_handheld → id_card_handheld`；`side=="back" → id_card_back`；`side=="front" → id_card_front`；`side` 空 → `id_card_front`（默认正面）
  - 其余 → `unknown`
- 新增设置开关 `wework_id_vision_side_classify_enabled: bool = True`（见下）。关闭时 `file_field_key` 回退旧的「仅按类型」映射，prompt 仍可带新字段但忽略。

### 2. `config/settings.py`

- 新增 `wework_id_vision_side_classify_enabled: bool = True`（视觉正反面/手持判定开关，默认开；关闭则回退旧行为）。

### 3. `src/wework/material_handler.py`（修覆盖 bug）

- 重写 [L222-227](file:///d:/projects/finance-ai/src/wework/material_handler.py#L222-L227) 的 `is_id` 分支：**视觉 `file_field_key` 优先**；仅当其为 `unknown` 时回退文件名 `filename_key`（且仅取 `id_card_front/id_card_back/id_card_handheld/passport` 之一，否则默认 `id_card_front`）。
- 删除旧的 `if field_key not in ("id_card_back",)` 保护（视觉现已能判 back，不再需要文件名特例）。
- `CLASSIFY_RULES` 保留（视觉关闭/硬错误时仍作兜底）。
- 日志补充 `side`/`is_handheld`/`field_key` 便于排查。

### 4. `notify_classification`（material_handler.py L287+）

- 对客提示语微调：当落盘字段为 `id_card_handheld` 时提示「已收到手持证件照」；`id_card_back` 提示「已收到反面」。其余沿用现有话术。（仅文案，无逻辑变化。）

## Assumptions & Decisions（假设与决策）

- **方案**：用户选定「扩展现有 OpenAI Vision」（非本地 Ollama）。PII 流向不变——图片本已发给 OpenAI 读类型/号码，本次仅在同一调用加问正反面/手持，无额外调用、无新增第三方。
- **手持优先于正反面**：手持拍照通常展示正面，但 `id_card_handheld` 是独立必填槽位，故 `is_handheld=True` 一律归 `id_card_handheld`，不再看 `side`。
- **side 空默认正面**：模型判不出正反面时默认 `id_card_front`（多数上传为正面），避免丢件。
- **信任视觉判定**：GPT-4o 对正反面/手持识别可靠；状态 `needs_review` 仍保留人工复核通道。
- **回退**：视觉硬错误（vision_disabled/no_api_key/not_image）或 API 失败时，沿用文件名分类，不丢件。
- **不改动**：`captcha_ollama.py`、`aggregator.py`、`checklist.py`（上一任务已收窄必填，本次自动联动：PRC_ID/HKID 需 front+back+handheld，PASSPORT 需 passport）。

## Verification（验证）

1. `py_compile` id_document_vision.py、material_handler.py、settings.py。
2. 纯逻辑冒烟（`.venv`，不调真实 OpenAI）：
   - 构造 payload `{"id_type":"PRC_ID","id_number":"44051420000318492X","confidence":0.9,"side":"front","is_handheld":false}` → `file_field_key=="id_card_front"`、`ok==True`。
   - `side:"back"` → `id_card_back`；`is_handheld:true` → `id_card_handheld`（即使 side=front）。
   - `id_type:"PASSPORT"` → `passport`（忽略 side/handheld）。
   - 关闭 `wework_id_vision_side_classify_enabled` → `file_field_key` 回退为 `id_card_front`（旧行为）。
3. material_handler 选择逻辑（用桩 `recognize_id_document` 返回带 side/handheld 的结果）：手持图片 → 落盘 `id_card_handheld`（不再被覆盖为 front）；反面 → `id_card_back`。
4. 端到端（需 OpenAI key，可选）：上传一张手持身份证照，确认入库 `field_key=id_card_handheld`、`id_type` 写入、进度 +1。
