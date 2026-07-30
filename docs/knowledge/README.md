# 知识库目录

将香港公司注册相关的业务知识放在此目录，供 RAG 检索使用。

## 主知识文件

| 文件 | 说明 |
|------|------|
| **`注册.md`** | **唯一主知识源**（由 `ai注册.docx` 转换），含香港/国内各注册步骤话术与注意事项 |

Bot 产品说明（群指令、非 RAG）见 [`docs/product/`](../product/)。

## 支持格式

- `.md` / `.txt` — 推荐
- `.pdf`
- `.docx`（默认排除，请维护 `注册.md`）

## 入库

```powershell
python main.py rag-ingest --file docs/knowledge/注册.md --verbose
python main.py rag-status
```

## 检索调试

```powershell
python main.py rag-query "开户面签要注意什么"
python main.py rag-query "催资料怎么说" --answer
python scripts/rag_golden_hk.py
```

## 配置

- `RAG_SCOPE=hk` — 外部群默认仅检索香港段落
- `RAG_PRIMARY_SOURCES=docs/knowledge/注册.md`
