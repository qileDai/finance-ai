"""OpenAI 大模型客户端"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import OpenAI

from config.settings import settings

logger = logging.getLogger(__name__)


class LLMClient:
    """封装 OpenAI 兼容 API，用于智能对话与表单字段推断"""

    def __init__(
        self,
        *,
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        timeout = float(
            timeout_seconds
            if timeout_seconds is not None
            else (getattr(settings, "openai_timeout_seconds", 20.0) or 20.0)
        )
        self.client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_api_base,
            timeout=timeout,
        )
        self.model = (model or "").strip() or settings.openai_model

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

    def chat_json(self, system: str, user: str, temperature: float = 0.0) -> dict:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("LLM JSON 解析失败: %s", raw[:200])
            return {}

    def generate_answer(
        self,
        question: str,
        context: str,
        *,
        history: list[str] | None = None,
        group_meta: dict[str, str] | None = None,
    ) -> str:
        system = (
            "你是香港公司注册顾问助手，熟悉 ICRIS 电子注册流程及所需材料。"
            "用简洁专业的中文回答客户问题。\n"
            "事实优先级（必须遵守）：\n"
            "1) 「会话材料/状态」中的事实（已收集、证件类型/号码、办理状态）——据实回答，"
            "摘要没有则明确说尚未收到/未识别，禁止编造「您已提交某资料」。\n"
            "2) 「检索片段」中的行业/流程知识（时效、注意事项等）——片段未提及不要编造。\n"
            "3) 「近期对话」用于理解指代（刚才、那个、我的）。\n"
            "若片段中有编号列表或注意事项，请完整保留要点。"
        )
        if re.search(r"注意|要注意|注意事项", question):
            system += (
                "客户询问注意事项，请优先完整列出检索片段中的「注意事项」条目"
                "（含编号列表），不要只回答话术模板或资料清单。"
            )
        if re.search(r"多久|周期|多长时间", question):
            system += (
                "客户询问办理时效，请依据检索片段中的审核周期、工作日等明确时间作答，"
                "直接给出具体时间范围。"
            )
        parts: list[str] = []
        if context.strip():
            parts.append(f"检索片段:\n{context.strip()}")
        if history:
            hist_text = "\n".join(f"- {h}" for h in history[-10:])
            parts.append(f"近期对话:\n{hist_text}")
        if group_meta:
            mat = (group_meta.get("materials_summary") or "").strip()
            if mat:
                parts.append(f"会话材料/状态:\n{mat}")
            job = (group_meta.get("job_status") or "").strip()
            if job:
                parts.append(f"办理快照: {job}")
            meta = ", ".join(
                f"{k}={v}"
                for k, v in group_meta.items()
                if v and k not in ("materials_summary", "job_status")
            )
            if meta:
                parts.append(f"会话信息: {meta}")
        parts.append(f"客户问题: {question}")
        return self.chat(system, "\n\n".join(parts))

    def regenerate_answer(
        self, question: str, context: str, prev_answer: str, feedback: str,
    ) -> str:
        system = (
            "你是香港公司注册顾问助手。上次回答质量不足，请依据检索片段重写。"
            "不要编造，完整覆盖注意事项与编号要点。"
        )
        user = (
            f"检索片段:\n{context.strip()}\n\n"
            f"客户问题: {question}\n\n"
            f"上次回答:\n{prev_answer}\n\n"
            f"改进建议: {feedback}\n\n"
            "请输出修正后的完整回答。"
        )
        return self.chat(system, user, temperature=0.2)

    def judge_retrieval_relevance(
        self, question: str, hits: list,
    ) -> list[float]:
        from src.rag.prompt import format_hits_for_prompt

        context = format_hits_for_prompt(hits)
        system = (
            "你是检索质量评估员。对每条检索片段与问题的相关程度打 1-5 分。"
            '输出 JSON: {"scores": [分数列表，与片段顺序一致]}'
        )
        user = f"问题: {question}\n\n检索片段:\n{context}"
        data = self.chat_json(system, user)
        scores = data.get("scores", [])
        if isinstance(scores, list) and scores:
            return [float(s) for s in scores[: len(hits)]]
        return []

    def rewrite_query(self, question: str, hits: list, feedback: str) -> str:
        from src.rag.prompt import format_hits_for_prompt

        context = format_hits_for_prompt(hits[:3]) if hits else "（无命中）"
        system = (
            "你是检索查询优化助手。根据原问题和已有检索结果，输出更适合知识库检索的查询。"
            'JSON 格式: {"expanded_query": "改写后的查询"}'
        )
        user = (
            f"原问题: {question}\n"
            f"检索反馈: {feedback}\n"
            f"已有片段:\n{context}"
        )
        data = self.chat_json(system, user)
        return str(data.get("expanded_query") or question).strip()

    def generate_contextual_answer(
        self,
        question: str,
        history: list[str] | None = None,
        group_meta: dict[str, str] | None = None,
        hits: list | None = None,
    ) -> dict:
        """无知识库强命中时，结合群上下文与弱检索片段用 LLM 兜底。"""
        from src.rag.prompt import format_hits_for_prompt

        has_hits = bool(hits)
        system = (
            "你是香港公司注册顾问助手，仅回答香港公司注册、开户、材料收集相关问题。"
        )
        if has_hits:
            system += (
                "当前有弱相关检索片段，请优先依据片段作答。"
                "若片段中有明确时效（如审核周期、工作日），可引用；"
                "无依据则不编造具体银行个案政策或费用。"
            )
        else:
            system += (
                "当前无知识库检索片段，可结合对话上下文和通用注册常识简要回答。"
                "若检索片段或注册流程常识中有明确时效，可引用；"
                "无依据则不编造具体银行个案政策。"
            )
        system += (
            "若问题与注册完全无关、或信息不足以准确回答，"
            '必须返回 can_answer=false。'
            '输出 JSON: {"can_answer": true/false, "answer": "...", "reason": "..."}'
        )
        parts: list[str] = [f"客户问题: {question}"]
        if has_hits:
            parts.insert(0, f"参考检索片段:\n{format_hits_for_prompt(hits[:3])}")
        if history:
            hist_text = "\n".join(f"- {h}" for h in history[-10:])
            parts.append(f"近期对话:\n{hist_text}")
        if group_meta:
            meta = ", ".join(f"{k}={v}" for k, v in group_meta.items() if v)
            if meta:
                parts.append(f"群信息: {meta}")
        data = self.chat_json(system, "\n\n".join(parts))
        return {
            "can_answer": bool(data.get("can_answer", False)),
            "answer": str(data.get("answer") or "").strip(),
            "reason": str(data.get("reason") or "").strip(),
        }

    def judge_answer_quality(
        self, question: str, hits: list, answer: str,
    ) -> dict:
        from src.rag.prompt import format_hits_for_prompt

        context = format_hits_for_prompt(hits) if hits else "（无检索片段）"
        system = (
            "你是回答质量评估员。评估回答是否忠实于检索片段、是否完整回答问题。"
            "输出 JSON: "
            '{"faithfulness":0-1,"completeness":0-1,"grounded":true/false,'
            '"missing_points":["..."],"feedback":"重写建议"}'
        )
        user = (
            f"问题: {question}\n\n检索片段:\n{context}\n\n回答:\n{answer}"
        )
        data = self.chat_json(system, user)
        return {
            "faithfulness": float(data.get("faithfulness", 0.5)),
            "completeness": float(data.get("completeness", 0.5)),
            "grounded": bool(data.get("grounded", True)),
            "missing_points": list(data.get("missing_points") or []),
            "feedback": str(data.get("feedback") or ""),
        }

    def summarize_answer(self, question: str, answer: str, max_chars: int) -> str:
        """LLM 摘要压缩回答到指定字数内（优化 5）。

        保留核心结论、关键步骤、编号要点与重要数字。失败时返回原文由调用方截断。
        """
        if max_chars <= 0 or not answer.strip():
            return answer
        system = (
            "你是回答压缩助手。在不丢失核心结论、关键步骤、编号要点和重要数字的前提下，"
            f"将回答压缩到 {max_chars} 个字符以内。保持专业简洁，保留要点编号，"
            "不要添加解释或客套。直接输出压缩后的回答。"
        )
        user = f"客户问题: {question}\n\n原回答:\n{answer}"
        try:
            summary = self.chat(system, user, temperature=0.2).strip()
            if summary:
                return summary
        except Exception as e:
            logger.warning("LLM 摘要失败，回退截断: %s", e)
        return answer

    def check_answer_contradiction(
        self, q1: str, a1: str, q2: str, a2: str
    ) -> bool:
        """判断两个相似问题的回答是否矛盾（优化 4）。返回 True 表示矛盾。"""
        system = (
            "你是回答一致性审核员。判断同一客户两个相似问题的两个回答是否矛盾。"
            "关注核心事实结论（时效、费用、流程步骤、所需材料等）是否冲突；"
            "措辞差异或详略不同不算矛盾。"
            '输出 JSON: {"contradictory": true/false, "reason": "..."}'
        )
        user = (
            f"问题1: {q1}\n回答1:\n{a1}\n\n"
            f"问题2: {q2}\n回答2:\n{a2}\n\n"
            "两个回答的核心事实结论是否矛盾？"
        )
        try:
            data = self.chat_json(system, user)
            return bool(data.get("contradictory", False))
        except Exception as e:
            logger.warning("一致性检查 LLM 调用失败: %s", e)
            return False

    def extract_material_fields_llm(self, text: str) -> dict[str, str]:
        """LLM 从自然语言提取材料字段（优化 10 兜底）。

        返回 dict，key 为标准 field_key，过滤无效 key 与空值。
        """
        system = (
            "你是注册材料信息提取助手。从客户自然语言中提取公司注册相关字段。"
            "可提取字段（仅输出这些 key，无法确定则省略）：\n"
            "company_name_en(公司英文名), company_name_cn(公司中文名), "
            "registered_office(注册地址), contact_email(联络邮箱), "
            "contact_phone(联络电话), founder_members(股东资料), "
            "directors(董事资料), company_secretary(秘书资料), "
            "business_desc(业务性质), br_certificate_years(商业登记证年限 1或3), "
            "applicant_name(申请人姓名), applicant_email(申请人电邮), "
            "applicant_phone(申请人电话), id_type(证件类型 HKID/PRC_ID/PASSPORT), "
            "id_number(证件号码)\n"
            '输出 JSON: {"field_key": "value", ...}，只包含能确定的字段。'
        )
        user = f"客户消息:\n{text}"
        try:
            data = self.chat_json(system, user)
        except Exception as e:
            logger.warning("LLM 字段提取失败: %s", e)
            return {}
        if not isinstance(data, dict):
            return {}
        valid_keys = {
            "company_name_en", "company_name_cn", "registered_office",
            "contact_email", "contact_phone", "founder_members", "directors",
            "company_secretary", "business_desc", "br_certificate_years",
            "applicant_name", "applicant_email", "applicant_phone",
            "id_type", "id_number",
        }
        result: dict[str, str] = {}
        for k, v in data.items():
            if k in valid_keys and isinstance(v, str) and v.strip():
                result[k] = v.strip()
        return result

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
            if re.search(r"注意|要注意|注意事项", question):
                system += (
                    "若客户询问注意事项，请优先完整列出检索片段中的「注意事项」条目"
                    "（含编号列表），不要只回答话术模板或资料清单部分。"
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
