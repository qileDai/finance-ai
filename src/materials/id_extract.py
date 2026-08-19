"""证件识别服务：按选定类型（中国身份证 / 香港身份证 / 护照）抽取字段并翻译补全。"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

ALLOWED_EXPECTED = frozenset({"PRC_ID", "HKID", "PASSPORT", "TW_ID", "SCREENSHOT"})


def run_id_extract(
    *,
    image_bytes: bytes,
    filename: str = "",
    expected_id_type: str = "",
) -> dict[str, Any]:
    """识别一张证件图，返回结构化结果（供管理后台证件识别模块）。

    expected_id_type: PRC_ID | HKID | PASSPORT | TW_ID（可空=自动判别）
    """
    from src.materials.id_document_ocr import enrich_number_from_ocr, extract_id_number_ocr
    from src.materials.id_document_translate import enrich_extracted_fields
    from src.materials.id_document_vision import (
        ID_TYPE_PASSPORT,
        ID_TYPE_PRC,
        ID_TYPE_SCREENSHOT,
        ID_TYPE_TW,
        ID_TYPE_UNKNOWN,
        looks_like_taiwan_address,
        recognize_id_document,
    )

    expected = (expected_id_type or "").strip().upper()
    if expected and expected not in ALLOWED_EXPECTED:
        return {
            "ok": False,
            "error": "expected_id_type 须为 PRC_ID / HKID / PASSPORT / TW_ID / SCREENSHOT",
        }

    # vision + OCR 并行（OCR 不依赖 vision 结果，可提前跑）
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_vision = pool.submit(
            recognize_id_document,
            image_bytes,
            filename=filename or "id.jpg",
            expected_id_type=expected,
        )
        f_ocr = pool.submit(
            extract_id_number_ocr,
            image_bytes,
            hint_type=expected,
        )
        vision = f_vision.result()
        try:
            otype, onum = f_ocr.result()
        except Exception:
            otype, onum = "", ""

    if vision.error == "not_image":
        return {"ok": False, "error": "仅支持图片识别（请上传 JPG/PNG/WEBP）"}
    if vision.error == "no_api_key":
        return {"ok": False, "error": "未配置 OpenAI API Key，无法识别证件"}
    if vision.error == "vision_disabled":
        return {"ok": False, "error": "证件视觉识别已关闭"}

    # 低置信度重判：vision 返回 confidence < 0.7 且有 alternative_types 时，
    # 按第一个 alternative_type 重新识别（仅当当前类型未知/截图时）
    if (
        vision.confidence < 0.7
        and vision.id_type in (ID_TYPE_UNKNOWN, ID_TYPE_SCREENSHOT, "")
    ):
        alt_types = (vision.raw or {}).get("alternative_types") or []
        if alt_types:
            retry_type = str(alt_types[0]).strip().upper()
            if retry_type in (ID_TYPE_HKID, ID_TYPE_PRC, ID_TYPE_PASSPORT, ID_TYPE_TW):
                logger.info(
                    "低置信度 %.2f (type=%s)，按 alternative_type=%s 重新识别",
                    vision.confidence,
                    vision.id_type,
                    retry_type,
                )
                retry_result = recognize_id_document(
                    image_bytes,
                    filename=filename or "id.jpg",
                    expected_id_type=retry_type,
                )
                # 若重判成功且置信度提升，采用重判结果
                if retry_result.confidence > vision.confidence:
                    logger.info(
                        "重判成功：confidence %.2f -> %.2f, type %s -> %s",
                        vision.confidence,
                        retry_result.confidence,
                        vision.id_type,
                        retry_result.id_type,
                    )
                    vision = retry_result

    try:
        # OCR 已有结果直接用；vision 无数码时才跑 enrich（内部会再调 OCR 兜底）
        if not onum:
            otype2, onum2 = enrich_number_from_ocr(
                image_bytes=image_bytes,
                id_type=vision.id_type if vision.id_type != ID_TYPE_UNKNOWN else expected,
                id_number=vision.id_number,
            )
            if onum2:
                onum = onum2
            if otype2 and not otype:
                otype = otype2
        if onum and not vision.id_number:
            vision.id_number = onum
        # 仅类型未知时采用 OCR 类型；已是 TW/HK 等不得被 OCR 改写
        if otype and vision.id_type in (ID_TYPE_UNKNOWN, ""):
            vision.id_type = otype
    except Exception:
        logger.debug("OCR 号码兜底跳过", exc_info=True)

    # 用户指定类型且模型未判出时，采用指定类型
    if expected and vision.id_type in (ID_TYPE_UNKNOWN, ""):
        vision.id_type = expected

    if vision.id_type in (ID_TYPE_UNKNOWN, "", ID_TYPE_SCREENSHOT) and looks_like_taiwan_address(
        vision.address_cn
    ):
        vision.id_type = ID_TYPE_TW

    # 内地证号码末位 X：再从 raw 抢救一次
    if (expected == ID_TYPE_PRC or vision.id_type == ID_TYPE_PRC) and not vision.id_number:
        from src.materials.id_document_vision import salvage_id_number

        raw_num = str((vision.raw or {}).get("id_number") or "")
        salvaged = salvage_id_number(ID_TYPE_PRC, raw_num)
        if salvaged:
            vision.id_number = salvaged
            vision.id_type = ID_TYPE_PRC
    elif (expected == ID_TYPE_PRC or vision.id_type == ID_TYPE_PRC) and vision.id_number:
        from src.materials.id_document_vision import salvage_id_number, validate_id_number

        fixed = salvage_id_number(ID_TYPE_PRC, vision.id_number)
        if fixed and validate_id_number(ID_TYPE_PRC, fixed):
            vision.id_number = fixed

    # 港证号码：从 raw 抢救并格式化为 A123456（7）
    if expected == "HKID" or vision.id_type == "HKID":
        from src.materials.id_document_vision import salvage_id_number, validate_id_number
        import re as _re

        raw_num = str((vision.raw or {}).get("id_number") or vision.id_number or "")
        fixed = salvage_id_number("HKID", raw_num)
        if fixed and (
            validate_id_number("HKID", fixed)
            or _re.match(r"^[A-Z]{1,2}\d{6}", fixed, _re.I)
        ):
            vision.id_number = fixed
            vision.id_type = "HKID"

    # 台证号码：须像身分證字號，拒绝纯数字流水号
    if expected == ID_TYPE_TW or vision.id_type == ID_TYPE_TW:
        from src.materials.id_document_vision import salvage_id_number, tw_number_acceptable

        raw_num = str((vision.raw or {}).get("id_number") or vision.id_number or "")
        fixed = salvage_id_number(ID_TYPE_TW, raw_num)
        if tw_number_acceptable(fixed):
            vision.id_number = fixed
            vision.id_type = ID_TYPE_TW
        elif not tw_number_acceptable(vision.id_number):
            vision.id_number = ""

    type_mismatch = bool(
        expected
        and vision.id_type not in (ID_TYPE_UNKNOWN, "", expected)
        and not (expected == ID_TYPE_PASSPORT and vision.id_type == ID_TYPE_TW)
        and not (expected == ID_TYPE_SCREENSHOT and vision.id_type in (ID_TYPE_SCREENSHOT,))
    )

    # 港/台/截图姓名：仅从 raw 兜底补空
    if not vision.name_cn and (
        expected in ("HKID", ID_TYPE_TW, ID_TYPE_SCREENSHOT)
        or vision.id_type in ("HKID", ID_TYPE_TW, ID_TYPE_SCREENSHOT)
    ):
        from src.materials.id_document_vision import _pick_name_cn

        picked = _pick_name_cn(vision.raw or {})
        if picked:
            vision.name_cn = picked
            from src.materials.id_document_vision import display_name

            vision.full_name = display_name(
                vision.name_cn, vision.name_en, vision.full_name
            )

    # 国内身份证/截图姓名 → 繁体；港台原样
    if vision.name_cn:
        from src.materials.id_document_vision import (
            display_name,
            looks_like_hkid_number,
            should_convert_name_to_traditional,
            to_traditional_name,
        )

        tid = vision.id_type if vision.id_type not in ("", ID_TYPE_UNKNOWN) else expected
        # 港/台绝不转繁（含误判兜底：expected 为港台、或号码已是港证格式）
        if (
            tid in ("HKID", ID_TYPE_TW)
            or expected in ("HKID", ID_TYPE_TW)
            or looks_like_hkid_number(vision.id_number)
        ):
            pass
        elif should_convert_name_to_traditional(tid, vision.issuing_country):
            vision.name_cn = to_traditional_name(vision.name_cn)
            vision.full_name = display_name(
                vision.name_cn, vision.name_en, vision.full_name
            )

    # 内地/台湾住址：极轻量换行漏字纠错（须在翻译前）
    if vision.address_cn and (
        expected in (ID_TYPE_PRC, ID_TYPE_TW)
        or vision.id_type in (ID_TYPE_PRC, ID_TYPE_TW)
    ):
        from src.materials.id_document_translate import repair_prc_address_ocr

        vision.address_cn = repair_prc_address_ocr(vision.address_cn)

    extracted = vision.to_admin_fields()
    if vision.id_type == ID_TYPE_TW:
        extracted.pop("id_type", None)
        if vision.issuing_country:
            extracted["issuing_country"] = vision.issuing_country
    elif vision.id_type == ID_TYPE_SCREENSHOT:
        # 截图不是 ICRIS 证件类型，但 enrich 需要 id_type 来生成英文姓名
        extracted["id_type"] = ID_TYPE_SCREENSHOT
    elif expected in ("PRC_ID", "HKID", "PASSPORT") and not extracted.get("id_type"):
        extracted["id_type"] = expected

    extracted = enrich_extracted_fields(extracted)

    # 截图最终不输出 id_type（不是 ICRIS 证件类型）
    if extracted.get("id_type") == ID_TYPE_SCREENSHOT:
        extracted.pop("id_type", None)

    # 结果展示字段（按证件类型裁剪）
    result = _shape_result(expected or vision.id_type, extracted, vision)

    need_taiwan_id = (
        (expected == ID_TYPE_PASSPORT or vision.id_type == ID_TYPE_PASSPORT)
        and (vision.issuing_country or "").upper() == "TWN"
    ) or vision.id_type == ID_TYPE_TW

    ok_enough = bool(
        vision.classify_ok
        or vision.id_number
        or vision.name_cn
        or vision.name_en
        or vision.address_cn
        or result.get("fields")
    )
    if not ok_enough:
        return {
            "ok": False,
            "error": vision.error or "未能识别证件信息，请换更清晰的图片",
            "vision": {
                "id_type": vision.id_type,
                "confidence": vision.confidence,
                "side": vision.side,
            },
            "type_mismatch": type_mismatch,
        }

    hints = _hints(expected or vision.id_type, vision.issuing_country, need_taiwan_id)
    if type_mismatch:
        hints.insert(
            0,
            f"提示：您选择的是 {_label(expected)}，模型识别为 {_label(vision.id_type)}，请核对图片",
        )

    return {
        "ok": True,
        "expected_id_type": expected,
        "fields": result["fields"],
        "display": result["display"],
        "vision": {
            "id_type": vision.id_type,
            "id_number": vision.id_number,
            "name_cn": vision.name_cn,
            "name_en": vision.name_en,
            "address_cn": vision.address_cn,
            "issuing_country": vision.issuing_country,
            "confidence": vision.confidence,
            "side": vision.side,
            "ok": vision.ok,
            "error": vision.error,
            "type_label": vision.type_label,
        },
        "need_taiwan_id": need_taiwan_id,
        "type_mismatch": type_mismatch,
        "hints": hints,
    }


def _label(id_type: str) -> str:
    return {
        "PRC_ID": "中国身份证",
        "HKID": "香港身份证",
        "PASSPORT": "护照",
        "TW_ID": "台湾身份证",
        "SCREENSHOT": "聊天截图/图片文字",
    }.get((id_type or "").upper(), id_type or "未知")


def _hints(id_type: str, issuing: str, need_taiwan_id: bool) -> list[str]:
    t = (id_type or "").upper()
    hints: list[str] = []
    if t == "PRC_ID":
        hints.append("中国身份证：姓名、号码、住址中文；住址英文已自动翻译")
    elif t == "HKID":
        hints.append("香港身份证：中文名、英文名、香港身份证号码")
    elif t == "PASSPORT":
        hints.append("护照：中文名（可空）、英文名、护照号码")
        if (issuing or "").upper() == "TWN" or need_taiwan_id:
            hints.append("台湾护照：请再选「台湾身份证」上传以提取住址")
    elif t == "TW_ID":
        hints.append("台湾身份证：提取身分證字號与户籍/住址，住址英文已自动翻译")
    elif t == "SCREENSHOT":
        hints.append("聊天截图/图片文字：从中提取姓名和住址，住址英文已自动翻译")
    return hints


def _shape_result(
    id_type: str,
    extracted: dict[str, str],
    vision: Any,
) -> dict[str, Any]:
    """按证件类型整理 fields + 可读 display 列表。"""
    t = (id_type or "").upper()
    fields: dict[str, str] = {}
    display: list[dict[str, str]] = []

    def add(key: str, label: str, value: str) -> None:
        v = (value or "").strip()
        if not v:
            return
        fields[key] = v
        display.append({"key": key, "label": label, "value": v})

    if t == "PRC_ID":
        add("director_name_cn", "姓名", extracted.get("director_name_cn") or vision.name_cn)
        add("id_number", "身份证号码", extracted.get("id_number") or vision.id_number)
        add(
            "director_address_cn",
            "住址中文",
            extracted.get("director_address_cn") or vision.address_cn,
        )
        add("director_address_en", "住址英文", extracted.get("director_address_en", ""))
        fields["id_type"] = "PRC_ID"
    elif t == "HKID":
        add("director_name_cn", "中文名", extracted.get("director_name_cn") or vision.name_cn)
        add("director_name_en", "英文名", extracted.get("director_name_en") or vision.name_en)
        hkid_no = (
            extracted.get("id_number")
            or vision.id_number
            or str((getattr(vision, "raw", None) or {}).get("id_number") or "")
        ).strip()
        add("id_number", "香港身份证号码", hkid_no)
        fields["id_type"] = "HKID"
    elif t == "PASSPORT":
        add("director_name_cn", "中文名", extracted.get("director_name_cn") or vision.name_cn)
        add("director_name_en", "英文名", extracted.get("director_name_en") or vision.name_en)
        add("id_number", "护照号码", extracted.get("id_number") or vision.id_number)
        add(
            "issuing_country",
            "签发地",
            extracted.get("issuing_country") or vision.issuing_country,
        )
        fields["id_type"] = "PASSPORT"
    elif t == "TW_ID":
        add("id_number", "台湾身份证号码", extracted.get("id_number") or vision.id_number)
        add(
            "director_address_cn",
            "住址中文",
            extracted.get("director_address_cn") or vision.address_cn,
        )
        add("director_address_en", "住址英文", extracted.get("director_address_en", ""))
        add("director_name_cn", "姓名", extracted.get("director_name_cn") or vision.name_cn)
    elif t == "SCREENSHOT":
        add("director_name_cn", "姓名", extracted.get("director_name_cn") or vision.name_cn)
        add("director_name_en", "英文名", extracted.get("director_name_en") or vision.name_en)
        add("id_number", "证件号码", extracted.get("id_number") or vision.id_number)
        add(
            "director_address_cn",
            "住址中文",
            extracted.get("director_address_cn") or vision.address_cn,
        )
        add("director_address_en", "住址英文", extracted.get("director_address_en", ""))
    else:
        # 自动：尽量展示全部已抽字段
        add("director_name_cn", "中文名", extracted.get("director_name_cn", ""))
        add("director_name_en", "英文名", extracted.get("director_name_en", ""))
        add("id_number", "证件号码", extracted.get("id_number", ""))
        add("director_address_cn", "住址中文", extracted.get("director_address_cn", ""))
        add("director_address_en", "住址英文", extracted.get("director_address_en", ""))
        if extracted.get("id_type"):
            fields["id_type"] = extracted["id_type"]

    if extracted.get("director_name") and "director_name" not in fields:
        fields["director_name"] = extracted["director_name"]

    return {"fields": fields, "display": display}
