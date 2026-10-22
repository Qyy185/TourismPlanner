"""
Agent 工具模块

把本地旅游知识库封装成 LangChain Tool，供 Agent 在需要专业旅游信息时调用。
工具采用懒加载：只有真正被调用时才去加载向量库，
避免「未构建知识库」导致整个程序在导入阶段就崩溃。
"""
import os
import threading

from langchain_core.tools import tool

from knowledge_base.vector_store import get_retriever

# 检索器缓存（懒加载）
_RETRIEVER = None
# 标记初始化失败，避免每次调用都重试加载向量库
_RETRIEVER_FAILED = False
# 初始化锁：Agent 会并行发起多个工具调用，必须保证向量库只被初始化一次
_INIT_LOCK = threading.Lock()


def _load_retriever():
    """按需加载检索器；成功返回 retriever，失败返回 None

    注意：LangGraph 在执行 Agent 时会**并行**调用同一工具（例如同时检索门票、美食、住宿）。
    若不加锁，多个线程会同时执行 Chroma 的客户端初始化，互相抢占资源导致部分调用抛异常，
    进而把 _RETRIEVER_FAILED 置为 True，表现为「知识库明明建好了却报加载失败」。
    """
    global _RETRIEVER, _RETRIEVER_FAILED

    # 快路径：已初始化成功，直接复用（无锁，避免检索被串行化）
    if _RETRIEVER is not None:
        return _RETRIEVER
    if _RETRIEVER_FAILED:
        return None

    with _INIT_LOCK:
        # 双重检查：等待锁期间可能已被其它线程初始化完成
        if _RETRIEVER is not None:
            return _RETRIEVER
        if _RETRIEVER_FAILED:
            return None

        try:
            _RETRIEVER = get_retriever()
        except Exception as exc:
            print(f"⚠️  向量库加载失败：{exc}")
            _RETRIEVER = None

        if _RETRIEVER is None:
            _RETRIEVER_FAILED = True

    return _RETRIEVER


def _format_source(doc):
    """把片段的来源信息整理成一行可读标题"""
    source = str(doc.metadata.get("source", "未知来源")).replace("\\", "/")
    source = os.path.basename(source)

    headers = [
        str(doc.metadata[key])
        for key in ("Header 1", "Header 2", "Header 3")
        if doc.metadata.get(key)
    ]
    return f"{source}（{' > '.join(headers)}）" if headers else source


@tool
def travel_knowledge_search_tool(query: str) -> str:
    """检索本地旅游知识库。

    当用户询问某个目的地的景点、门票价格、开放时间、交通方式、特色美食、
    住宿区域、最佳旅行季节或出行注意事项时，优先调用本工具获取准确资料。

    Args:
        query: 检索用的自然语言问题，例如「成都有哪些必去的景点」
    """
    retriever = _load_retriever()
    if retriever is None:
        return "本地旅游知识库尚未构建或加载失败，请先运行 python init_kb.py。本轮回答请退回通用知识。"

    try:
        docs = retriever.invoke(query)
    except Exception as exc:
        return f"检索本地知识库时出错：{exc}。本轮回答请退回通用知识。"

    if not docs:
        return "本地知识库中未找到相关旅游信息，请使用通用知识回答，并标注【通用参考】。"

    blocks = []
    for index, doc in enumerate(docs, 1):
        blocks.append(f"[参考{index}] {_format_source(doc)}\n{doc.page_content.strip()}")
    return "\n\n".join(blocks)


def get_tools():
    """返回旅游规划 Agent 可用的工具列表"""
    return [travel_knowledge_search_tool]


def warmup():
    """预热向量库：建议在 Web 服务启动时于后台线程调用。

    向量库首次加载（Chroma 客户端初始化）需要 1~2 秒，若等用户第一次
    点击生成才加载，首屏会明显变慢。这里提前加载好，后续请求直接复用。
    """
    return _load_retriever() is not None
