"""住址中→英翻译引擎：Google / 有道官方 API。

供管理后台证件识别模块的「翻译引擎切换按钮」调用，防止 LLM 翻译不准。
任一引擎密钥未配置则该引擎不可用（translate_with 返回空串并 raise ValueError 由调用方处理）。
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

ENGINE_GOOGLE = "google"
ENGINE_YOUDAO = "youdao"
ENGINE_DEEPL = "deepl"
SUPPORTED_ENGINES = (ENGINE_GOOGLE, ENGINE_YOUDAO, ENGINE_DEEPL)


def google_configured() -> bool:
    return bool((settings.google_translate_api_key or "").strip())


def youdao_configured() -> bool:
    return bool(
        (settings.youdao_app_key or "").strip()
        and (settings.youdao_app_secret or "").strip()
    )


def deepl_configured() -> bool:
    return bool((settings.deepl_auth_key or "").strip())


def available_engines() -> list[str]:
    """已配置密钥的引擎列表（供前端决定显示哪些切换按钮）。"""
    out: list[str] = []
    if google_configured():
        out.append(ENGINE_GOOGLE)
    if youdao_configured():
        out.append(ENGINE_YOUDAO)
    if deepl_configured():
        out.append(ENGINE_DEEPL)
    return out


def _translate_google(text: str, source: str = "zh", target: str = "en") -> str:
    """Google Cloud Translation API v2（key 鉴权）。

    文档：https://cloud.google.com/translate/docs/reference/rest/v2/translate
    """
    key = (settings.google_translate_api_key or "").strip()
    if not key:
        raise ValueError("未配置 GOOGLE_TRANSLATE_API_KEY")
    url = "https://translation.googleapis.com/language/translate/v2"
    params = {"key": key, "format": "text"}
    payload = {"q": text, "source": source, "target": target}
    with httpx.Client(timeout=20.0) as client:
        resp = client.post(url, params=params, json=payload)
    if resp.status_code != 200:
        raise ValueError(f"Google 翻译 HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    try:
        return str(data["data"]["translations"][0]["translatedText"]).strip()
    except (KeyError, IndexError, ValueError):
        raise ValueError(f"Google 翻译响应异常: {str(data)[:200]}")


def _translate_youdao(text: str, source: str = "auto", target: str = "en") -> str:
    """有道智云翻译 API（appKey + appSecret，signType=v3 SHA256 签名）。

    文档：https://ai.youdao.com/DOCSIRMD-as/naturalLanguageTranslation/translation/api_s/
    """
    app_key = (settings.youdao_app_key or "").strip()
    app_secret = (settings.youdao_app_secret or "").strip()
    if not app_key or not app_secret:
        raise ValueError("未配置 YOUDAO_APP_KEY / YOUDAO_APP_SECRET")

    salt = str(uuid.uuid4())
    curtime = str(int(time.time()))
    # 输入字段：q<=20 字符用原 q；否则 q 前 10 + 长度 + q 后 10
    if len(text) <= 20:
        sign_input = text
    else:
        sign_input = text[:10] + str(len(text)) + text[-10:]
    raw = app_key + sign_input + salt + curtime + app_secret
    sign = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    params = {
        "q": text,
        "from": source,
        "to": target,
        "appKey": app_key,
        "salt": salt,
        "signType": "v3",
        "curtime": curtime,
        "sign": sign,
    }
    with httpx.Client(timeout=20.0) as client:
        # 有道接受 GET 或 POST(form)；这里用 POST form-data
        resp = client.post("https://openapi.youdao.com/api", data=params)
    if resp.status_code != 200:
        raise ValueError(f"有道翻译 HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    errcode = str(data.get("errorCode", "")).strip()
    if errcode and errcode != "0":
        raise ValueError(f"有道翻译错误 errorCode={errcode}")
    trans = data.get("translation") or []
    if not trans:
        raise ValueError(f"有道翻译响应无 translation: {str(data)[:200]}")
    return str(trans[0]).strip()


def _translate_deepl(text: str, source: str = "ZH", target: str = "EN") -> str:
    """DeepL 翻译 API v2。Free 版 key 以 :fx 结尾走 api-free.deepl.com；Pro 走 api.deepl.com。

    文档：https://developers.deepl.com/docs/api-reference/translate
    """
    key = (settings.deepl_auth_key or "").strip()
    if not key:
        raise ValueError("未配置 DEEPL_AUTH_KEY")
    endpoint = (
        "https://api-free.deepl.com/v2/translate"
        if key.endswith(":fx")
        else "https://api.deepl.com/v2/translate"
    )
    data = {"auth_key": key, "text": text, "source_lang": source, "target_lang": target}
    with httpx.Client(timeout=20.0) as client:
        resp = client.post(endpoint, data=data)
    if resp.status_code != 200:
        raise ValueError(f"DeepL 翻译 HTTP {resp.status_code}: {resp.text[:200]}")
    payload = resp.json()
    trans = payload.get("translations") or []
    if not trans:
        raise ValueError(f"DeepL 翻译响应无 translations: {str(payload)[:200]}")
    return str(trans[0].get("text") or "").strip()


def translate_with(engine: str, text: str) -> str:
    """按引擎翻译中文→英文。成功返回英文，失败 raise ValueError（含原因）。"""
    t = (text or "").strip()
    if not t:
        return ""
    eng = (engine or "").strip().lower()
    if eng == ENGINE_GOOGLE:
        return _translate_google(t)
    if eng == ENGINE_YOUDAO:
        return _translate_youdao(t)
    if eng == ENGINE_DEEPL:
        return _translate_deepl(t)
    raise ValueError(f"不支持的翻译引擎: {engine}（仅 google / youdao / deepl）")
