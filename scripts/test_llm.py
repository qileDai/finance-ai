"""快速验证 OpenAI 连接与配置"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import settings
from src.llm.openai_client import LLMClient


def main() -> None:
    print(f"API Base: {settings.openai_api_base}")
    print(f"Model:    {settings.openai_model}")
    print(f"Key:      {settings.openai_api_key[:8]}...")

    llm = LLMClient()
    answer = llm.chat(
        "你是助手",
        "用一句话说明香港公司注册需要什么材料",
    )
    print(f"\nLLM 回复: {answer}")
    print("\n[OK] OpenAI 连接正常")


if __name__ == "__main__":
    main()
