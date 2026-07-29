"""OpenAI 大模型客户端"""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI

from config.settings import settings

logger = logging.getLogger(__name__)


class LLMClient:
    """封装 OpenAI 兼容 API，用于智能对话与表单字段推断"""

    def __init__(self) -> None:
        self.client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_api_base,
        )
        self.model = settings.openai_model

    def chat(self, system: str, user: str, temperature: float = 0.3) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
        )
        return response.choices[0].message.content or ""

    def answer_material_question(self, question: str, context: str = "") -> str:
        system = (
            "你是香港公司注册顾问助手，熟悉 ICRIS 电子注册流程及所需材料。"
            "用简洁专业的中文回答客户关于注册材料的问题。"
        )
        if context.strip():
            system += (
                "请优先依据下方「检索片段」作答；片段中未提及的内容不要编造。"
                "若检索片段不足以回答，请明确说明并建议客户联系专员。"
            )
            user = f"检索片段:\n{context.strip()}\n\n客户问题: {question}"
        else:
            system += "若问题超出材料范围，建议客户联系专员。"
            user = question
        return self.chat(system, user)

    def confirm_materials_summary(self, materials: dict[str, Any]) -> str:
        system = (
            "你是香港公司注册专员。根据客户提交的材料，生成一份确认清单摘要，"
            "列出已收到的材料和仍缺失的项目，供客户确认。"
        )
        user = f"客户材料:\n{json.dumps(materials, ensure_ascii=False, indent=2)}"
        return self.chat(system, user)

    def generate_colleague_notification(
        self, company_name: str, summary: str, next_steps: list[str]
    ) -> str:
        system = "你是工商注册流程协调员，生成给内部同事的后续操作提醒。"
        user = (
            f"公司名称: {company_name}\n"
            f"材料摘要: {summary}\n"
            f"后续事项: {', '.join(next_steps)}\n"
            "请生成简洁的工作提醒消息。"
        )
        return self.chat(system, user)

    def solve_captcha_from_image(self, image_base64: str, expected_length: int = 5) -> str:
        """使用视觉模型识别 ICRIS 验证码（区分大小写）"""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"这是香港 ICRIS 网页验证码，共 {expected_length} 个字符。"
                                "字符包含大写/小写字母 A-Z 和数字 0-9，必须区分大小写。"
                                "忽略红色干扰线和圆圈，只读字符本身。"
                                f"只输出 {expected_length} 位验证码，不要空格、标点或解释。"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_base64}"},
                        },
                    ],
                }
            ],
            temperature=0,
        )
        return (response.choices[0].message.content or "").strip()
