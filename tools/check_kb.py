"""
知识库健康检查脚本

用途：在不消耗大模型额度的前提下，验证向量库是否构建成功、检索是否可用。
（构建完知识库后跑一遍，可以快速确认 RAG 链路的下半段是通的）

用法：
    python tools/check_kb.py
"""
import os
import sys

# 允许以 `python tools/check_kb.py` 的方式从项目根目录运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from knowledge_base.vector_store import get_retriever

SAMPLE_QUERIES = [
    "成都有哪些必去的景点",
    "故宫门票多少钱，需要提前预约吗",
    "三亚冬天适合去吗，要注意什么",
]


def describe(doc):
    """把片段的来源整理成「文件名（标题层级）」形式"""
    source = str(doc.metadata.get("source", "未知")).replace("\\", "/").split("/")[-1]
    headers = [
        str(doc.metadata[key])
        for key in ("Header 1", "Header 2", "Header 3")
        if doc.metadata.get(key)
    ]
    return f"{source}（{' > '.join(headers)}）" if headers else source


def main():
    retriever = get_retriever()
    if retriever is None:
        print("❌ 向量库不可用，请先运行 python init_kb.py")
        sys.exit(1)

    print("=" * 60)
    print("知识库检索自检")
    print("=" * 60)

    for query in SAMPLE_QUERIES:
        print(f"\n查询：{query}")
        try:
            docs = retriever.invoke(query)
        except Exception as exc:
            print(f"  ❌ 检索出错：{exc}")
            continue

        if not docs:
            print("  ⚠️  未检索到任何片段")
            continue

        for index, doc in enumerate(docs, 1):
            preview = doc.page_content.strip().replace("\n", " ")[:70]
            print(f"  [{index}] {describe(doc)}")
            print(f"      {preview}...")

    print("\n" + "=" * 60)
    print("自检完成")


if __name__ == "__main__":
    main()
