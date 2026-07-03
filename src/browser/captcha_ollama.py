"""Ollama 本地视觉模型识别验证码（免费，需本机安装 Ollama）"""

from __future__ import annotations

import base64
import logging

import httpx

logger = logging.getLogger(__name__)


def solve_ollama_vision(
    image_bytes: bytes,
    base_url: str,
    model: str,
    expected_length: int = 5,
    timeout: int = 90,
) -> str:
    """
    调用 Ollama 多模态模型识别验证码。
    推荐模型: qwen2.5vl:7b / llava:13b / minicpm-v
    安装: https://ollama.com  →  ollama pull qwen2.5vl:7b
    """
    b64 = base64.b64encode(image_bytes).decode()
    prompt = (
        f"这是网页图形验证码图片，包含 {expected_length} 个英文字母或数字字符。"
        f"请只输出这 {expected_length} 个字符，不要空格、标点或任何解释。"
    )

    url = base_url.rstrip("/") + "/api/chat"
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            url,
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt, "images": [b64]}],
                "stream": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = (data.get("message") or {}).get("content") or ""
        logger.info("Ollama(%s) 原始输出: %r", model, text[:80])
        return text.strip()


def ollama_available(base_url: str) -> bool:
    try:
        with httpx.Client(timeout=5) as client:
            r = client.get(base_url.rstrip("/") + "/api/tags")
            return r.status_code == 200
    except Exception:
        return False
