"""验证码识别与预处理"""

from __future__ import annotations

import base64
import io
import logging
import re
from collections import Counter
from typing import Any

logger = logging.getLogger(__name__)

_ocr_default = None
_ocr_beta = None


def _get_ocr(beta: bool = False):
    global _ocr_default, _ocr_beta
    import ddddocr

    if beta:
        if _ocr_beta is None:
            _ocr_beta = ddddocr.DdddOcr(show_ad=False, beta=True)
        return _ocr_beta
    if _ocr_default is None:
        _ocr_default = ddddocr.DdddOcr(show_ad=False, beta=False)
    return _ocr_default


def normalize_captcha_text(text: str, max_length: int | None = None) -> str:
    """清洗 OCR 结果，只保留字母数字"""
    if not text:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", text.strip())
    if max_length and len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    return cleaned


def _composite_gif_frames(img) -> "Image.Image":
    """GIF 动图各帧叠加为静态图，避免字符分散在不同帧"""
    from PIL import Image, ImageChops

    n_frames = getattr(img, "n_frames", 1)
    if n_frames <= 1:
        return img.convert("RGB")

    frames = []
    for i in range(n_frames):
        img.seek(i)
        frames.append(img.convert("RGB"))

    merged = frames[0]
    for frame in frames[1:]:
        merged = ImageChops.lighter(merged, frame)
    return merged


def preprocess_captcha_image(image_bytes: bytes) -> list[bytes]:
    """
    生成多种预处理图供 OCR 投票，提升 ICRIS 彩色扭曲验证码识别率。
    """
    from PIL import Image, ImageEnhance, ImageOps

    variants: list[bytes] = []

    def to_png_bytes(img: Image.Image) -> bytes:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def process_frame(frame_img: Image.Image) -> None:
        img = frame_img.convert("RGB")
        w, h = img.size
        base = img.resize((max(w * 3, 360), max(h * 3, 120)), Image.LANCZOS)

        pipelines: list[Image.Image] = [
            base,
            ImageEnhance.Contrast(base).enhance(2.5),
            ImageEnhance.Sharpness(base).enhance(3.0),
            ImageOps.autocontrast(base),
        ]

        gray = ImageOps.grayscale(base)
        pipelines.extend([
            gray.convert("RGB"),
            ImageEnhance.Contrast(gray).enhance(3.0).convert("RGB"),
        ])

        for threshold in (120, 140, 160, 180, 200):
            bw = gray.point(lambda p, t=threshold: 255 if p > t else 0).convert("RGB")
            pipelines.append(bw)

        for p in pipelines:
            try:
                variants.append(to_png_bytes(p))
            except Exception:
                pass

    try:
        img = Image.open(io.BytesIO(image_bytes))
        n_frames = getattr(img, "n_frames", 1)
        if n_frames > 1:
            process_frame(_composite_gif_frames(img))
            for i in range(min(n_frames, 4)):
                img.seek(i)
                process_frame(img.copy())
        else:
            process_frame(img)
    except Exception as e:
        logger.warning("验证码图片解码失败，使用原始 bytes: %s", e)
        variants.append(image_bytes)

    if not variants:
        variants.append(image_bytes)
    return variants


def _run_ocr_candidates(image_bytes: bytes) -> list[str]:
    """对单张图用多个 OCR 配置识别，收集候选结果"""
    results: list[str] = []
    for beta in (False, True):
        ocr = _get_ocr(beta=beta)
        for png_fix in (True, False):
            try:
                raw = ocr.classification(image_bytes, png_fix=png_fix)
                text = normalize_captcha_text(str(raw))
                if len(text) >= 4:
                    results.append(text)
            except Exception:
                pass
    return results


def _vote_by_position(candidates: list[str], length: int = 4, min_pool: int = 3) -> str:
    """对固定长度候选做逐字符投票，纠正单字符误识"""
    pool = [c for c in candidates if len(c) == length]
    if len(pool) < min_pool:
        return ""

    chars: list[str] = []
    for i in range(length):
        column = [c[i] for c in pool]
        lower_counts = Counter(ch.lower() for ch in column)
        winner_lower = lower_counts.most_common(1)[0][0]
        matching = [ch for ch in column if ch.lower() == winner_lower]
        chars.append(Counter(matching).most_common(1)[0][0])
    return "".join(chars)


def _pick_best_result(candidates: list[str], max_length: int | None = None) -> str:
    """投票选最优结果，过滤 OCR 碎片"""
    candidates = [c for c in candidates if len(c) >= 4]
    if max_length:
        candidates = [c[:max_length] for c in candidates if len(c) >= max_length]

    if not candidates:
        return ""

    len_counts = Counter(len(c) for c in candidates)
    preferred_len = max_length or len_counts.most_common(1)[0][0]
    min_pool = 2 if max_length else 3

    positional = _vote_by_position(candidates, preferred_len, min_pool=min_pool)
    if positional:
        logger.info("验证码逐字符投票(%d位): %s", preferred_len, positional)
        return positional

    pool = [c for c in candidates if len(c) == preferred_len] or candidates

    # 大小写归一合并后选票数最多的写法
    groups: dict[str, list[str]] = {}
    for c in pool:
        groups.setdefault(c.lower(), []).append(c)

    best = ""
    best_score = -1
    for variants in groups.values():
        rep = Counter(variants).most_common(1)[0][0]
        score = len(variants)
        if score > best_score:
            best_score = score
            best = rep

    logger.info(
        "验证码投票: %s (票数=%d, 池大小=%d, 长度分布=%s)",
        best,
        best_score,
        len(pool),
        dict(len_counts),
    )
    return best


def _solve_ocr(
    image_bytes: bytes,
    llm_client=None,
    max_length: int | None = None,
) -> str:
    """本地识别：优先 Ollama 视觉模型，回退 ddddocr"""
    from config.settings import settings

    min_len = max_length or 4
    mode = (settings.captcha_mode or "auto").lower()

    if settings.ollama_vision_model and mode in ("auto", "ollama", "ocr"):
        try:
            from src.browser.captcha_ollama import solve_ollama_vision

            raw = solve_ollama_vision(
                preprocess_captcha_image(image_bytes)[0],
                settings.ollama_base_url,
                settings.ollama_vision_model,
                expected_length=max_length or 5,
            )
            cleaned = normalize_captcha_text(raw, max_length)
            if cleaned and len(cleaned) >= min_len:
                logger.info("Ollama 识别验证码: %s", cleaned)
                return cleaned
        except Exception as e:
            logger.warning("Ollama 视觉识别异常: %s", e)
            if mode == "ollama":
                return ""

    all_candidates: list[str] = []
    for variant in preprocess_captcha_image(image_bytes):
        all_candidates.extend(_run_ocr_candidates(variant))

    best = _pick_best_result(all_candidates, max_length=max_length)
    if best and len(best) >= min_len and re.fullmatch(r"[A-Za-z0-9]+", best):
        if max_length and len(best) != max_length:
            best = best[:max_length]
        logger.info("ddddocr 识别验证码: %s", best)
        return best

    if best:
        logger.warning("OCR 结果格式可疑仍采用: %s", best)
        return best

    if llm_client and settings.openai_api_key:
        png_bytes = preprocess_captcha_image(image_bytes)[0]
        b64 = base64.b64encode(png_bytes).decode()
        try:
            result = llm_client.solve_captcha_from_image(b64)
            cleaned = normalize_captcha_text(result or "", max_length)
            if cleaned and len(cleaned) >= min_len:
                logger.info("LLM 识别验证码: %s", cleaned)
                return cleaned
            logger.warning("LLM 结果不可用: %r", (result or "")[:80])
        except Exception as e:
            logger.warning("LLM 视觉识别异常: %s", e)

    return ""


def solve_ocr_only(
    image_bytes: bytes,
    max_length: int | None = None,
) -> str:
    """仅 ddddocr 多预处理投票，不走 2Captcha / LLM"""
    min_len = max_length or 4
    all_candidates: list[str] = []
    for variant in preprocess_captcha_image(image_bytes):
        all_candidates.extend(_run_ocr_candidates(variant))
    best = _pick_best_result(all_candidates, max_length=max_length)
    if best and len(best) >= min_len:
        return best
    return ""


def _try_2captcha(image_bytes: bytes, max_length: int | None) -> str:
    """调用 2Captcha，成功返回文本，失败返回空字符串"""
    from config.settings import settings

    if not settings.twocaptcha_api_key:
        return ""
    from src.browser.captcha_api import solve_2captcha

    length = max_length or 5
    try:
        raw = solve_2captcha(
            image_bytes,
            settings.twocaptcha_api_key,
            min_len=length,
            max_len=length,
        )
        text = normalize_captcha_text(raw, max_length)
        if not text:
            logger.warning("2Captcha 结果清洗后为空: %r", raw)
            return ""

        if text.isalpha() and text.lower() in (
            "white", "black", "image", "captcha", "code", "error", "none",
        ):
            logger.warning("2Captcha 返回可疑结果，忽略: %s", text)
            return ""
        if len(set(text.lower())) == 1:
            logger.warning("2Captcha 返回重复字符，忽略: %s", text)
            return ""

        if len(text) >= length:
            logger.info("2Captcha 识别验证码: %s", text[:length])
            return text[:length]
        if len(text) >= length - 1:
            logger.info("2Captcha 识别验证码 (%d位): %s", len(text), text)
            return text
    except Exception as e:
        logger.warning("2Captcha 识别失败: %s", e)
    return ""


def try_solve_captcha(
    image_bytes: bytes,
    llm_client=None,
    max_length: int | None = None,
) -> str | None:
    """识别图形验证码，失败返回 None（不抛异常）"""
    try:
        return solve_captcha(image_bytes, llm_client, max_length=max_length)
    except Exception as e:
        logger.warning("图形识别未成功: %s", e)
        return None


def solve_captcha(
    image_bytes: bytes,
    llm_client=None,
    max_length: int | None = None,
) -> str:
    """
    识别验证码，按 captcha_mode 选择策略：
    - 已配置 TWOCAPTCHA_API_KEY 时，auto/2captcha 模式优先走 2Captcha
    - ocr: 仅本地 OCR
    - ollama: 仅 Ollama
    - 2captcha: 仅打码平台
    - auto: 2Captcha（若有 key）→ OCR → 失败由 fill_captcha 等待手动输入
    - manual: 不在此函数处理
    """
    from config.settings import settings

    if not image_bytes or len(image_bytes) < 50:
        raise RuntimeError("验证码图片数据为空或过小")

    mode = (settings.captcha_mode or "auto").lower()

    if mode == "manual":
        raise RuntimeError("manual 模式请在浏览器中手动输入验证码")

    # 配置了 2Captcha 密钥时优先使用（auto / 2captcha）
    if settings.twocaptcha_api_key and mode in ("auto", "2captcha"):
        result = _try_2captcha(image_bytes, max_length)
        if result:
            return result
        if mode == "2captcha":
            raise RuntimeError("2Captcha 识别失败，请检查 TWOCAPTCHA_API_KEY 与账户余额")
        # auto：2Captcha 失败后继续 OCR / LLM
        logger.warning("2Captcha 未识别成功，尝试 OCR/LLM")
        ocr_result = _solve_ocr(image_bytes, llm_client, max_length)
        if ocr_result:
            return ocr_result
        return ""

    if mode == "ollama" and settings.ollama_vision_model:
        from src.browser.captcha_ollama import solve_ollama_vision

        raw = solve_ollama_vision(
            preprocess_captcha_image(image_bytes)[0],
            settings.ollama_base_url,
            settings.ollama_vision_model,
            expected_length=max_length or 5,
        )
        text = normalize_captcha_text(raw, max_length)
        if text:
            return text
        raise RuntimeError("Ollama 识别失败，请确认 ollama serve 已运行且模型已 pull")

    if mode in ("ocr", "auto"):
        ocr_result = _solve_ocr(image_bytes, llm_client, max_length)
        if ocr_result:
            return ocr_result

    raise RuntimeError(
        "验证码识别失败。可改用 CAPTCHA_MODE=manual 手动输入，"
        "或检查 TWOCAPTCHA_API_KEY / Ollama 配置"
    )


def decode_data_url_image(data_url: str) -> bytes | None:
    """从 data:image/...;base64,... 解码原始图片"""
    if not data_url or not data_url.startswith("data:image"):
        return None
    try:
        _, b64 = data_url.split(",", 1)
        return base64.b64decode(b64)
    except Exception as e:
        logger.warning("data URL 解码失败: %s", e)
        return None
