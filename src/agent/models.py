"""QA Agent 数据模型"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.rag.models import RetrievedChunk


class AnswerMode(str, Enum):
    KNOWLEDGE = "knowledge"
    CONTEXTUAL = "contextual"
    SILENT = "silent"


class AgentAction(str, Enum):
    REPLY = "reply"
    SILENT = "silent"
    ABSTAIN = "abstain"
    HUMAN = "human"


class TaskType(str, Enum):
    QA = "qa"
    MATERIAL = "material"
    HUMAN = "human"


@dataclass
class AgentContext:
    question: str
    roomid: str = ""
    scope: str = "hk"
    history: list[str] = field(default_factory=list)
    group_meta: dict[str, str] = field(default_factory=dict)


@dataclass
class RetrievalEval:
    score: float
    passed: bool
    top_score: float = 0.0
    hit_count: int = 0
    keyword_coverage: float = 0.0
    llm_relevance: float = 0.0
    feedback: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class AnswerEval:
    score: float
    passed: bool
    faithfulness: float = 0.0
    completeness: float = 0.0
    grounded: bool = True
    missing_points: list[str] = field(default_factory=list)
    feedback: str = ""


@dataclass
class AgentTraceStep:
    step: str
    attempt: int
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class QAResult:
    run_id: str
    question: str
    answer: str
    action: AgentAction
    confidence: float
    retrieval_score: float
    answer_score: float
    retries: int
    answer_mode: AnswerMode = AnswerMode.KNOWLEDGE
    hits: list[RetrievedChunk] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    trace: list[AgentTraceStep] = field(default_factory=list)
    needs_human: bool = False
    silent_reason: str = ""

    @staticmethod
    def new_run_id() -> str:
        return str(uuid.uuid4())


ABSTAIN_MESSAGE = (
    "该问题涉及的具体政策或个案情况，建议联系专属服务老师进一步确认，"
    "我已为您记录，稍后会有专员跟进。"
)

HUMAN_MESSAGE = (
    "已为您转接人工服务，专属服务老师将尽快回复您。"
)
