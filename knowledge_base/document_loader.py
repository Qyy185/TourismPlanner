"""
文档加载模块

从知识库目录批量加载旅游资料，支持三种格式：
- .md   城市/目的地攻略（走标题层级切片，检索效果最好）
- .txt  零散贴士类资料（走递归字符切片）
- .json 结构化景点数据（最外层必须是数组，一条一个景点）

目录组织说明
------------
为支持「34 个省级行政区」的全量知识库，目录按行政区类别分文件：

    knowledge_base/
    ├── destinations/             # 目的地知识库（每个省级行政区一份主攻略 + 一份路线库）
    │   ├── 直辖市/               # 北京 天津 上海 重庆
    │   ├── 省/                   # 23 个省
    │   ├── 自治区/               # 内蒙古 广西 西藏 宁夏 新疆
    │   └── 特别行政区/           # 中国香港 中国澳门
    ├── parameters/               # 参数层：全局共享的预算档位标准、人数适配通用指南
    ├── 景点数据.json              # 结构化门票/开放时间数据
    └── 出行实用指南.md            # 跨目的地通用的出行常识

因此加载器必须**递归扫描**子目录——早期版本用 os.listdir 只扫一层，
一旦把文件放进 destinations/直辖市/ 就会全部漏掉。
"""
import json
import os

from langchain_core.documents import Document

# 支持的文档扩展名
SUPPORTED_SUFFIXES = (".json", ".md", ".txt")
# 需要跳过的目录名（缓存/版本控制等）
SKIP_DIR_NAMES = {"__pycache__", ".git", ".idea", "node_modules"}


def load_tourism_json(file_path):
    """加载旅游JSON（景点门票/开放时间等结构化数据）

    约定：最外层必须是数组，每个元素代表一个景点；写成对象会直接报错。
    """
    docs = []
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        for item in data:
            content = json.dumps(item, ensure_ascii=False)
            docs.append(Document(page_content=content, metadata={"source": file_path}))
    return docs


def load_text(file_path):
    """加载旅游纯文本/Markdown文件"""
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    return content


def _iter_doc_files(folder_path):
    """递归遍历知识库目录，产出所有可加载的文档文件路径（按路径排序，保证构建结果稳定）

    跳过规则：
    - 隐藏文件/目录（以 . 开头，如 .Rhistory）
    - 缓存/IDE 目录（__pycache__、.git、.idea、node_modules）
    """
    for root, dirs, files in os.walk(folder_path):
        # 原地过滤，避免 os.walk 继续深入这些目录
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIR_NAMES)
        for filename in sorted(files):
            if filename.startswith("."):
                continue
            if filename.endswith(SUPPORTED_SUFFIXES):
                yield os.path.join(root, filename)


def batch_load_tourism_docs(folder_path):
    """批量加载knowledge_base文件夹内所有旅游文档（支持txt/md/json，递归扫描子目录）"""
    docs = []
    for file_path in _iter_doc_files(folder_path):
        if file_path.endswith(".json"):
            docs.extend(load_tourism_json(file_path))
        else:
            content = load_text(file_path)
            metadata = {"source": file_path, "type": "text/markdown"}
            docs.append(Document(page_content=content, metadata=metadata))
    return docs
