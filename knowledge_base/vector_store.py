"""
旅游知识库向量存储模块

职责：
1. 构造阿里百炼通义千问 Embedding（OpenAI 兼容模式）
2. 把切片后的旅游文档批量写入 Chroma 向量库并持久化
3. 加载已构建的向量库，并对外提供检索器（Retriever）

说明：本模块统一使用 langchain_chroma.Chroma（LangChain 1.x 官方集成包），
不再使用已弃用的 langchain_community.vectorstores.Chroma。
"""
import os
import shutil

from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings

# 向量库持久化目录（相对于项目根目录）
PERSIST_DIR = "./chroma_db"
# 集合名称，便于与机器上其它项目的向量库区分
COLLECTION_NAME = "tourism_kb"
# 单次提交给 Embedding 接口的文本条数（百炼接口对批量大小有上限）
EMBED_BATCH_SIZE = 10
# 每批写入向量库的文档条数，避免一次性占用过多内存
WRITE_BATCH_SIZE = 20
# 默认检索返回的片段数（旅游方案涉及门票/交通/美食/住宿多方面，取 5 覆盖更全）
DEFAULT_TOP_K = 5


def get_embeddings():
    """构造通义千问 Embedding 模型（text-embedding-v3）"""
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        raise RuntimeError("未找到 DASHSCOPE_API_KEY，请检查项目根目录下的 .env 文件")
    return OpenAIEmbeddings(
        model="text-embedding-v3",
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        # 关闭本地 token 计数：兼容模式下交由服务端处理，避免超长文本提前报错
        check_embedding_ctx_length=False,
        # 压小单次请求的文本条数，规避接口批量上限
        chunk_size=EMBED_BATCH_SIZE,
    )


def build_vector_store(docs, persist_directory=PERSIST_DIR, reset=True):
    """
    构建旅游知识库向量存储

    :param docs: 切片后的文档列表（List[Document]）
    :param persist_directory: 向量库持久化目录
    :param reset: 是否先清空旧向量库（重建时置 True，避免历史脏数据残留）
    :return: 构建好的 Chroma 实例；无有效文档时返回 None
    """
    if not docs:
        print("❌ 错误：没有找到任何有效的旅游文档！")
        print("请检查 knowledge_base 文件夹中是否有 .txt/.md/.json 格式的文档")
        print("注意：文档可放在 knowledge_base 根目录，也可放在其子目录中（加载器会递归扫描）")
        return None

    if reset and os.path.exists(persist_directory):
        shutil.rmtree(persist_directory)
        print(f"🧹 已清空旧的向量库目录 {persist_directory}")

    print(f"🔄 开始构建旅游向量库，共 {len(docs)} 个文档片段")
    embeddings = get_embeddings()
    vectordb = None
    total_batches = (len(docs) + WRITE_BATCH_SIZE - 1) // WRITE_BATCH_SIZE

    # 分批次写入，避免单次请求过大
    for i in range(0, len(docs), WRITE_BATCH_SIZE):
        batch = docs[i:i + WRITE_BATCH_SIZE]
        print(f"  正在处理第 {i // WRITE_BATCH_SIZE + 1}/{total_batches} 批...")

        if vectordb is None:
            vectordb = Chroma.from_documents(
                documents=batch,
                embedding=embeddings,
                persist_directory=persist_directory,
                collection_name=COLLECTION_NAME,
            )
        else:
            vectordb.add_documents(documents=batch)

    # Chroma 0.4.x 起由客户端自动持久化，无需手动调用 persist()
    print(f"✅ 旅游向量库构建完成！共写入 {len(docs)} 个片段，已保存到 {persist_directory}")
    return vectordb


def get_vector_store(persist_directory=PERSIST_DIR):
    """
    加载已构建的旅游向量库

    :param persist_directory: 向量库持久化目录
    :return: Chroma 实例；未构建时返回 None
    """
    if not os.path.exists(os.path.join(persist_directory, "chroma.sqlite3")):
        print("⚠️  未找到已构建的向量库，请先运行 python init_kb.py 构建知识库")
        return None

    embeddings = get_embeddings()
    vectordb = Chroma(
        persist_directory=persist_directory,
        embedding_function=embeddings,
        collection_name=COLLECTION_NAME,
    )
    print(f"✅ 成功加载旅游向量库，目录：{persist_directory}")
    return vectordb


def get_retriever(top_k=DEFAULT_TOP_K, persist_directory=PERSIST_DIR):
    """
    获取知识库检索器（供 Agent 工具调用）

    :param top_k: 每次检索返回的片段数
    :return: VectorStoreRetriever；向量库不可用时返回 None
    """
    vectordb = get_vector_store(persist_directory)
    if vectordb is None:
        return None
    return vectordb.as_retriever(search_kwargs={"k": top_k})
