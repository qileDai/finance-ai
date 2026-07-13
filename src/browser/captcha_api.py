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


def _http_client(timeout: int = 30) -> httpx.Client:
    """绕过系统代理，避免 SSL/连接错误"""
    return httpx.Client(timeout=timeout, trust_env=False)


def _merge_gif_frames_max(img) -> "Image.Image":
    """各帧取最亮像素合并"""
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
            px[x, y] = (max(rs), max(gs), max(bs))
    return merged


def _merge_gif_frames_lighter(img) -> "Image.Image":
    """各帧叠加取较亮像素（与 min/max 互补）"""
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
    生成多种图片供 2Captcha 尝试。
    有 Pillow 时做 GIF 多帧合并增强；无 Pillow 时直接提交原始 GIF。
    """
    if not image_bytes or len(image_bytes) < 50:
        return []

    try:
        from PIL import Image, ImageEnhance, ImageOps
    except ImportError:
        logger.warning(
            "未安装 Pillow，2Captcha 将使用原始 GIF（建议: pip install Pillow 提升识别率）"
        )
        return [("raw_gif", image_bytes)]

    variants: list[tuple[str, bytes]] = []
    seen_hashes: set[int] = set()

    def to_png(img, label: str) -> None:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        if len(data) <= 100 or len(data) >= 120_000:
            return
        digest = hash(data)
        if digest in seen_hashes:
            return
        seen_hashes.add(digest)
        variants.append((label, data))

    def enhance(img: Image.Image, *, scale: int = 4) -> Image.Image:
        w, h = img.size
        scale = max(scale, 500 // max(w, 1))
        img = img.resize((w * scale, h * scale), Image.LANCZOS)
        img = ImageOps.autocontrast(img)
        img = ImageEnhance.Contrast(img).enhance(2.5)
        img = ImageEnhance.Sharpness(img).enhance(2.0)
        return img

    def add_gray_variants(rgb: Image.Image, prefix: str) -> None:
        gray = ImageOps.grayscale(rgb)
        to_png(enhance(gray.convert("RGB")), f"{prefix}_gray")
        for threshold in (130, 160, 190):
            bw = gray.point(lambda p, t=threshold: 255 if p > t else 0).convert("RGB")
            to_png(enhance(bw, scale=3), f"{prefix}_bw{t}")

    try:
        img = Image.open(io.BytesIO(image_bytes))
        n_frames = getattr(img, "n_frames", 1)

        if n_frames > 1:
            for merge_fn, label in (
                (_merge_gif_frames_min, "merged_min"),
                (_merge_gif_frames_max, "merged_max"),
                (_merge_gif_frames_lighter, "merged_light"),
            ):
                img.seek(0)
                to_png(enhance(merge_fn(img)), label)
                img.seek(0)
                add_gray_variants(merge_fn(img), label)

            scores: list[tuple[int, int, Image.Image]] = []
            for i in range(n_frames):
                img.seek(i)
                frame = img.convert("RGB")
                gray = ImageOps.grayscale(frame)
                lo, hi = gray.getextrema()
                scores.append((hi - lo, i, frame))
            scores.sort(reverse=True)

            for rank, (_, idx, frame) in enumerate(scores[:4]):
                to_png(enhance(frame), f"frame{idx}")
                if rank < 2:
                    add_gray_variants(frame, f"frame{idx}")
        else:
            rgb = img.convert("RGB")
            to_png(enhance(rgb), "static")
            add_gray_variants(rgb, "static")
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
            "minLength": length,
            "maxLength": length,
            "comment": (
                f"ICRIS Hong Kong government captcha: exactly {length} alphanumeric "
                "characters, CASE SENSITIVE. Animated GIF may show chars on different "
                "frames — read all visible letters and numbers. Ignore red lines, "
                "circles, and background noise."
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
    from config.settings import settings

    deadline = time.time() + timeout
    interval = max(0.8, float(getattr(settings, "twocaptcha_poll_interval", 1.0)))
    first_poll = True
    while time.time() < deadline:
        if not first_poll:
            time.sleep(interval)
        first_poll = False
        resp = client.post(
            API_RESULT,
            json={"clientKey": api_key, "taskId": task_id},
            timeout=15,
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


def solve_2captcha_fast(
    image_bytes: bytes,
    api_key: str,
    timeout: int | None = None,
    min_len: int = 5,
    max_len: int = 5,
) -> tuple[str, str] | None:
    """最快路径：只提交 1 张图，立即轮询，成功即返回 (source, code)"""
    from config.settings import settings

    if not api_key or not image_bytes:
        return None

    length = max_len or min_len or 5
    task_timeout = timeout or settings.twocaptcha_timeout
    variants = prepare_image_variants(image_bytes)
    if not variants:
        return None

    # 优先原始 GIF（ICRIS 动画验证码识别率已足够，且体积最小）
    label, img_bytes = variants[0]
    for item in variants:
        if item[0] in ("raw_gif", "raw"):
            label, img_bytes = item
            break

    try:
        with _http_client() as client:
            task_id = _create_task(client, api_key, img_bytes, length)
            logger.info("2Captcha 快速任务 %s (%s, %d bytes)", task_id, label, len(img_bytes))
            text = _poll_task(client, api_key, task_id, timeout=task_timeout)
            code = _normalize_2captcha_text(text, length)
            if code:
                return f"2captcha:{label}", code
            logger.warning("2Captcha 快速结果无效: %r", text)
    except Exception as e:
        logger.warning("2Captcha 快速识别失败: %s", e)
    return None


def _normalize_2captcha_text(text: str, length: int) -> str | None:
    """清洗 2Captcha 返回，要求恰好 length 位字母数字"""
    cleaned = "".join(c for c in (text or "").strip() if c.isalnum())
    if len(cleaned) < length:
        return None
    code = cleaned[:length]
    if len(code) != length:
        return None
    if code.isalpha() and code.lower() in (
        "white", "black", "image", "captcha", "code", "error", "none",
    ):
        return None
    if len(set(code.lower())) == 1:
        return None
    return code


def solve_2captcha_voted(
    image_bytes: bytes,
    api_key: str,
    timeout: int | None = None,
    min_len: int = 5,
    max_len: int = 5,
    max_variants: int | None = None,
) -> list[tuple[str, str]]:
    """
    并行提交多个 GIF 帧变体到 2Captcha，返回 [(source, code), ...] 供投票。
    source 形如 2captcha:merged_min
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from config.settings import settings

    if not api_key:
        raise RuntimeError("未配置 TWOCAPTCHA_API_KEY")

    length = max_len or min_len or 5
    limit = max_variants or settings.twocaptcha_max_variants
    task_timeout = timeout or settings.twocaptcha_timeout
    variants = prepare_image_variants(image_bytes)[:limit]
    if not variants:
        logger.error("2Captcha: 无可用图片变体")
        return []

    logger.info("2Captcha: 准备提交 %d 个图片变体", len(variants))

    candidates: list[tuple[str, str]] = []

    def _solve_variant(item: tuple[str, bytes]) -> tuple[str, str] | None:
        label, img_bytes = item
        try:
            with _http_client() as client:
                task_id = _create_task(client, api_key, img_bytes, length)
                logger.info("2Captcha 已提交任务 %s (%s, %d bytes)", task_id, label, len(img_bytes))
                text = _poll_task(client, api_key, task_id, timeout=task_timeout)
                code = _normalize_2captcha_text(text, length)
                if code:
                    return f"2captcha:{label}", code
                logger.warning("2Captcha 变体 %s 结果无效: %r", label, text)
        except Exception as e:
            logger.warning("2Captcha 变体 %s 失败: %s", label, e)
        return None

    workers = min(limit, len(variants), 5)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_solve_variant, v) for v in variants]
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                src, code = result
                candidates.append((src, code))
                logger.info("2Captcha [%s]: %s", src, code)
                if limit <= 1:
                    return candidates

    return candidates


def solve_2captcha(
    image_bytes: bytes,
    api_key: str,
    timeout: int | None = None,
    min_len: int = 5,
    max_len: int = 5,
) -> str:
    """通过 2Captcha API v2 识别图形验证码，多帧图片依次尝试"""
    from config.settings import settings

    if not api_key:
        raise RuntimeError("未配置 TWOCAPTCHA_API_KEY")

    length = max_len or min_len or 5
    task_timeout = timeout or settings.twocaptcha_timeout
    variants = prepare_image_variants(image_bytes)
    last_error = "无可用图片"

    with _http_client() as client:
        for label, img_bytes in variants:
            try:
                task_id = _create_task(client, api_key, img_bytes, length)
                logger.info(
                    "2Captcha 任务 %s (%s, %d bytes)",
                    task_id,
                    label,
                    len(img_bytes),
                )
                text = _poll_task(client, api_key, task_id, timeout=task_timeout)
                logger.info("2Captcha 识别结果 [%s]: %s", label, text)
                code = _normalize_2captcha_text(text, length)
                if code:
                    return code
                last_error = f"结果无效: {text!r}"
                logger.warning("2Captcha %s", last_error)
            except Exception as e:
                last_error = str(e)
                logger.warning("2Captcha 尝试 %s 失败: %s", label, e)
                continue

    raise RuntimeError(f"2Captcha 全部尝试失败: {last_error}")


def get_balance(api_key: str) -> str:
    with _http_client(timeout=15) as client:
        resp = client.post(API_BALANCE, json={"clientKey": api_key}, timeout=15)
        data = resp.json()
        if data.get("errorId", 1) != 0:
            raise RuntimeError(data.get("errorDescription", data))
        return str(data.get("balance", "?"))
