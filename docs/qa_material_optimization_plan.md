# Q&A 系统与材料收集模块优化实施方案

## 概述

本方案针对香港公司注册助手的 Q&A 问答系统和材料收集模块，提出 12 项具体优化。每项优化均基于代码实际审查和业界最佳实践研究，指定修改文件、具体改动、关键实现细节和新增配置项，执行者可直接实施无需再做架构决策。

### 优化项总览

| 编号 | 优化项 | 模块 | 修改文件数 |
|------|--------|------|-----------|
| 1 | MMR 多样性重排序 | Q&A | 2 |
| 2 | 多策略重试机制 | Q&A | 3 |
| 3 | Embedding 忠实度检查 | Q&A | 2 |
| 4 | 回答一致性控制 | Q&A | 4 |
| 5 | 自适应回答长度控制 | Q&A | 3 |
| 6 | 检索上下文元数据丰富 | Q&A | 1 |
| 7 | 文件哈希去重 | 材料 | 3 |
| 8 | 图片质量预检 | 材料 | 2 |
| 9 | 材料字段格式标准化 | 材料 | 1 |
| 10 | LLM 辅助字段提取兜底 | 材料 | 3 |
| 11 | 增强跨字段校验规则 | 材料 | 2 |
| 12 | 智能缺失材料提醒 | 材料 | 3 |

---

## 现状分析

### Q&A 系统现状

1. **检索层** (`src/rag/hybrid_retriever.py`)：使用 RRF 融合 SQLite FTS5 关键词检索与 Qdrant 向量检索，有 query-specific boost（注意事项/时效查询）和 step-sibling 扩展。但缺少 MMR 多样性重排序，同一 source_path 或 step_id 的片段可能重复占据 top_k 位置。

2. **重试机制** (`src/agent/qa_agent.py` + `src/agent/query_rewriter.py`)：重试循环中仅使用单一策略——`query_rewriter.rewrite()`（规则改写或 LLM 改写二选一）。检索范围 scope 在重试中不变，无 scope 放宽或关键词提取等备选策略。

3. **忠实度检查** (`src/agent/scoring/answer_scorer.py`)：使用 2 字中文 bigram 重叠率（`_faithfulness_heuristic`）作为忠实度启发式，粒度粗，无法检测语义层面的幻觉。无 embedding 语义相似度辅助。

4. **一致性控制**：`agent_runs` 表存储历史 Q&A 记录，但无按语义相似度检索相似历史问题的功能。生成回答后不检查与历史回答的一致性。

5. **回答长度**：发送层有粗暴字节切分（`_split_utf8_chunks`），但 QA Agent 生成回答后直接返回全文，无内容感知的长度控制。

6. **Prompt 格式化** (`src/rag/prompt.py`)：仅输出 `来源: {source_path}\n{text}`，`RetrievedChunk` 已有的 `step_title`、`region`、`chunk_kind` 等元数据未使用。

### 材料收集现状

7. **文件去重** (`src/wework/material_handler.py`)：`save_file_message()` 每次上传直接落盘，无哈希去重。同一文件重复上传会重复存储和入库。

8. **图片质量**：文件校验后直接进入视觉识别，无清晰度预检。模糊图片浪费视觉 API 调用并返回 low_confidence。

9. **字段标准化** (`src/materials/form_parser.py`)：仅对 `id_type` 和 `id_number` 做标准化，电话/邮箱/公司名无格式统一。不同来源（聊天文字 vs H5 表单 vs 视觉识别）格式可能不一致。

10. **字段提取**：`extract_material_fields()` 纯基于正则规则，用户自然语言描述不符合正则模式时可能提取不到字段。

11. **跨字段校验** (`src/materials/checklist.py`)：`progress_summary()` 仅统计 received/missing，不校验字段间逻辑一致性（如证件类型与号码格式、电话区号与证件类型）。

12. **缺失提醒** (`src/wework/group_state_machine.py`)：材料提醒仅在用户显式请求或提交材料后被动触发，COLLECTING 状态下发送业务 QA 消息时无主动提醒。

### 数据库现状 (`src/storage/db.py`)

`group_materials` 表结构：`id, roomid, field_key, field_value, file_path, source, status, created_at, updated_at`。无 `file_hash` 列。`upsert_material()` 和 `get_materials()` 方法已存在。`_migrate_columns` 方法已在 `_init_schema` 中调用（用于列迁移）。

---

## 提议变更

### 优化 1：MMR 多样性重排序

**修改文件：** `src/rag/hybrid_retriever.py`, `config/settings.py`

**目标：** 在检索结果中平衡相关性与多样性，避免同一来源/步骤的片段重复占据 top_k。

**具体改动：**

1. 新增模块级函数 `_bigram_jaccard(text_a: str, text_b: str) -> float`：
   - 对两个文本提取 2 字中文 bigram 集合，计算 Jaccard 相似度
   - 空集返回 0.0

2. 新增 `HybridRetriever._mmr_rerank(self, candidates, top_k, lambda_) -> list[RetrievedChunk]` 方法：
   - 经典 MMR 贪心选择：首轮选 score 最高的，后续每轮计算 `mmr = lambda_ * score(d_i) - (1 - lambda_) * max(sim(d_i, d_j) for d_j in selected)`
   - 预计算候选对的 bigram Jaccard 缓存，避免重复计算
   - 直到选满 top_k 或候选耗尽

3. 修改 `retrieve()` 方法返回逻辑（当前第 257-264 行）：
   - `_expand_step_siblings` 返回后，若 `rag_mmr_enabled` 且结果数 > top_k，调用 `_mmr_rerank`
   - MMR 候选池取 `expanded[:max(top_k * 2, 20)]`，再从中选 top_k

**新增配置项：**
```python
rag_mmr_enabled: bool = True
rag_mmr_lambda: float = 0.6  # 0=最大多样性, 1=最大相关性
```

**依据：** MMR 是 RAG 检索多样性重排序的工业标配，lambda=0.5-0.7 为推荐区间。

---

### 优化 2：多策略重试机制

**修改文件：** `src/agent/query_rewriter.py`, `src/agent/qa_agent.py`, `config/settings.py`

**目标：** 将单一 query rewrite 重试扩展为策略链：rewrite -> relax_scope -> keyword_extract。

**具体改动：**

1. 在 `QueryRewriter` 类中新增 `rewrite_with_strategy(self, question, hits, eval_result, strategy) -> tuple[str, str | None]`：
   - `strategy="rewrite"`：调用现有 `self.rewrite()`，返回 `(result, None)`
   - `strategy="relax_scope"`：不改写查询，返回 `(question, "all")`（当前 scope 为 "hk" 时扩大到 "all"）
   - `strategy="keyword_extract"`：提取 2-4 个核心业务关键词，返回 `(keywords, None)`

2. 修改 `qa_agent.py` 的 `run()` 方法重试循环（第 221-259 行）：
   - 定义策略链 `strategies = ["rewrite", "relax_scope", "keyword_extract"]`
   - 每次重试按 attempt 索引选择策略，调用 `rewrite_with_strategy`
   - 若返回 new_scope，更新下一轮检索的 scope
   - 每次重试仍使用 `_merge_hits` 合并历史命中
   - trace 记录中增加 `strategy` 和 `scope` 字段

**新增配置项：**
```python
agent_multi_strategy_retry_enabled: bool = True
```

**依据：** 多策略查询重试是 RAG 系统的标准实践，包括 query rewrite、scope relaxation、keyword extraction 等策略。

---

### 优化 3：基于 Embedding 的忠实度检查

**修改文件：** `src/agent/scoring/answer_scorer.py`, `config/settings.py`

**目标：** 在现有 bigram 启发式基础上，融合 embedding 余弦相似度，提升幻觉检测精度。

**具体改动：**

1. 在 `AnswerScorer.__init__` 中增加 `embedder` 延迟初始化（与 `HybridRetriever` 相同的懒加载模式）

2. 新增 `_embedding_faithfulness(self, answer, context) -> float` 方法：
   - 调用 `Embedder.embed_texts([answer, context[:8000]])`
   - 计算余弦相似度
   - 不可用时返回 -1.0

3. 修改 `score()` 方法（第 65-69 行区域）：
   - 若 `agent_embedding_faithfulness_enabled` 且有 hits，计算 embedding 忠实度
   - 融合：`faithfulness = (1 - weight) * bigram_faith + weight * embedding_faith`
   - embedding 不可用时回退到纯 bigram

**新增配置项：**
```python
agent_embedding_faithfulness_enabled: bool = True
agent_embedding_faithfulness_weight: float = 0.4  # 0=仅 bigram, 1=仅 embedding
```

**依据：** RAGAS faithfulness 指标的低成本替代方案，用 embedding 余弦相似度做粗筛。

---

### 优化 4：回答一致性控制

**修改文件：** `src/storage/db.py`, `src/agent/qa_agent.py`, `src/llm/openai_client.py`, `config/settings.py`

**目标：** 检测当前回答与历史相似问题的回答是否存在矛盾，避免前后不一致。

**具体改动：**

1. 在 `ExternalGroupStore` 中新增 `get_recent_agent_runs(self, roomid, *, limit=20) -> list[dict]`：查询最近 N 条已回复的 agent_runs 记录

2. 在 `QAAgent` 中新增 `_check_consistency(self, question, answer, roomid) -> tuple[bool, str]`：
   - 用 Embedder 计算当前问题与历史问题的余弦相似度
   - 找到最相似的历史问题（超过阈值时）
   - 用 LLM 判断两个回答是否矛盾

3. 在 `LLMClient` 中新增 `check_answer_contradiction(self, q1, a1, q2, a2) -> bool`：通过 `chat_json` 让 LLM 判断矛盾

4. 在 `_run_knowledge_mode` 返回前调用一致性检查（需将 `roomid` 参数传入 `_run_knowledge_mode`），矛盾时追加免责声明并记录 trace

**新增配置项：**
```python
agent_consistency_check_enabled: bool = True
agent_consistency_similarity_threshold: float = 0.85
agent_consistency_history_limit: int = 20
agent_consistency_append_disclaimer: bool = True
```

---

### 优化 5：自适应回答长度控制

**修改文件：** `src/agent/qa_agent.py`, `src/llm/openai_client.py`, `config/settings.py`

**目标：** 超长回答用 LLM 摘要压缩而非粗暴截断，保持核心结论和关键信息。

**具体改动：**

1. 在 `LLMClient` 中新增 `summarize_answer(self, question, answer, max_chars) -> str`：LLM 压缩回答到指定字数内

2. 在 `QAAgent` 中新增 `_enforce_response_length(self, question, answer) -> str`：
   - 超过 `agent_response_max_bytes` 时，先估算目标字符数（UTF-8 中文约 3 字节/字符）
   - 启用摘要时调用 `llm.summarize_answer`
   - 摘要仍超长或禁用摘要时，回退到 UTF-8 安全截断（在句号/换行处截断）

3. 在 `_run_knowledge_mode` 和 `_run_contextual_mode` 返回前调用 `_enforce_response_length`

4. 新增静态方法 `_truncate_utf8(text, max_bytes) -> str`：UTF-8 安全截断，在句号/换行处截断并追加提示

**新增配置项：**
```python
agent_response_max_bytes: int = 1800  # 0=不限制
agent_response_summarize_enabled: bool = True
```

---

### 优化 6：检索上下文元数据丰富

**修改文件：** `src/rag/prompt.py`

**目标：** 在 prompt 中加入 step_title、region、chunk_kind 元数据，帮助 LLM 更好理解上下文。

**具体改动：**

修改 `format_hits_for_prompt` 函数（第 8-15 行）：
- 构建 meta_line，包含来源、步骤、地区、类型
- `step_title` 转为 `步骤: xxx`
- `region` 转为 `地区: 香港/内地`
- `chunk_kind`（非 script 时）转为 `类型: 注意事项/时效信息/要求`
- 输出格式：`【检索片段 {i}】来源: xxx | 步骤: xxx | 地区: 香港 | 类型: 注意事项\n{text}`

**无需新增配置项**（纯格式化改进，对所有调用方透明）。

**影响范围：** 被 `qa_agent.py`、`openai_client.py`（LLM judge/rewrite/contextual/answer_quality）调用，均自动受益。

---

### 优化 7：文件哈希去重

**修改文件：** `src/storage/db.py`, `src/wework/material_handler.py`, `config/settings.py`

**目标：** 通过 SHA-256 哈希检测重复文件，避免同一文件重复存储和入库。

**具体改动：**

1. DB 层 - 在 `_migrate_columns` 中新增 `file_hash` 列到 `group_materials` 表，并创建索引 `idx_group_materials_hash`

2. DB 层 - 新增 `find_material_by_hash(self, roomid, file_hash) -> dict | None` 方法

3. DB 层 - 修改 `upsert_material` 签名，新增 `file_hash: str = ""` 参数

4. Handler 层 - 在 `save_file_message` 中 `validate_upload` 之后计算 SHA-256 哈希，调用 `find_material_by_hash` 检查重复，命中则返回 `"duplicate"` 跳过落盘

5. 在 `_persist_file` 中传递 `file_hash` 到 `upsert_material`

6. 在 `notify_classification` 中增加重复文件提示

7. 新增常量 `DUPLICATE_FILE = "duplicate"`

**新增配置项：**
```python
materials_dedup_enabled: bool = True
```

**依据：** SHA-256 精确去重是文件去重的标准方法，碰撞概率可忽略。

---

### 优化 8：图片质量预检

**修改文件：** `src/wework/material_handler.py`, `config/settings.py`

**目标：** 在视觉识别前检查图片清晰度，模糊图片直接拒绝，节省 API 调用。

**具体改动：**

1. 新增模块级函数 `_check_image_quality(data: bytes) -> tuple[bool, float]`：
   - 使用 `cv2.imdecode` 解码图片，转灰度
   - 计算 `cv2.Laplacian(gray, cv2.CV_64F).var()`（拉普拉斯方差）
   - 与阈值比较，返回 (is_acceptable, variance)
   - cv2 不可用时安全降级返回 (True, -1.0)

2. 在 `save_file_message` 中，`vision_enabled` 为 True 且 `materials_image_quality_enabled` 时，调用 `_check_image_quality`，不通过则返回 `"rejected_blur"`

3. 在 `notify_classification` 中增加模糊图片提示

4. 新增常量 `REJECTED_BLUR = "rejected_blur"`

5. 在 `requirements.txt` 中添加 `opencv-python-headless`

**新增配置项：**
```python
materials_image_quality_enabled: bool = True
materials_blur_threshold: float = 100.0  # Laplacian 方差阈值
```

**依据：** Laplacian 方差法是模糊检测的工业经典做法（Pech-Pacheco 2000），阈值 ~100 为常用值。

---

### 优化 9：材料字段格式标准化

**修改文件：** `src/materials/form_parser.py`

**目标：** 统一电话、邮箱、公司名的格式，保证不同来源数据一致性。

**具体改动：**

1. 新增 `_normalize_phone(raw: str) -> str`：去空格/横线，识别香港号码（852 前缀补 +852）、大陆号码（1[3-9] 开头补 +86），保留已有 + 前缀

2. 新增 `_normalize_email(raw: str) -> str`：去空格、转小写、去尾部多余标点

3. 新增 `_normalize_company_name(raw: str) -> str`：统一全角/半角括号，压缩连续空格，去首尾标点

4. 在 `extract_material_fields` 函数末尾（第 228 行 id_type/id_number 标准化之后）添加对 contact_email、applicant_email、contact_phone、applicant_phone、company_name_en、company_name_cn 的标准化调用

**无需新增配置项**（纯逻辑改进，对所有调用方透明）。

---

### 优化 10：LLM 辅助材料字段提取兜底

**修改文件：** `src/materials/form_parser.py`, `src/llm/openai_client.py`, `config/settings.py`

**目标：** 正则提取不足时用 LLM 从自然语言中补充提取字段。

**具体改动：**

1. 在 `LLMClient` 中新增 `extract_material_fields_llm(self, text) -> dict[str, str]`：通过 `chat_json` 让 LLM 从自然语言提取 14 个标准字段，过滤无效 key

2. 在 `extract_material_fields` 函数末尾（标准化之后）添加 LLM 兜底逻辑：
   - 若 `materials_llm_extraction_enabled` 且正则提取字段数 < `materials_llm_extraction_min_fields` 且文本长度 >= 10
   - 调用 LLM 提取，仅补充正则未提取到的字段
   - LLM 不可用时静默回退
   - 对 LLM 补充的字段也执行标准化

3. 将标准化逻辑抽取为 `_normalize_fields(fields: dict) -> dict`，在正则提取后和 LLM 补充后统一调用

**新增配置项：**
```python
materials_llm_extraction_enabled: bool = True
materials_llm_extraction_min_fields: int = 2  # 正则提取 < 此值时触发 LLM
```

---

### 优化 11：增强跨字段校验规则

**修改文件：** `src/materials/checklist.py`, `src/wework/group_state_machine.py`

**目标：** 校验字段间逻辑一致性，在材料入库时即时发现问题。

**具体改动：**

1. 在 `checklist.py` 中新增 `validate_cross_fields(materials: dict) -> list[dict[str, str]]` 函数，包含 4 条规则：
   - **规则 1（error）**：证件类型与号码格式一致性——HKID 匹配 `^[A-Z]{1,2}\d{6}\(?[\dA]\)?$`，PRC_ID 匹配 18 位身份证，PASSPORT 匹配 `^[A-Z0-9]{5,15}$`
   - **规则 2（warning）**：联系邮箱与申请人邮箱域名差异提示（仅个人邮箱域）
   - **规则 3（warning）**：电话区号与证件类型一致性——HKID 配香港号码、PRC_ID 配大陆号码
   - **规则 4（error）**：商业登记证年限应为 1 或 3

2. 在 `progress_summary` 返回值中增加 `cross_field_issues` 字段

3. 在 `group_state_machine.py` 的材料入库步骤后调用 `validate_cross_fields`，error 级问题阻止进入 REVIEW 并发送校验提示

4. 在 `format_progress_text` 中展示跨字段问题（warning 用提醒标记，error 用错误标记）

**无需新增配置项**（校验规则为硬编码业务逻辑）。

---

### 优化 12：智能缺失材料提醒

**修改文件：** `src/materials/checklist.py`, `src/wework/group_state_machine.py`, `config/settings.py`

**目标：** 在 COLLECTING 状态下，按消息计数和速率限制主动提醒缺失的关键材料。

**具体改动：**

1. 在 `checklist.py` 中新增：
   - `FIELD_PRIORITY: dict[str, int]`：必填字段优先级映射（company_name_en=1, registered_office=2, directors=3, ...）
   - `CRITICAL_FIELD_KEYS = {"company_name_en", "registered_office", "directors"}`
   - `prioritized_missing(materials, *, limit=3) -> list[dict]`：按优先级返回缺失字段列表

2. 在 `group_state_machine.py` 的 `GroupStateMachine` 中新增：
   - 实例变量 `_message_counts: dict[str, int]` 和 `_last_reminder_at: dict[str, float]`
   - 方法 `_maybe_proactive_reminder(self, roomid, *, to_external_userid=None)`：
     - 检查 `materials_proactive_reminder_enabled`
     - 速率限制：距上次提醒间隔 < `materials_proactive_reminder_interval` 则跳过
     - 仅在 COLLECTING/QA/WELCOMED 状态触发
     - 消息计数：每 N 条消息触发一次检查
     - 计算关键缺失，有缺失时发送提醒（区分关键/非关键）
     - `enforce_quota=True` 遵守 KF 额度限制

3. 在消息处理流程中调用 `_maybe_proactive_reminder`：
   - `_execute_plan` 方法末尾
   - QA 回复发送后

4. 消息计数器为内存态，进程重启后重置（可接受——重启后重新计数不会导致刷屏）

**新增配置项：**
```python
materials_proactive_reminder_enabled: bool = True
materials_proactive_reminder_interval: float = 3600.0  # 最短提醒间隔（秒）
materials_proactive_reminder_every_n_messages: int = 5  # 每 N 条消息触发检查
materials_proactive_reminder_max_items: int = 3  # 提醒最多列出几个缺失项
```

---

## 配置项汇总

以下为 `config/settings.py` 的 `Settings` 类中需要新增的所有配置项：

```python
# === RAG MMR ===
rag_mmr_enabled: bool = True
rag_mmr_lambda: float = 0.6

# === Agent 多策略重试 ===
agent_multi_strategy_retry_enabled: bool = True

# === Agent Embedding 忠实度 ===
agent_embedding_faithfulness_enabled: bool = True
agent_embedding_faithfulness_weight: float = 0.4

# === Agent 一致性检查 ===
agent_consistency_check_enabled: bool = True
agent_consistency_similarity_threshold: float = 0.85
agent_consistency_history_limit: int = 20
agent_consistency_append_disclaimer: bool = True

# === Agent 回答长度控制 ===
agent_response_max_bytes: int = 1800
agent_response_summarize_enabled: bool = True

# === 材料去重 ===
materials_dedup_enabled: bool = True

# === 材料图片质量 ===
materials_image_quality_enabled: bool = True
materials_blur_threshold: float = 100.0

# === 材料 LLM 提取 ===
materials_llm_extraction_enabled: bool = True
materials_llm_extraction_min_fields: int = 2

# === 材料主动提醒 ===
materials_proactive_reminder_enabled: bool = True
materials_proactive_reminder_interval: float = 3600.0
materials_proactive_reminder_every_n_messages: int = 5
materials_proactive_reminder_max_items: int = 3
```

---

## 实施顺序与依赖关系

### 建议实施顺序（按依赖关系排列）

1. **优化 6**（prompt 元数据丰富）- 无依赖，改动最小，仅修改 `prompt.py`
2. **优化 9**（字段格式标准化）- 无依赖，仅修改 `form_parser.py`
3. **优化 1**（MMR 重排序）- 仅改 `hybrid_retriever.py`
4. **优化 7**（文件去重）- DB 迁移 + handler 改动
5. **优化 8**（图片质量预检）- 需引入 `opencv-python-headless` 依赖
6. **优化 3**（Embedding 忠实度）- 改 `answer_scorer.py`
7. **优化 2**（多策略重试）- 改 `query_rewriter.py` + `qa_agent.py`
8. **优化 5**（长度控制）- 改 `qa_agent.py` + `openai_client.py`
9. **优化 4**（一致性检查）- 改 `db.py` + `qa_agent.py` + `openai_client.py`
10. **优化 10**（LLM 提取兜底）- 改 `form_parser.py` + `openai_client.py`
11. **优化 11**（跨字段校验）- 改 `checklist.py` + `group_state_machine.py`
12. **优化 12**（主动提醒）- 改 `checklist.py` + `group_state_machine.py`

### 同文件串行合并注意

- 优化 2、3、4、5 都修改 `qa_agent.py`，需串行合并
- 优化 9、10 都修改 `form_parser.py`，需串行合并
- 优化 11、12 都修改 `checklist.py` + `group_state_machine.py`，需串行合并

### 可并行实施的组

- **组 A**（检索质量）：优化 1 + 优化 6（互不依赖）
- **组 B**（材料处理独立项）：优化 7、8、9 可并行（不同文件）

---

## 修改文件清单

| 文件 | 涉及优化项 |
|------|-----------|
| `config/settings.py` | 1,2,3,4,5,7,8,10,12 |
| `src/rag/hybrid_retriever.py` | 1 |
| `src/rag/prompt.py` | 6 |
| `src/agent/qa_agent.py` | 2,3,4,5 |
| `src/agent/query_rewriter.py` | 2 |
| `src/agent/scoring/answer_scorer.py` | 3 |
| `src/llm/openai_client.py` | 4,5,10 |
| `src/storage/db.py` | 4,7 |
| `src/wework/material_handler.py` | 7,8 |
| `src/materials/form_parser.py` | 9,10 |
| `src/materials/checklist.py` | 11,12 |
| `src/wework/group_state_machine.py` | 11,12 |
| `requirements.txt` | 8（新增 opencv-python-headless）|

---

## 假设与决策

1. **MMR 相似度计算**：使用 bigram Jaccard 而非 embedding 余弦相似度，因为 MMR 在检索热路径上执行，embedding 调用会增加延迟。bigram Jaccard 足以识别文本重复片段。

2. **多策略重试 scope 放宽**：`relax_scope` 将 scope 从 "hk" 扩大到 "all"，可能引入内地相关片段。这是预期行为——当香港域知识库无法命中时，扩大范围是合理的回退策略。

3. **Embedding 忠实度**：使用 Embedder 的 `embed_texts` 方法（复用现有 text-embedding-3-small 模型），不引入额外模型依赖。每次评分增加约 100-200ms 延迟，可通过配置关闭。

4. **一致性检查 embedding 缓存**：当前方案每次检查都重新 embed 历史 20 条问题。如性能成为问题，可后续优化为缓存历史问题的 embedding vector（本次不在范围内）。

5. **图片质量预检降级**：`opencv-python-headless` 不可用时安全降级（跳过检查），不影响主流程。

6. **文件去重粒度**：采用精确去重（SHA-256 整文件哈希），不做近似去重（MinHash+LSH）。材料收集场景中重复上传通常是完全相同的文件，精确去重已足够。

7. **主动提醒额度控制**：`enforce_quota=True` 确保遵守 KF 5/48h 额度限制。群聊场景不受额度限制但仍受速率限制。

8. **LLM 提取兜底触发条件**：正则提取字段数 < 2 且文本长度 >= 10 时触发，避免对短消息或已充分提取的消息浪费 LLM 调用。

---

## 验证步骤

### 优化 1（MMR）
- 单元测试：构造 5 个候选（3 个同 source_path），验证 MMR 后同 source 片段不超过 2 个
- 集成测试：对「香港公司注册注意事项」查询，检查返回结果中 source_path 多样性

### 优化 2（多策略重试）
- 单元测试：mock retriever 首轮返回空，验证第二轮使用 rewrite 策略，第三轮使用 relax_scope
- 验证 trace 中记录了 strategy 和 scope 字段

### 优化 3（Embedding 忠实度）
- 单元测试：mock embedder 返回高相似度向量，验证 faithfulness 融合值
- 验证 embedder 不可用时回退到纯 bigram

### 优化 4（一致性检查）
- 单元测试：构造矛盾回答对，验证 LLM 判定为 contradictory
- 验证不矛盾时不追加免责声明

### 优化 5（长度控制）
- 单元测试：构造 3000 字节回答，验证摘要后 < 1800 字节
- 验证摘要不可用时回退到 UTF-8 安全截断

### 优化 6（Prompt 元数据）
- 单元测试：构造含 step_title/region/chunk_kind 的 hits，验证 format 输出包含元数据

### 优化 7（文件去重）
- 单元测试：同一文件内容上传两次，验证第二次返回 "duplicate"
- 验证 DB 中 file_hash 列正确存储

### 优化 8（图片质量）
- 单元测试：构造模糊图片 bytes，验证返回 "rejected_blur"
- 验证 cv2 不可用时安全降级

### 优化 9（字段标准化）
- 单元测试：输入 `contact_phone = "138 0013 8000"`，验证输出 `+86 13800138000`
- 输入 `contact_email = "TEST@Gmail.com "`，验证输出 `test@gmail.com`

### 优化 10（LLM 提取兜底）
- 单元测试：输入自然语言，mock LLM 返回字段，验证合并结果
- 验证正则已提取 >= 2 字段时不触发 LLM

### 优化 11（跨字段校验）
- 单元测试：id_type=HKID + id_number=123456789012345678，验证返回 error
- 单元测试：id_type=PRC_ID + contact_phone=+852 98765432，验证返回 warning

### 优化 12（主动提醒）
- 单元测试：mock 5 条消息后验证调用 `_maybe_proactive_reminder`
- 验证速率限制：1 小时内第二次调用被跳过
- 验证 COLLECTING 状态外不触发

### 整体验证
- 运行 `python -m pytest tests/ -v` 确保无回归
- 手动测试：发送业务问题，检查 QA 回答质量
- 手动测试：上传重复文件、模糊图片，检查拒绝行为
- 手动测试：提交部分材料，发送业务 QA，检查是否收到主动提醒
