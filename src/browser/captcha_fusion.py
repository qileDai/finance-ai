"""多源验证码结果融合（2Captcha 图形 + 语音 + OCR）"""

from __future__ import annotations

import logging
import re
from collections import Counter

logger = logging.getLogger(__name__)

SOURCE_PRIORITY = {
    "2captcha": 0,
    "image": 0,
    "audio": 1,
    "ocr": 2,
    "llm": 3,
}


def _source_priority(src: str) -> int:
    if src.startswith("2captcha"):
        return 0
    return SOURCE_PRIORITY.get(src, 99)

# 常见 OCR/语音混淆对（小写）
_AMBIGUOUS_PAIRS: set[frozenset[str]] = {
    frozenset({"o", "0"}),
    frozenset({"i", "1", "l"}),
    frozenset({"s", "5"}),
    frozenset({"b", "8"}),
    frozenset({"g", "6"}),
    frozenset({"z", "2"}),
}


def _normalize_code(code: str, expected_len: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", code or "")
    return cleaned[:expected_len] if len(cleaned) >= expected_len else cleaned


def _is_ambiguous_pair(a: str, b: str) -> bool:
    pair = frozenset({a.lower(), b.lower()})
    return pair in _AMBIGUOUS_PAIRS


def merge_captcha_candidates(
    candidates: list[tuple[str, str]],
    expected_len: int = 5,
) -> str | None:
    """
    融合多路识别结果。
    candidates: [(source, code), ...]，source 如 2captcha / audio / ocr
    """
    valid: list[tuple[str, str]] = []
    for source, raw in candidates:
        code = _normalize_code(raw, expected_len)
        if len(code) >= expected_len:
            valid.append((source, code[:expected_len]))
        elif len(code) >= expected_len - 1:
            valid.append((source, code))

    if not valid:
        return None

    full = [(s, c) for s, c in valid if len(c) >= expected_len]
    if len(full) == 1:
        src, code = full[0]
        logger.info("验证码采用唯一完整结果 [%s]: %s", src, code)
        return code

    if len(full) >= 2:
        codes = [c for _, c in full]
        winner, votes = Counter(codes).most_common(1)[0]
        if votes >= 2:
            logger.info("验证码多源一致 (%d/%d): %s", votes, len(full), winner)
            return winner

        ordered = sorted(full, key=lambda x: _source_priority(x[0]))
        primary_src, primary = ordered[0]
        secondary_src, secondary = ordered[1]

        if primary == secondary:
            logger.info("验证码 [%s/%s] 一致: %s", primary_src, secondary_src, primary)
            return primary

        merged: list[str] = []
        disagreements: list[str] = []
        for i in range(expected_len):
            chars: list[tuple[int, str, str]] = []
            for src, code in ordered:
                if i < len(code):
                    chars.append((_source_priority(src), src, code[i]))
            chars.sort(key=lambda x: x[0])

            votes_at = Counter(c for _, _, c in chars)
            best_char, best_votes = votes_at.most_common(1)[0]
            if best_votes >= 2:
                merged.append(best_char)
                continue

            pri_char = chars[0][2]
            sec_char = chars[1][2] if len(chars) > 1 else pri_char
            if pri_char != sec_char:
                disagreements.append(f"{i}:{pri_char}/{sec_char}")
            merged.append(pri_char)

        result = "".join(merged)
        logger.info(
            "验证码融合 [%s+%s]: %s (分歧位: %s)",
            primary_src,
            secondary_src,
            result,
            ",".join(disagreements) or "无",
        )
        return result

    # 仅有不足 5 位的候选：取优先级最高且最长的
    ordered = sorted(valid, key=lambda x: (-len(x[1]), _source_priority(x[0])))
    src, code = ordered[0]
    if len(code) >= expected_len - 1:
        logger.warning("验证码仅识别 %d 位 [%s]: %s", len(code), src, code)
        return code if len(code) >= expected_len else None
    return None


def pick_best_captcha(
    candidates: list[tuple[str, str]],
    expected_len: int = 5,
) -> str | None:
    """
    从多源结果中选出最佳验证码。
    优先：多源完全一致 > 2Captcha 完整结果 > 融合 > 语音完整 > OCR
    """
    merged = merge_captcha_candidates(candidates, expected_len)
    if merged and len(merged) >= expected_len:
        return merged[:expected_len]

    for prefer in ("2captcha", "image", "audio", "ocr", "llm"):
        for src, raw in candidates:
            if prefer == "2captcha":
                if not src.startswith("2captcha"):
                    continue
            elif src != prefer:
                continue
            code = _normalize_code(raw, expected_len)
            if len(code) >= expected_len:
                logger.info("验证码回退采用 [%s]: %s", src, code[:expected_len])
                return code[:expected_len]

    return merged


def assess_confidence(
    candidates: list[tuple[str, str]],
    result: str,
    expected_len: int = 5,
) -> str:
    """
    评估识别置信度。
    - high: 2+ 源完全一致，或 2+ 个 2Captcha 变体一致
    - medium: 仅 1 个可靠源（2Captcha / LLM）
    - low: 多源明显分歧
    """
    if not result or len(result) < expected_len:
        return "low"

    result = result[:expected_len]
    full = [
        (src, _normalize_code(code, expected_len))
        for src, code in candidates
        if len(_normalize_code(code, expected_len)) >= expected_len
    ]
    if not full:
        return "low"

    exact_matches = sum(1 for _, code in full if code[:expected_len] == result)
    if exact_matches >= 2:
        return "high"

    twocap_codes = [
        code[:expected_len]
        for src, code in full
        if src.startswith("2captcha") and len(code) >= expected_len
    ]
    if twocap_codes:
        winner, votes = Counter(twocap_codes).most_common(1)[0]
        if votes >= 2 and winner == result:
            return "high"

    if exact_matches == 1:
        src = next(s for s, c in full if c[:expected_len] == result)
        if src.startswith("2captcha") or src == "llm":
            if len(full) == 1:
                return "medium"
            others = [c for _, c in full if c[:expected_len] != result]
            max_diff = max(
                sum(1 for i in range(expected_len) if result[i] != other[i])
                for other in others
            )
            return "medium" if max_diff <= 1 else "low"

    return "low"
