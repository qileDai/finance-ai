#!/usr/bin/env python3
"""香港场景 RAG golden query 回归（需先 rag-ingest 注册.md）"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOLDEN_QUERIES: list[tuple[str, str]] = [
    ("进群怎么打招呼", "hk"),
    ("香港注册需要什么资料", "hk"),
    ("催资料怎么说", "hk"),
    ("名称被抽查怎么办", "hk"),
    ("注册成功怎么通知客户", "hk"),
    ("开户面签要注意什么", "hk"),
    ("邮寄地址确认", "hk"),
    ("董事股东要哪些材料", "hk"),
    ("注册提交后多久出结果", "hk"),
]

CN_QUERIES = [
    "深圳注册被驳回怎么办",
    "U盾办理注意事项",
]

CAUTION_QUERY = "开户面签要注意什么"
CAUTION_MARKERS = ("面签注意事项", "被制裁国家", "仅需要面签人员", "不要拍照")


def main() -> int:
    from config.settings import settings
    from src.rag.hybrid_retriever import HybridRetriever, is_primary_source

    retriever = HybridRetriever()
    scope = "hk"
    print(f"[Golden] RAG scope={scope}, primary={settings.rag_primary_sources}\n")

    failed = 0
    for query, expect_region in GOLDEN_QUERIES:
        hits = retriever.retrieve(query, scope=scope)
        if not hits:
            print(f"FAIL 无命中: {query}")
            failed += 1
            continue
        top = hits[0]
        ok_region = expect_region == "cn" or top.region != "cn"
        ok_source = is_primary_source(top.source_path)
        status = "OK" if ok_region and ok_source else "WARN"
        if status != "OK":
            failed += 1
        print(f"{status}  Q: {query}")
        print(f"      region={top.region} source={top.source_path}")
        print(f"      step={top.step_title[:50]} kind={top.chunk_kind}")
        print()

    print("--- 面签注意事项内容断言 ---")
    caution_hits = retriever.retrieve(CAUTION_QUERY, scope=scope)
    if not caution_hits:
        print(f"FAIL  {CAUTION_QUERY} → 无命中")
        failed += 1
    else:
        merged = "\n".join(h.text for h in caution_hits[:3])
        missing = [m for m in CAUTION_MARKERS if m not in merged]
        top = caution_hits[0]
        if top.chunk_kind != "caution" and "面签注意事项" not in top.text:
            print(
                f"WARN  {CAUTION_QUERY} → #1 非 caution 块: "
                f"kind={top.chunk_kind} step={top.step_title[:40]}"
            )
            failed += 1
        elif missing:
            print(f"FAIL  {CAUTION_QUERY} → top-3 缺少: {missing}")
            failed += 1
        else:
            print(
                f"OK    {CAUTION_QUERY} → #1 kind={top.chunk_kind} "
                f"step={top.step_title[:30]}"
            )

    print("\n--- 国内问题 scope=hk 应过滤 ---")
    for query in CN_QUERIES:
        hits = retriever.retrieve(query, scope=scope)
        cn_hits = [h for h in hits if h.region == "cn"]
        if cn_hits:
            print(f"WARN  {query} → 仍命中 cn: {cn_hits[0].step_title[:40]}")
            failed += 1
        else:
            print(f"OK    {query} → 未命中国内段落")

    print(f"\n[Golden] 完成，异常项: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
