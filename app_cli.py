"""
命令行交互入口

与旧版本的关键差异：使用 create_travel_agent 构建的「旅游规划 Agent」，
Agent 会根据用户问题自主决定是否调用本地旅游知识库检索工具（RAG），
终端会打印完整的执行轨迹，便于观察 ReAct 思考与工具调用过程。
"""
import logging

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from core.agent import create_travel_agent

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ai-tourism.cli")

QUIT_WORDS = {"exit", "quit", "q", "退出"}
MAX_HISTORY = 10


def print_agent_trace(result):
    """打印 Agent 执行过程中所有消息的详细轨迹"""
    messages = result.get("messages", [])
    print("\n" + "─" * 60)
    print("📋 Agent 执行轨迹:")
    print("─" * 60)

    for index, msg in enumerate(messages):
        position = index + 1

        if isinstance(msg, HumanMessage):
            print(f"  [{position}] 用户输入: {msg.content}")

        elif isinstance(msg, AIMessage):
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                for call in tool_calls:
                    print(f"  [{position}] 决策 -> 调用工具: {call['name']}")
                    print(f"       参数: {call['args']}")
            else:
                preview = str(msg.content)[:100].replace("\n", " ")
                print(f"  [{position}] 回复: {preview}...")

        elif isinstance(msg, ToolMessage):
            preview = str(msg.content)[:160].replace("\n", " ")
            print(f"  [{position}] 工具返回 [{msg.name}]: {preview}...")

        else:
            preview = str(getattr(msg, "content", ""))[:80]
            print(f"  [{position}] {type(msg).__name__}: {preview}...")

    print("─" * 60 + "\n")


def extract_answer(messages):
    """取最后一条含正文的 AI 消息（跳过只带 tool_calls 的中间消息）"""
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue

        content = msg.content
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            text = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
            if text.strip():
                return text

    return "抱歉，我现在无法回答这个问题。"


def main():
    print("正在初始化旅游规划 Agent...")
    try:
        agent = create_travel_agent()
        logger.info("Agent 初始化成功")
    except Exception as exc:
        print(f"初始化失败: {exc}")
        print("请检查 .env 中的 DASHSCOPE_API_KEY 是否正确配置。")
        return

    chat_history = []

    print("\n" + "=" * 52)
    print("旅游助手: 你好！我是你的智能旅游规划助手。")
    print("          告诉我目的地、天数、预算和兴趣偏好，我来帮你安排行程。")
    print("(输入 'exit' 或 'quit' 退出)")
    print("=" * 52 + "\n")

    while True:
        try:
            user_input = input("你: ")
        except (KeyboardInterrupt, EOFError):
            print("\n旅游助手: 祝你旅途愉快，再见！")
            break

        if user_input.strip().lower() in QUIT_WORDS:
            print("旅游助手: 祝你旅途愉快，再见！")
            break

        if not user_input.strip():
            continue

        try:
            messages = chat_history + [HumanMessage(content=user_input)]
            logger.info(f"收到用户输入: {user_input}")

            result = agent.invoke({"messages": messages})

            # 打印执行轨迹，观察 Agent 是否调用了知识库检索
            print_agent_trace(result)

            output = extract_answer(result["messages"])
            print(f"旅游助手: {output}\n")

            # 维护简单的多轮上下文
            chat_history.append(HumanMessage(content=user_input))
            chat_history.append(AIMessage(content=output))
            if len(chat_history) > MAX_HISTORY:
                chat_history = chat_history[-MAX_HISTORY:]

        except Exception as exc:
            logger.error(f"处理请求时出错: {exc}")
            print(f"\n旅游助手: 抱歉，发生了一些错误 - {exc}\n")


if __name__ == "__main__":
    main()
