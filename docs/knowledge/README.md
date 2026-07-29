# 知识库目录

将香港公司注册相关的自定义文档放在此目录，供 RAG 检索使用。

## 支持格式

- `.md` / `.txt` — 推荐
- `.pdf`
- `.docx`

## 建议组织方式

按主题拆分文件，文件名尽量包含关键词，便于 FTS5 关键词检索，例如：

- `注册地址说明.md`
- `董事股东材料.md`
- `ICRIS流程与时效.md`

## 入库

先启动 Qdrant（见项目文档），再执行：

```powershell
python main.py rag-ingest
```

单文件重建：

```powershell
python main.py rag-ingest --file docs/knowledge/注册地址说明.md
```

## 检索调试

```powershell
python main.py rag-query "香港公司注册地址可以用 PO Box 吗"
python main.py rag-query "公司住哪里" --answer
```
