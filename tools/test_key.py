"""
阿里百炼 API Key 连通性测试脚本

用途：在正式构建知识库之前，快速验证 Key 是否有效、Embedding 接口是否可达。
（本脚本由原 init_kb.py 中错放的内容整理而来）

用法：
    python tools/test_key.py
"""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

# 通义千问嵌入接口地址
EMBEDDING_URL = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"


def main():
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        print("❌ 未找到 DASHSCOPE_API_KEY，请检查项目根目录下的 .env 文件")
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "text-embedding-v3",
        "input": {"texts": ["北京故宫，旧称紫禁城，是中国明清两代的皇家宫殿"]},
    }

    print("正在测试 Embedding 接口连通性...")
    try:
        response = requests.post(EMBEDDING_URL, headers=headers, json=payload, timeout=30)
    except Exception as exc:
        print(f"❌ 请求失败：{exc}")
        sys.exit(1)

    print(f"状态码：{response.status_code}")
    if response.status_code == 200:
        print("✅ API Key 有效，Embedding 接口可达")
    else:
        print(f"❌ 返回内容：{response.text}")


if __name__ == "__main__":
    main()
