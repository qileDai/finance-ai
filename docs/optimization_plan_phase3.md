# 问答系统与材料收集 Phase 3 优化计划

> 基于代码审查 + 业界最佳实践研究，识别出 **15 项优化**，分 3 个优先级实施。

---

## 一、现状诊断

### 已完成（Phase 1 & 2）

| 模块 | 已实现优化 |
|------|-----------|
| RAG 检索 | MMR 多样性重排序、双路 RRF 融合、caution/duration boost、step-siblings 扩展 |
| 回答质量 | bigram 忠实度启发式、embedding 忠实度检查、LLM judge、completeness 启发式 |
| 材料收集 | 文件哈希去重、图片模糊预检、字段格式标准化、LLM 辅助提取、跨字段校验、主动缺失提醒 |
| Agent 循环 | FAQ 快路径、query rewrite、自我纠错重试、域内/域外兜底 |

### 关键发现：3 项已配置但未实现的优化

通过代码审查发现，以下 Phase 2 配置项已在 `settings.py` 中定义，但**代码中未实际实现**：

| 配置项 | 状态 | 影响 |
|--------|------|------|
| `agent_multi_strategy_retry_enabled` | 未实现 | 检索失败时仅单一 query rewrite，无多策略兜底 |
| `agent_consistency_check_enabled` | 未实现 | 同一问题可能给出矛盾答案 |
| `agent_response_max_bytes` / `agent_response_summarize_enabled` | 未实现 | 长答案可能超企微字数限制被截断 |

---

## 二、优化项总览

### 优先级 P0（紧急补全，1-3 天）

#### 1. 多策略重试机制（补全已配置功能）
- **文件**: `src/agent/query_rewriter.py`、`src/agent/qa_agent.py`
- **现状**: 检索失败仅走单一 LLM query rewrite，命中率有限
- **方案**: 实现三策略并行重试：
  - 策略 A: LLM query rewrite（已有）
  - 策略 B: scope 放宽（hk -> all）
  - 策略 C: 关键词提取（从问题中抽取核心实体词重检）
- **预期效果**: 检索召回率提升 15-25%

#### 2. 回答一致性检查（补全已配置功能）
- **文件**: `src/agent/qa_agent.py`、`src/storage/db.py`
- **现状**: 同一用户在不同时间问相同问题，可能得到矛盾答案
- **方案**:
  - 从 `agent_runs` 表查询近期（24h）相同 roomid 的历史回答
  - 用 bigram Jaccard 计算新回答与历史回答的相似度
  - 低于阈值时，在答案末尾追加"注：此前回复为 XXX，如有差异请以本次为准"
- **预期效果**: 减少矛盾回答投诉

#### 3. 自适应回答长度控制（补全已配置功能）
- **文件**: `src/agent/qa_agent.py`、`src/llm/openai_client.py`
- **现状**: 长答案可能超出企微 2048 字节限制被截断
- **方案**:
  - 生成后检查字节数，超 `agent_response_max_bytes` 时触发 LLM 摘要压缩
  - 摘要 prompt: "请将以下回答压缩到 N 字以内，保留关键信息和编号列表"
  - 压缩仍超限则按段落切分发送（已有 `wework_kf_long_reply_max_bytes` 切分逻辑）

---

### 优先级 P1（核心增强，1-2 周）

#### 4. LLM API 熔断器与降级链
- **文件**: `src/llm/openai_client.py`（新增 `circuit_breaker.py`）
- **现状**: LLM 调用失败时无熔断保护，连续故障会拖垮整个系统
- **方案**:
  - 实现三态熔断器（CLOSED -> OPEN -> HALF_OPEN）
  - 错误率 >50% 或连续超时 5 次时熔断
  - 熔断期间降级链: FAQ 缓存 -> 规则话术 -> 友好兜底文案
  - 半开态放行 1 个探测请求验证恢复
- **业界参考**: LLM 限流需额外限制 Token 维度，预计算输入 Token 后预拦截超长请求

#### 5. 流式响应支持
- **文件**: `src/llm/openai_client.py`、`src/wework/group_state_machine.py`
- **现状**: QA 生成完全同步，用户等待 3-8 秒无反馈
- **方案**:
  - `generate_answer` 增加 `stream=True` 选项
  - 首字到达前先发送"正在为您查询，请稍候…"（利用已有 `wework_thinking_ack`）
  - 企微不支持流式推送，但可先发 ack 再异步发送完整答案
- **业界参考**: 首 token 延迟 (TTFT) 优先，检索与 prompt 组装应与用户等待并行

#### 6. 材料完整性加权评分
- **文件**: `src/materials/checklist.py`
- **现状**: 进度仅按 received/total 计数，不区分字段重要性
- **方案**:
  ```python
  FIELD_WEIGHTS = {
      "company_name_en": 20,
      "registered_office": 15,
      "directors": 15,
      "contact_email": 10,
      "contact_phone": 10,
      "founder_members": 10,
      "company_secretary": 5,
      "business_desc": 5,
      "applicant_name": 5,
      "applicant_email": 3,
      "applicant_phone": 2,
      "id_card_front": 10,
  }
  ```
  - `progress_summary` 增加 `completeness_score` (0-100)
  - 低于 60 分时提醒"关键材料不足"
  - 高于 90 分时提示"基本齐全，可确认提交"

#### 7. 查询语义路由层
- **文件**: `src/agent/qa_agent.py`（新增 `query_router.py`）
- **现状**: 所有问题走同一条检索路径，FAQ 命中后直接跳过 RAG
- **方案**: 在 FAQ 检查后、RAG 检索前增加语义路由：
  - **事实型查询**（"XXX 是什么"、"XXX 需要什么"）-> 标准检索 + 知识回答
  - **进度型查询**（"我的资料"、"还缺什么"）-> 直接查 DB 材料状态
  - **比较型查询**（"A 和 B 区别"）-> 增加检索 top_k + 结构化对比输出
  - **时效型查询**（"多久"、"周期"）-> duration boost（已有）+ 优先返回
- **业界参考**: context routing 在 query 进入 context window 前分类并导向正确 context source

#### 8. H5 表单草稿保存与断点续传
- **文件**: `src/web/collect_server.py`、`src/storage/db.py`
- **现状**: H5 表单填写中途刷新/退出会丢失所有内容
- **方案**:
  - 前端 localStorage 自动保存（每 5 秒）
  - 后端新增 `form_drafts` 表：`roomid -> draft_json + updated_at`
  - 表单加载时检查是否有草稿，提示"检测到上次未完成的填写，是否恢复？"
  - 提交成功后自动清除草稿

#### 9. 证件 OCR 置信度持久化
- **文件**: `src/materials/id_document_vision.py`、`src/storage/db.py`
- **现状**: 证件识别的 confidence 仅日志记录，未持久化
- **方案**:
  - `group_materials` 表增加 `ocr_confidence REAL DEFAULT 0` 列
  - 证件入库时写入 confidence
  - 管理后台审核界面高亮低置信度（<0.8）项
  - 低置信度自动标 `needs_review` 并通知专员

---

### 优先级 P2（体验优化，2-4 周）

#### 10. 引用透明度增强
- **文件**: `src/rag/prompt.py`、`src/agent/qa_agent.py`
- **现状**: citations 仅在答案末尾列出来源文件名
- **方案**:
  - prompt 中要求 LLM 在每个事实陈述后标注 [片段N]
  - 回答末尾附"详细来源"列表，含片段编号 + 文件名 + 步骤标题
  - 管理后台可查看完整检索片段与答案的对应关系
- **业界参考**: 片段级内联引用支持点击跳转到原文位置

#### 11. 多轮对话分层记忆
- **文件**: `src/agent/qa_agent.py`、新增 `src/agent/memory.py`
- **现状**: history 是简单字符串列表，全量塞入 prompt（最多 10 条）
- **方案**: 三层记忆架构：
  - **短期**: 最近 5 轮原文（已有）
  - **中期**: 滚动摘要（每 5 轮用 LLM 压缩为 1 段摘要）
  - **长期**: agent_runs 表按 roomid 检索历史 Q&A
- **业界参考**: 对话历史视为独立 RAG 数据源，向量化索引后按需检索

#### 12. PDF/Word 材料结构化解析
- **文件**: `src/materials/`（新增 `document_extractor.py`）
- **现状**: 仅支持图片证件识别，不支持 PDF/Word 材料的内容提取
- **方案**:
  - 复用 RAG 的 `document_parser.py` 的 `load_text` 函数
  - PDF/Word 文件上传后自动提取文本
  - 提取文本走 `extract_material_fields` 补充结构化字段
  - 地址证明 PDF 可自动提取地址信息

#### 13. RAGAS 三维质量评估
- **文件**: 新增 `src/agent/evaluation.py`
- **现状**: 无系统化的 RAG 质量评估，仅靠 agent_runs 日志
- **方案**:
  - 定期（每日/每周）从 agent_runs 采样 50 条
  - 计算 RAGAS 三维指标：
    - **context precision**: 检索片段中相关比例
    - **context recall**: 回答所需信息在检索片段中的覆盖比例
    - **faithfulness**: 回答是否忠实于检索片段
  - 生成评估报告，指标低于基线时告警
- **业界参考**: RAGAS 三维指标应作为 RAG 系统质量生命线

#### 14. needs_review 自动转人工
- **文件**: `src/wework/group_state_machine.py`
- **现状**: 材料标 `needs_review` 后无后续动作，需人工巡检
- **方案**:
  - 材料 `needs_review` 数量 >= 2 时自动通知专员
  - 通知消息含: 群名、公司名、待复核项列表、复核链接
  - 专员可通过管理后台一键确认/驳回

#### 15. Token 预算限流
- **文件**: `src/llm/openai_client.py`
- **现状**: 仅靠 `openai_timeout_seconds` 超时控制，无 Token 级限流
- **方案**:
  - 调用前预计算 input token 数（按 1 中文字约等于 2 token 估算）
  - 超过模型上下文 80% 时拒绝并记录
  - 按 roomid 维度限流: 每会话每分钟最多 N 次 LLM 调用
  - 全局 Token 预算: 每日上限，接近上限时降级到小模型
- **业界参考**: LLM 限流必须包含 Token 维度，预计算后预拦截

---

## 三、实施排期

| 阶段 | 优化项 | 预计工期 | 依赖 |
|------|--------|---------|------|
| **Sprint 1** (P0) | #1 多策略重试、#2 一致性检查、#3 长度控制 | 3 天 | 无 |
| **Sprint 2** (P1-A) | #4 熔断器、#5 流式响应、#6 完整性评分 | 5 天 | 无 |
| **Sprint 3** (P1-B) | #7 语义路由、#8 表单草稿、#9 OCR 置信度 | 5 天 | #6 完成 |
| **Sprint 4** (P2-A) | #10 引用增强、#11 分层记忆、#12 PDF 解析 | 7 天 | #7 完成 |
| **Sprint 5** (P2-B) | #13 RAGAS 评估、#14 自动转人工、#15 Token 限流 | 7 天 | #4 完成 |

---

## 四、风险评估

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| 熔断器误判导致正常请求被拒 | 中 | 高 | 半开态探测 + 动态阈值调整 |
| LLM 摘要压缩丢失关键信息 | 中 | 中 | 保留原文备查 + 仅在超限时触发 |
| 语义路由分类错误 | 低 | 中 | fallback 到标准检索路径 |
| 表单草稿与正式提交冲突 | 低 | 低 | 提交时加锁 + 版本号校验 |
| RAGAS 评估消耗额外 LLM 额度 | 中 | 低 | 采样而非全量 + 用小模型评估 |
