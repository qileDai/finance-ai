"""ICRIS 验证码语音读出 + 粤语/中文语音识别"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from config.settings import settings
from src.browser.captcha_solver import normalize_captcha_text

if TYPE_CHECKING:
    from playwright.async_api import Page, Response

logger = logging.getLogger(__name__)

READ_CAPTCHA_SELECTORS = [
    "a.audioBtn",
    "a:has-text('讀出驗證碼')",
    "a:has-text('读出验证码')",
    ".audioBtn",
]

# 英文数字读法（Whisper 常见输出）
_EN_DIGIT_WORDS: dict[str, str] = {
    "zero": "0", "oh": "0", "\u6d1e": "0",
    "one": "1", "won": "1", "wan": "1",
    "two": "2", "to": "2", "too": "2",
    "three": "3", "tree": "3", "free": "3",
    "four": "4", "for": "4", "fore": "4",
    "five": "5", "fife": "5",
    "six": "6", "sics": "6",
    "seven": "7",
    "eight": "8", "ate": "8", "ait": "8",
    "nine": "9", "niner": "9",
}

# 中文/粤语数字（使用 Unicode 转义，避免编码问题）
_CN_DIGIT_CHARS: dict[str, str] = {
    "\u96f6": "0", "\u3007": "0", "\u9748": "0",
    "\u4e00": "1", "\u58f9": "1", "\u5e7b": "1",
    "\u4e8c": "2", "\u4e24": "2", "\u5169": "2", "\u8d30": "2", "\u8d32": "2",
    "\u4e09": "3", "\u53c1": "3", "\u53c3": "3", "\u53c2": "3",
    "\u56db": "4", "\u8086": "4",
    "\u4e94": "5", "\u4f0d": "5",
    "\u516d": "6", "\u9678": "6", "\u967d": "6",
    "\u4e03": "7", "\u67d2": "7",
    "\u516b": "8", "\u634c": "8",
    "\u4e5d": "9", "\u7396": "9",
}

# 英文字母粤语/中文读法（常用字）
_CN_LETTER_HINTS: dict[str, str] = {
    "\u6bd4": "B", "\u78a7": "B",
    "\u897f": "C", "\u932b": "C", "\u932f": "C",
    "\u5f1f": "D", "\u8fea": "D",
    "\u4f0a": "E", "\u8863": "E",
    "\u4f5b": "F",
    "\u5409": "G", "\u8a18": "G", "\u8bb0": "G",
    "\u6770": "J", "\u5091": "J",
    "\u958b": "K", "\u514b": "K",
    "\u6a02": "L", "\u4e50": "L",
    "\u59c6": "M",
    "\u6069": "N",
    "\u54ed": "O",
    "\u76ae": "P",
    "\u723e": "R",
    "\u4e1d": "S", "\u7d72": "S",
    "\u63d0": "T", "\u7279": "T",
    "\u7dad": "V", "\u7ef4": "V",
    "\u827e": "X", "\u7231": "X",
    "\u5916": "Y", "\u6b6a": "Y",
}


_EN_LETTER_WORDS: dict[str, str] = {
    "ay": "A", "a": "A", "ei": "A", "alpha": "A",
    "bee": "B", "be": "B", "bravo": "B",
    "see": "C", "cee": "C", "sea": "C", "charlie": "C",
    "dee": "D", "delta": "D",
    "ee": "E", "e": "E", "echo": "E",
    "eff": "F", "ef": "F", "foxtrot": "F",
    "gee": "G", "ji": "G", "golf": "G",
    "aitch": "H", "age": "H", "hotel": "H",
    "eye": "I", "i": "I", "india": "I",
    "jay": "J", "j": "J", "juliet": "J",
    "kay": "K", "k": "K", "kilo": "K",
    "el": "L", "ell": "L", "lima": "L",
    "em": "M", "m": "M", "mike": "M",
    "en": "N", "n": "N", "november": "N",
    "oscar": "O",
    "pee": "P", "p": "P", "papa": "P",
    "cue": "Q", "q": "Q", "quebec": "Q",
    "are": "R", "r": "R", "romeo": "R",
    "ess": "S", "s": "S", "sierra": "S",
    "tee": "T", "t": "T", "tango": "T",
    "you": "U", "u": "U", "uniform": "U",
    "vee": "V", "v": "V", "victor": "V",
    "doubleu": "W", "w": "W", "whiskey": "W",
    "ex": "X", "x": "X", "xray": "X",
    "why": "Y", "y": "Y", "yankee": "Y",
    "zee": "Z", "zed": "Z", "z": "Z", "zulu": "Z",
}


def _map_token(token: str) -> str:
    """将单个语音 token 映射为验证码字符"""
    if not token:
        return ""

    t = token.strip()
    if not t:
        return ""

    # 单字符：先映射中文数字/字母读法，避免 isalnum() 把「三」当成普通字符
    if len(t) == 1:
        if t in _CN_DIGIT_CHARS:
            return _CN_DIGIT_CHARS[t]
        if t in _CN_LETTER_HINTS:
            return _CN_LETTER_HINTS[t]
        if t in "0123456789":
            return t
        if t.isascii() and t.isalnum():
            return t
        return ""

    lower = t.lower().strip(".")

    # 英文数字词（仅整词匹配，避免 o57gv 被误判为 0）
    if lower in _EN_DIGIT_WORDS:
        return _EN_DIGIT_WORDS[lower]

    # 连续字母数字串（Whisper 有时输出 O57GV 无分隔）
    if re.fullmatch(r"[A-Za-z0-9]{4,8}", t):
        return t

    # 英文字母读法
    if lower in _EN_LETTER_WORDS:
        return _EN_LETTER_WORDS[lower]

    # 单个中文数字
    if t in _CN_DIGIT_CHARS:
        return _CN_DIGIT_CHARS[t]

    # 中文 token 内匹配（优先长词）
    for cn, ch in sorted(_CN_DIGIT_CHARS.items(), key=lambda x: -len(x[0])):
        if cn in t:
            return ch
    for cn, ch in sorted(_CN_LETTER_HINTS.items(), key=lambda x: -len(x[0])):
        if cn in t:
            return ch

    # B.E.K 这种带点的单字母
    stripped = t.strip(".")
    if len(stripped) == 1 and stripped.isalnum():
        return stripped

    return ""


def parse_spoken_captcha(text: str, expected_len: int = 5) -> str:
    """从语音识别文本逐 token 提取验证码（保留大小写，正确处理数字）"""
    if not text:
        return ""

    chars: list[str] = []

    # 先按分隔符拆 token（避免把 "three" 拆成 t-h-r-e-e）
    tokens = re.split(r"[\s,，、.;；!！?？\-]+", text)
    for token in tokens:
        mapped = _map_token(token)
        if not mapped:
            continue
        if len(mapped) > 1 and re.fullmatch(r"[A-Za-z0-9]+", mapped):
            chars.extend(list(mapped))
        else:
            chars.append(mapped)

    if len(chars) >= expected_len:
        return "".join(chars[:expected_len])

    # 兜底：提取点分单字母 B.E.K.D.W
    dotted = re.findall(r"(?<![A-Za-z0-9])[A-Za-z0-9](?![A-Za-z0-9])", text)
    if len(dotted) >= expected_len:
        return "".join(dotted[:expected_len])

    # 兜底：逐字符扫描中文数字
    for ch in text:
        if ch in _CN_DIGIT_CHARS:
            chars.append(_CN_DIGIT_CHARS[ch])
        elif ch.isascii() and ch.isalnum():
            chars.append(ch)

    # 去重连续误拆：若 chars 来自混合解析，取前 expected_len
    result = "".join(chars)
    if len(result) >= expected_len:
        return result[:expected_len]
    return result


def _llm_parse_transcript(raw: str, expected_len: int, llm_client=None) -> str:
    """用 LLM 从粤语语音转录文本还原验证码"""
    if not llm_client or not raw.strip():
        return ""
    try:
        result = llm_client.chat(
            system=(
                "你是香港 ICRIS 验证码解析助手。"
                "语音用粤语逐个读出5位验证码，包含英文字母（区分大小写）和阿拉伯数字0-9。"
                "数字必须转为阿拉伯数字：三→3，seven→7，zero→0，唔好留中文数字或英文单词。"
                "只输出5位验证码字符，无空格无标点。"
            ),
            user=f"语音识别原文：{raw}\n请输出{expected_len}位验证码：",
            temperature=0,
        )
        code = normalize_captcha_text(result or "", expected_len)
        if code and len(code) >= expected_len - 1:
            logger.info("LLM 解析语音验证码: %s (原文: %r)", code, raw[:80])
            return code[:expected_len] if len(code) >= expected_len else code
    except Exception as e:
        logger.warning("LLM 解析语音失败: %s", e)
    return ""


def transcribe_captcha_audio(
    audio_bytes: bytes,
    expected_len: int = 5,
    llm_client=None,
) -> str:
    """Whisper 多语言识别 + 规则/LLM 解析"""
    if not audio_bytes or len(audio_bytes) < 100:
        raise RuntimeError("验证码音频为空")

    if not settings.openai_api_key:
        raise RuntimeError("语音识别需要 OPENAI_API_KEY（Whisper）")

    from collections import Counter

    from openai import OpenAI

    client = OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_api_base,
    )

    prompt = (
        f"{expected_len} characters captcha. "
        "Cantonese speech reads each character separately. "
        "Use digits 0-9 for numbers, not words. Use letters A-Z for letters."
    )

    raw_lines: list[str] = []
    parsed_candidates: list[str] = []

    for lang in ("zh", "en", None):
        try:
            kwargs: dict = dict(
                model="whisper-1",
                file=("captcha.mp3", audio_bytes, "audio/mpeg"),
                prompt=prompt,
                response_format="text",
            )
            if lang:
                kwargs["language"] = lang
            raw = str(client.audio.transcriptions.create(**kwargs)).strip()
            if raw:
                raw_lines.append(raw)
                logger.info("Whisper(%s): %r", lang or "auto", raw)
                parsed = parse_spoken_captcha(raw, expected_len)
                if parsed:
                    parsed_candidates.append(parsed)
                llm_code = _llm_parse_transcript(raw, expected_len, llm_client)
                if llm_code:
                    parsed_candidates.append(llm_code)
        except Exception as e:
            logger.warning("Whisper(%s) 失败: %s", lang or "auto", e)

    if not parsed_candidates and raw_lines:
        for raw in raw_lines:
            llm_code = _llm_parse_transcript(raw, expected_len, llm_client)
            if llm_code:
                parsed_candidates.append(llm_code)

    if not parsed_candidates:
        raise RuntimeError(f"Whisper 无法识别: {raw_lines!r}")

    full = [c for c in parsed_candidates if len(c) >= expected_len]
    pool = full or parsed_candidates
    best, _ = Counter(pool).most_common(1)[0]
    if len(best) >= expected_len:
        return best[:expected_len]
    if len(best) >= expected_len - 1:
        logger.warning("语音仅识别 %d 位: %s", len(best), best)
        return best
    raise RuntimeError(f"无法解析 {expected_len} 位验证码: {pool}")


def _is_captcha_sound_response(response: "Response") -> bool:
    url = response.url
    return "captcha/sound" in url and "zh-HK.do" in url


async def _get_captcha_id(page: "Page") -> str | None:
    try:
        return await page.evaluate(
            """async () => {
                const csrt = document.querySelector('input[name=csrt]')?.value;
                if (!csrt) return null;
                const prefix = location.pathname.split('/system/')[0];
                const url = prefix + '/system/common/captcha.do?csrt=' + encodeURIComponent(csrt);
                const r = await fetch(url, { credentials: 'include' });
                if (!r.ok) return null;
                const j = await r.json();
                return j?.data?.captchaId || null;
            }"""
        )
    except Exception as e:
        logger.warning("获取 captchaId 失败: %s", e)
        return None


async def _fetch_audio_by_id(page: "Page", captcha_id: str) -> bytes:
    prefix = page.url.split("/system/")[0]
    audio_url = f"{prefix}/system/common/captcha/sound/{captcha_id}/zh-HK.do"
    resp = await page.request.get(audio_url)
    if not resp.ok:
        raise RuntimeError(f"下载语音失败 HTTP {resp.status}")
    body = await resp.body()
    if not body or len(body) < 100:
        raise RuntimeError("验证码语音文件为空")
    logger.info("已下载验证码语音 (%d bytes)", len(body))
    return body


async def click_and_capture_audio(page: "Page", timeout_ms: int = 15000) -> bytes:
    btn = page.locator(", ".join(READ_CAPTCHA_SELECTORS)).first
    if await btn.count() == 0:
        raise RuntimeError("未找到「读出验证码」按钮")

    await btn.scroll_into_view_if_needed()
    await page.wait_for_timeout(300)
    captcha_id = await _get_captcha_id(page)

    try:
        async with page.expect_response(_is_captcha_sound_response, timeout=timeout_ms) as resp_info:
            await btn.click()
        response = await resp_info.value
        body = await response.body()
        if body and len(body) > 100:
            logger.info("已点击读出验证码 (%d bytes)", len(body))
            return body
    except Exception as e:
        logger.warning("点击捕获语音失败: %s", e)

    if captcha_id:
        return await _fetch_audio_by_id(page, captcha_id)
    raise RuntimeError("未能获取验证码语音")


async def solve_captcha_from_audio(
    page: "Page",
    expected_len: int = 5,
    llm_client=None,
) -> str:
    audio = await click_and_capture_audio(page)
    if settings.captcha_save_debug:
        from config.settings import PROJECT_ROOT

        out = PROJECT_ROOT / "output" / "captcha_audio_latest.mp3"
        out.parent.mkdir(exist_ok=True)
        out.write_bytes(audio)
        logger.info("验证码语音已保存: %s", out)

    return transcribe_captcha_audio(audio, expected_len, llm_client=llm_client)
