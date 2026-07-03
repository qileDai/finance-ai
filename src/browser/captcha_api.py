"""第三方打码平台 API（2Captcha v2）"""

from __future__ import annotations

import base64
import io
import logging
import time

import httpx

logger = logging.getLogger(__name__)

API_CREATE = "https://api.2captcha.com/createTask"
API_RESULT = "https://api.2captcha.com/getTaskResult"
API_BALANCE = "https://api.2captcha.com/getBalance"


def _merge_gif_frames_min(img) -> "Image.Image":
    """各帧取最暗像素合并，保留分散在不同帧上的字符"""
    from PIL import Image

    n_frames = getattr(img, "n_frames", 1)
    if n_frames <= 1:
        return img.convert("RGB")

    frames = []
    for i in range(n_frames):
        img.seek(i)
        frames.append(img.convert("RGB"))

    w, h = frames[0].size
    merged = Image.new("RGB", (w, h))
    px = merged.load()
    for y in range(h):
        for x in range(w):
            rs, gs, bs = [], [], []
            for f in frames:
                r, g, b = f.getpixel((x, y))
                rs.append(r)
                gs.append(g)
                bs.append(b)
            px[x, y] = (min(rs), min(gs), min(bs))
    return merged


def prepare_image_variants(image_bytes: bytes) -> list[tuple[str, bytes]]:
    """
    生成多种 PNG 供 2Captcha 尝试。
    GIF 动图逐帧增强，避免单帧选错导致识别失败。
    """
    from PIL import Image, ImageEnhance, ImageOps

    variants: list[tuple[str, bytes]] = []

    def to_png(img, label: str) -> None:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        if len(data) > 100 and len(data) < 120_000:
            variants.append((label, data))

    def enhance(img: Image.Image) -> Image.Image:
        w, h = img.size
        scale = max(3, 400 // max(w, 1))
        img = img.resize((w * scale, h * scale), Image.LANCZOS)
        img = ImageOps.autocontrast(img)
        img = ImageEnhance.Contrast(img).enhance(2.2)
        img = ImageEnhance.Sharpness(img).enhance(1.8)
        return img

    try:
        img = Image.open(io.BytesIO(image_bytes))
        n_frames = getattr(img, "n_frames", 1)

        if n_frames > 1:
            to_png(enhance(_merge_gif_frames_min(img)), "merged_min")

            scores: list[tuple[int, int, Image.Image]] = []
            for i in range(n_frames):
                img.seek(i)
                frame = img.convert("RGB")
                gray = ImageOps.grayscale(frame)
                lo, hi = gray.getextrema()
                scores.append((hi - lo, i, frame))
            scores.sort(reverse=True)

            for rank, (_, idx, frame) in enumerate(scores[:3]):
                to_png(enhance(frame), f"frame{idx}")
        else:
            to_png(enhance(img.convert("RGB")), "static")
    except Exception as e:
        logger.warning("图片预处理失败: %s", e)

    if not variants:
        variants.append(("raw", image_bytes))
    return variants


def _create_task(client: httpx.Client, api_key: str, png_bytes: bytes, length: int) -> str:
    body = base64.b64encode(png_bytes).decode()
    payload = {
        "clientKey": api_key,
        "task": {
            "type": "ImageToTextTask",
            "body": body,
            "case": True,
            "numeric": 0,
            "minLength": max(1, length - 1),
            "maxLength": length + 1,
            "comment": (
                f"Type exactly {length} characters shown in the image. "
                "Letters and numbers, case sensitive. Ignore red lines and circles."
            ),
        },
        "languagePool": "en",
    }
    resp = client.post(API_CREATE, json=payload, timeout=30)
    data = resp.json()
    if data.get("errorId", 1) != 0:
        err = data.get("errorDescription") or data.get("errorCode") or data
        if "ZERO_BALANCE" in str(err).upper():
            raise RuntimeError("2Captcha 账户余额不足，请充值")
        raise RuntimeError(f"2Captcha 提交失败: {err}")
    return str(data["taskId"])


def _poll_task(client: httpx.Client, api_key: str, task_id: str, timeout: int) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        resp = client.post(
            API_RESULT,
            json={"clientKey": api_key, "taskId": task_id},
            timeout=30,
        )
        data = resp.json()
        if data.get("errorId", 1) != 0:
            err = data.get("errorDescription") or data
            raise RuntimeError(f"2Captcha 查询失败: {err}")

        status = data.get("status")
        if status == "processing":
            continue
        if status == "ready":
            text = str((data.get("solution") or {}).get("text", "")).strip()
            if text:
                return text
            raise RuntimeError("2Captcha 返回空结果")

    raise TimeoutError("2Captcha 识别超时")


def solve_2captcha_voted(
    image_bytes: bytes,
    api_key: str,
    timeout: int = 90,
    min_len: int = 5,
    max_len: int = 5,
    max_variants: int = 3,
) -> list[tuple[str, str]]:
    """
    并行提交多个 GIF 帧变体到 2Captcha，返回 [(source, code), ...] 供投票。
    source 形如 2captcha:merged_min
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not api_key:
        raise RuntimeError("未配置 TWOCAPTCHA_API_KEY")

    length = max_len or min_len or 5
    variants = prepare_image_variants(image_bytes)[:max_variants]
    if not variants:
        return []

    candidates: list[tuple[str, str]] = []

    def _solve_variant(item: tuple[str, bytes]) -> tuple[str, str] | None:
        label, png_bytes = item
        try:
            with httpx.Client(timeout=30) as client:
                task_id = _create_task(client, api_key, png_bytes, length)
                logger.info("2Captcha 并行任务 %s (%s)", task_id, label)
                text = _poll_task(client, api_key, task_id, timeout=timeout)
                cleaned = "".join(c for c in text if c.isalnum())
                if len(cleaned) >= length:
                    return f"2captcha:{label}", cleaned[:length]
                if len(cleaned) >= length - 1:
                    return f"2captcha:{label}", cleaned
        except Exception as e:
            logger.warning("2Captcha 变体 %s 失败: %s", label, e)
        return None

    workers = min(max_variants, len(variants))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_solve_variant, v) for v in variants]
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                src, code = result
                candidates.append((src, code))
                logger.info("2Captcha [%s]: %s", src, code)

    return candidates


def solve_2captcha(
    image_bytes: bytes,
    api_key: str,
    timeout: int = 90,
    min_len: int = 5,
    max_len: int = 5,
) -> str:
    """通过 2Captcha API v2 识别图形验证码，多帧图片依次尝试"""
    if not api_key:
        raise RuntimeError("未配置 TWOCAPTCHA_API_KEY")

    length = max_len or min_len or 5
    variants = prepare_image_variants(image_bytes)
    last_error = "无可用图片"

    with httpx.Client(timeout=30) as client:
        for label, png_bytes in variants:
            try:
                task_id = _create_task(client, api_key, png_bytes, length)
                logger.info(
                    "2Captcha 任务 %s (%s, PNG %d bytes)",
                    task_id,
                    label,
                    len(png_bytes),
                )
                text = _poll_task(client, api_key, task_id, timeout=timeout)
                logger.info("2Captcha 识别结果 [%s]: %s", label, text)
                cleaned = "".join(c for c in text if c.isalnum())
                if len(cleaned) >= length:
                    return cleaned[:length]
                if len(cleaned) >= length - 1 and label == variants[-1][0]:
                    return cleaned
                if len(cleaned) >= length - 1:
                    last_error = f"结果位数 {len(cleaned)}: {cleaned!r}，尝试下一帧"
                    logger.warning("2Captcha %s", last_error)
                    continue
                last_error = f"结果过短: {text!r}"
            except Exception as e:
                last_error = str(e)
                logger.warning("2Captcha 尝试 %s 失败: %s", label, e)
                continue

    raise RuntimeError(f"2Captcha 全部尝试失败: {last_error}")


def get_balance(api_key: str) -> str:
    with httpx.Client(timeout=15) as client:
        resp = client.post(API_BALANCE, json={"clientKey": api_key}, timeout=15)
        data = resp.json()
        if data.get("errorId", 1) != 0:
            raise RuntimeError(data.get("errorDescription", data))
        return str(data.get("balance", "?"))
