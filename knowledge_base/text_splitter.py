"""
文本切片模块

针对两类旅游资料采用差异化切片策略：
- Markdown 攻略：按标题层级切分，保留「城市 > 板块 > 景点」的结构信息
- 纯文本资料：按递归字符切分，兼顾语义完整与片段长度
"""
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter


def split_tourism_markdown(markdown_text):
    """
    分割旅游攻略的 Markdown 文本（按标题层级：城市 → 板块 → 景点）

    关键点：strip_headers=False 让标题保留在 page_content 中。
    默认的 strip_headers=True 会把标题剥离，只留在 metadata 里，
    导致片段正文失去上下文（例如只看到「门票 55 元」却不知道是哪个景点），
    向量化后检索命中率会明显下降。
    """
    headers_to_split_on = [
        ("#", "Header 1"),   # 对应「# 北京旅游攻略」
        ("##", "Header 2"),  # 对应「## 热门景点」
        ("###", "Header 3"), # 对应「### 故宫」
    ]
    markdown_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=headers_to_split_on,
        strip_headers=False,
    )
    return markdown_splitter.split_text(markdown_text)


def split_tourism_text(text, chunk_size=500, chunk_overlap=80):
    """
    分割旅游纯文本（交通/住宿/注意事项等非结构化资料）

    :param chunk_size: 单片段目标长度。旅游文本信息密度低于说明书类文档，故取 500
    :param chunk_overlap: 片段重叠长度，保证跨片段的上下文连贯
    """
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", "。", "！", "？", "；", "，", "、", ""],
    )
    return text_splitter.create_documents([text])
