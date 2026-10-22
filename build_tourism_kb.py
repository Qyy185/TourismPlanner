"""
知识库构建脚本

完整流程：
1. 批量加载 knowledge_base 文件夹中的所有旅游文档（txt / md / json）
2. 按文档格式选择切片策略（Markdown 按标题层级，其余按递归字符）
3. 写入 Chroma 向量库并持久化到本地

用法：
    python build_tourism_kb.py
"""
from knowledge_base.document_loader import batch_load_tourism_docs
from knowledge_base.text_splitter import split_tourism_markdown, split_tourism_text
from knowledge_base.vector_store import build_vector_store

# 旅游知识库文件夹路径（相对于项目根目录）
TOURISM_FOLDER = "./knowledge_base"


def build_tourism_knowledge_base():
    """执行一次完整的知识库构建"""
    # 1. 批量加载所有旅游文档
    print("🔍 正在加载旅游文档...")
    raw_docs = batch_load_tourism_docs(TOURISM_FOLDER)

    if len(raw_docs) == 0:
        print("❌ 错误：knowledge_base 文件夹中没有找到任何有效的旅游文档！")
        print("请确保文件夹中存在 .txt/.md/.json 格式的旅游攻略、景点介绍等文档")
        return

    print(f"✅ 成功加载 {len(raw_docs)} 个原始旅游文档")

    # 2. 按格式选择切片策略
    print("🔄 正在分割文档...")
    split_docs = []
    for doc in raw_docs:
        source = str(doc.metadata.get("source", ""))

        if source.endswith(".md"):
            # Markdown 攻略：按标题层级分割，保留结构信息
            md_splits = split_tourism_markdown(doc.page_content)
            for split in md_splits:
                # 合并而非覆盖：既保留原始 source，又保留 Header 1/2/3 层级信息
                split.metadata = {**doc.metadata, **split.metadata}
            split_docs.extend(md_splits)
        else:
            # 纯文本 / JSON：按递归字符分割
            text_splits = split_tourism_text(doc.page_content)
            for split in text_splits:
                # 补上来源信息，否则检索结果无法追溯出处
                split.metadata = {**doc.metadata, **split.metadata}
            split_docs.extend(text_splits)

    print(f"✅ 文档分割完成，共生成 {len(split_docs)} 个文档片段")

    # 3. 构建并保存向量库
    build_vector_store(split_docs)


if __name__ == "__main__":
    build_tourism_knowledge_base()
