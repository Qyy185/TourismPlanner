"""
Agent 核心逻辑模块

负责组装大模型与工具，产出可执行的旅游规划 Agent。
采用 LangChain 1.x 的 create_agent（底层为 LangGraph 运行时）。

支持三档「方案详略」，档位同时决定**模型**与**提示词的篇幅策略**：

| 档位 | 模型 | 篇幅约束 | 实测字数 | 实测总耗时 |
|------|------|----------|----------|-----------|
| fast | qwen3.6-flash | ≤ 700 字 | — | — |
| balanced | qwen-plus | ≤ 1000 字 | 1515 字 | 10.3 秒 |
| detailed | qwen-plus | ≤ 3000 字 | 4670 字 | 46.6 秒 |

> 模型迁移记录（2026-09-28）：阿里云百炼公告部分历史主线模型于 2026-10-10 下线
> （公告 https://cn.aliyun.com/notice/118177），qwen-turbo 在列。快速档已从
> qwen-turbo 迁移到官方推荐的 qwen3.6-flash（同为速度档）；qwen-plus 与
> text-embedding-v3 不在名单，不受影响。
"""
import os

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

from core.prompts import get_system_prompt
from tools.travel_tools import get_tools

# 大模型配置（阿里百炼通义千问，走 OpenAI 兼容模式）
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 三档方案详略：模型 + 界面标注。
# eta 只在实测比较稳定时才填（留空则界面只显示档位名，避免标注与真实耗时对不上）。
PROFILES = {
    "fast": {"label": "⚡ 快速", "model": "qwen3.6-flash", "eta": "约 5 秒"},
    "balanced": {"label": "⚖️ 均衡", "model": "qwen-plus", "eta": ""},
    "detailed": {"label": "📖 详细", "model": "qwen-plus", "eta": ""},
}
DEFAULT_PROFILE = "balanced"


def normalize_profile(profile):
    """把未知档位归到默认档，避免界面传来意外值时崩掉"""
    return profile if profile in PROFILES else DEFAULT_PROFILE


def get_model_name(profile=DEFAULT_PROFILE):
    """当前档位使用的模型名

    环境变量 TRAVEL_MODEL 优先级最高，可用于强制指定模型（此时覆盖所有档位）。
    """
    override = os.environ.get("TRAVEL_MODEL")
    if override:
        return override
    return PROFILES[normalize_profile(profile)]["model"]


def get_llm(temperature: float = 0.3, profile=DEFAULT_PROFILE):
    """初始化通义千问大模型"""
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        raise RuntimeError("未找到 DASHSCOPE_API_KEY，请检查项目根目录下的 .env 文件")
    return ChatOpenAI(
        model=get_model_name(profile),
        api_key=api_key,
        base_url=DASHSCOPE_BASE_URL,
        temperature=temperature,
    )


def create_travel_agent(temperature: float = 0.3, profile=DEFAULT_PROFILE):
    """
    创建旅游规划 Agent

    :param temperature: 生成温度。旅游方案需要一定亲和力，默认 0.3
    :param profile: 方案详略档位，fast / balanced / detailed，未知值回落默认档
    :return: 可执行 invoke/stream 的 Agent
    """
    profile = normalize_profile(profile)
    llm = get_llm(temperature=temperature, profile=profile)
    tools = get_tools()
    return create_agent(llm, tools, system_prompt=get_system_prompt(profile))
