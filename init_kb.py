"""
知识库一键初始化入口

流程：
1. 校验 .env 中的 DASHSCOPE_API_KEY 是否已配置
2. 构建旅游知识库向量库（加载 → 切片 → 向量化 → 持久化）

用法：
    python init_kb.py
"""
import os
import sys

from dotenv import load_dotenv

# 必须先加载 .env，后续模块才读得到 API Key
load_dotenv()


def check_api_key():
    """检查 API Key 是否已配置（只做存在性与格式检查，不发起网络请求）"""
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()

    if not api_key:
        print("❌ 未找到 DASHSCOPE_API_KEY")
        print("   请复制 .env.example 为 .env，并填入你的阿里百炼 API Key")
        return False

    if not api_key.startswith("sk-"):
        print("⚠️  DASHSCOPE_API_KEY 格式可能不正确（正常应以 sk- 开头），仍将继续尝试")

    print(f"✅ 已读取 API Key：{api_key[:8]}...{api_key[-4:]}")
    return True


def main():
    print("=" * 58)
    print("  智慧旅游规划助手 · 知识库初始化")
    print("=" * 58)

    if not check_api_key():
        sys.exit(1)

    print()
    # 延迟导入：确保 load_dotenv() 已生效
    from build_tourism_kb import build_tourism_knowledge_base

    build_tourism_knowledge_base()

    print()
    print("=" * 58)
    print("  初始化完成，接下来可以：")
    print("    python app_cli.py      # 命令行交互")
    print("    python app_gradio.py   # Web 图形界面")
    print("=" * 58)


if __name__ == "__main__":
    main()
