"""董事个人住址：香港/非香港、街道与区省市、住址国家。只看董事住址，不看注册地址。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from src.materials.countries import (
    normalize_address_country,
    passport_country_option_names,
)

logger = logging.getLogger(__name__)

S03_DISTRICTS_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "icris_s03_districts.json"
)
_S03_DISTRICTS: list[dict[str, Any]] | None = None

# ICRIS 非香港住址四栏（室／楼／座、大厦、街道、区市省）含空格均最多 60 字
ICRIS_ADDR_FIELD_MAX = 60

CLASSIFY_HK_DISTRICT_SYSTEM = (
    "你只根据董事兼股东的个人住址，从给定的 ICRIS 下拉选项里选一个香港郵遞區號／区。"
    "不要看注册地址、公司名。"
    '只输出 JSON：{"district":"选项原文"}。'
    "规则："
    "1) district 必须与选项列表中某一项完全一致，如「香港仔」「紅磡」「天水圍」。"
    "2) TIN SHUI WAI / NT / N.T. / 天水圍 选「天水圍」或「元朗」，不要选香港仔。"
    "3) 不要因为住址没有 Hong Kong 四字就选香港仔。"
    "4) 选项对不上时不要编造；不要输出其它键。"
)

_HK_DISTRICT_RULES: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    (re.compile(r"tin\s*shui\s*wai|天水[圍围]", re.I), ("天水圍", "元朗")),
    (re.compile(r"yuen\s*long|元朗", re.I), ("元朗",)),
    (re.compile(r"tuen\s*mun|屯[門门]", re.I), ("屯門",)),
    (re.compile(r"sha\s*tin|沙田", re.I), ("沙田",)),
    (re.compile(r"kwun\s*tong|觀塘|观塘", re.I), ("觀塘",)),
    (re.compile(r"tsuen\s*wan|荃灣|荃湾", re.I), ("荃灣",)),
    (re.compile(r"kwai\s*chung|葵涌|葵青", re.I), ("葵涌", "葵芳")),
    (re.compile(r"tai\s*po|大埔", re.I), ("大埔",)),
    (re.compile(r"fanling|粉嶺|粉岭", re.I), ("粉嶺",)),
    (re.compile(r"sheung\s*shui|上水", re.I), ("上水",)),
    (re.compile(r"tseung\s*kwan\s*o|將軍澳|将军澳", re.I), ("將軍澳", "西貢")),
    (re.compile(r"sai\s*kung|西貢|西贡", re.I), ("西貢",)),
    (re.compile(r"tung\s*chung|東涌|东涌", re.I), ("東涌", "離島")),
    (re.compile(r"mong\s*kok|旺角", re.I), ("旺角",)),
    (re.compile(r"tsim\s*sha\s*tsui|尖沙[咀嘴]", re.I), ("尖沙咀",)),
    (re.compile(r"sham\s*shui\s*po|深水埗", re.I), ("深水埗",)),
    (re.compile(r"wong\s*tai\s*sin|黃大仙|黄大仙", re.I), ("黃大仙",)),
    (re.compile(r"causeway\s*bay|銅鑼灣|铜锣湾", re.I), ("銅鑼灣", "灣仔")),
    (re.compile(r"wan\s*chai|灣仔|湾仔", re.I), ("灣仔",)),
    (re.compile(r"aberdeen|香港仔", re.I), ("香港仔",)),
    (re.compile(r"hung\s*hom|紅磡|红磡", re.I), ("紅磡",)),
]

CLASSIFY_ADDRESS_SYSTEM = (
    "你只根据董事兼股东的个人住址（标签可能是住址或地址）的英文正文判断，"
    "不要看注册地址、公司名或其它字段。"
    "只输出 JSON："
    '{"is_hk":true|false,"flat":"...","building":"...","street":"...","region":"...","address_country":"ISO3"}。'
    "规则："
    "1) is_hk 仅当该个人住址在香港特区。"
    "Hong Kong / HK / H.K. / HKG / HKSAR / Kowloon / KLN / "
    "New Territories / NT / N.T. / Tin Shui Wai / Kwun Tong 等均为香港，is_hk=true。"
    "内地、澳门、台湾、国外都是 false。注册地址在香港不能当成个人住址在香港。"
    "2) 香港（is_hk=true）必须拆四段，所有逗号段必须进入其中一段，禁止丢掉樓/座。"
    "末尾 HK / H.K. / HKG / KLN / NT / N.T. / Hong Kong / Kowloon 只表示香港，"
    "不要写入 flat/building/street。"
    "flat=室／樓／座（FLT/Flat/RM/Room/Shop、11/F、G/F、LG/F、BLK/Block、"
    "Phase/Wing、Tower+座号 全部并入 flat）；"
    "building=大廈（House/HSE、Building/BLDG、Court、Mansion、Centre、Plaza 等），"
    "没有大厦则空；Estate/EST 不是大厦；"
    "street=街道／屋苑／地段／村（Estate/EST、Tsuen/Village、Road 等），"
    "不要把大厦或郵遞區放进 street；无 Road 时街道可以只有屋苑名；"
    "region 必须从用户消息里的郵遞區號选项抄一项原文，如「天水圍」「紅磡」「觀塘」「柴灣」。"
    "禁止自造 TIN SHUI WAI NT 或只写区英文。"
    "address_country=HKG。"
    "例："
    "RM D, 11/F, BLK 5, LOCWOOD COURT, 1 TIN WU ROAD, TIN SHUI WAI NT → "
    'flat="RM D, 11/F, BLK 5", building="LOCWOOD COURT", '
    'street="1 TIN WU ROAD", region="天水圍"；'
    "Flat A, 9/F, Tai Yip Street, Kwun Tong, Kowloon, Hong Kong → "
    'flat="Flat A, 9/F", building="", street="Tai Yip Street", region="觀塘"；'
    "Shop 3, G/F, Hang Seng Building, 83 Des Voeux Road Central, Central → "
    'flat="Shop 3, G/F", building="Hang Seng Building", '
    'street="83 Des Voeux Road Central", region="中環"；'
    "FLT 2505, 25/F, MAN FU HOUSE, HING MAN ESTATE, CHAI WAN, HK → "
    'flat="FLT 2505, 25/F", building="MAN FU HOUSE", '
    'street="HING MAN ESTATE", region="柴灣"。'
    "3) 非香港（内地/国外）不要套香港四段：flat 与 building 输出空字符串。"
    "street=市县级以下全部逗号段（室/门牌、路、镇/乡、村、片区、社区、团场/连等）；"
    "region=从第一段市县级及以上起到省/州/邮编："
    "District/County/Banner/City/League/Prefecture/Province/State/"
    "Governorate/Emirate/New Area/Autonomous Region 等。"
    "Town/Township/Village/2nd Area/Community/Sub-district/Barangay/MFY 必须留在 street。"
    "所有逗号段必须进入 street 或 region，禁止丢掉中间段；不要把国家名放进 region。"
    "例："
    "Room 110, No. 8, Xili South Road, Nanshan District, Shenzhen City, Guangdong Province → "
    'street="Room 110, No. 8, Xili South Road", '
    'region="Nanshan District, Shenzhen City, Guangdong Province"；'
    "No. 5, Xizhi Lane, Qiaoqian 2nd Area, Jinzao Town, Chaoyang District, "
    "Shantou City, Guangdong Province → "
    'street="No. 5, Xizhi Lane, Qiaoqian 2nd Area, Jinzao Town", '
    'region="Chaoyang District, Shantou City, Guangdong Province"；'
    "House 8, 2nd Company, 14th Regiment, Alar City, "
    "Xinjiang Uyghur Autonomous Region → "
    'street="House 8, 2nd Company, 14th Regiment", '
    'region="Alar City, Xinjiang Uyghur Autonomous Region"；'
    "39-uy, Zevarsoy kochasi, Xamkorobod MFY, Yunusabad district, Tashkent city → "
    'street="39-uy, Zevarsoy kochasi, Xamkorobod MFY", '
    'region="Yunusabad district, Tashkent city"；'
    "123 Main Street, Springfield, IL 62704 → "
    'street="123 Main Street", region="Springfield, IL 62704"；'
    "Apt 5, 1-2-3 Jingumae, Shibuya-ku, Tokyo → "
    'street="Apt 5, 1-2-3 Jingumae", region="Shibuya-ku, Tokyo"。'
    "4) address_country 用 ISO 3166-1 alpha-3。"
    "内地 CHN，香港 HKG，澳门 MAC，台湾 TWN；国外用对应国家（如 UZB）。"
    "5) 无英文住址则各键空/false。不要输出其它键或解释。"
)

FIT_NON_HK_SYSTEM = (
    "你把非香港住址拆进 ICRIS 四个输入框。每栏含空格最多 60 个字符，禁止超长，"
    "禁止丢掉原文逗号段，不要缩写。"
    "只输出 JSON："
    '{"flat":"...","building":"...","street":"...","region":"..."}。'
    "先保持市县级边界：street=室/门牌/路/镇/社区等；"
    "region=District/City/Province/State/Autonomous Region。"
    "若 street 超过 60：把前端 Room/Rm/Flat/Apt/Unit/No.+门牌 放入 flat；"
    "再把含 Building/Mansion/Plaza/Tower/Centre 的段放入 building；"
    "Community/Street/Road/Lane/Town 留在 street。"
    "若 region 超过 60：把左侧更细的镇/市/盟/地区/自治州段并入 street；"
    "若 street 已满则并入 building 或 flat；region 尽量保留 City+Province/"
    "State/Autonomous Region。"
    "四栏都必须 ≤60。不要输出其它键。"
)

_HK_EN_RE = re.compile(
    r"hong\s*kong|kowloon|new\s*territories|\bhksar\b|\bhk\s*island\b|"
    r"\bhkg\b|\bkln\b|\bhk\b|\bh\.k\.?|\bN\.?\s*T\.?\b|"
    r"tin\s*shui\s*wai|yuen\s*long|tuen\s*mun|sha\s*tin|kwun\s*tong|"
    r"tsuen\s*wan|kwai\s*chung|tai\s*po|fanling|sheung\s*shui|"
    r"tseung\s*kwan\s*o|sai\s*kung|tung\s*chung|mong\s*kok|"
    r"tsim\s*sha\s*tsui|sham\s*shui\s*po|wong\s*tai\s*sin|"
    r"causeway\s*bay|wan\s*chai|aberdeen|hung\s*hom",
    re.I,
)
_HK_CN_RE = re.compile(r"香港|九[龍龙]|新界")
_NON_HK_EN_RE = re.compile(
    r"shenzhen|guangdong|beijing|shanghai|nanshan|hangzhou|guangzhou|"
    r"uzbekistan|tashkent|province\b",
    re.I,
)
_ADMIN_PART_RE = re.compile(
    r"\b(districts?|city|cities|province|state|prefecture|"
    r"count(?:y|ies)|territory|postal(?:\s*code)?|zip(?:\s*code)?|"
    r"region|oblast|viloyat|"
    r"banners?|leagues?|governorates?|emirates?|cantons?|"
    r"departments?|municipalit(?:y|ies)|krais?|voivodeships?)\b",
    re.I,
)
_SUBDISTRICT_RE = re.compile(r"sub[-\s]?districts?", re.I)
_NEW_AREA_RE = re.compile(r"\bnew\s+area\b", re.I)
_HK_REGION_TAIL_RE = re.compile(
    r"hong\s*kong|kowloon|new\s*territories|\bhksar\b|\bN\.?\s*T\.?\b|"
    r"\bhkg\b|\bkln\b|\bhk\b|\bh\.k\.?",
    re.I,
)
_FLAT_PART_RE = re.compile(
    r"^(rm|room|flat|flt|unit|apt|ste|suite|shop|office|"
    r"blk|block|phase|ph\.?|wing|tower)\b|"
    r"^(lg|ug|g|m|u)\s*/\s*f\b|"
    r"^\d+\s*/\s*f\b|"
    r"^\d+\s*(st|nd|rd|th)?\s*(fl\.?|floor)\b|"
    r"^(室|座|樓|楼)",
    re.I,
)
_BLDG_PART_RE = re.compile(
    r"\b(court|crt|mansion|building|bldg|bld|tower|twr|"
    r"centre|center|plaza|gardens?|gdns?|"
    r"house|hse|villa|heights|residence|park)\b",
    re.I,
)
_STREET_PART_RE = re.compile(
    r"\b(road|rd\.?|street|st\.?|avenue|ave\.?|lane|path|"
    r"drive|dr\.?|terrace|highway|circuit|"
    r"estates?|est\.?|vill(?:age)?)\b",
    re.I,
)
_NON_HK_NO_FLAT_RE = re.compile(r"^no\.\s*\d+[a-z]?\s*$", re.I)
_NON_HK_HASH_FLAT_RE = re.compile(r"^#\s*\d+[a-z]?\s*$", re.I)
_NON_HK_BLDG_RE = re.compile(
    r"\b(building|mansion|plaza|tower|centre|center)\b",
    re.I,
)
_NON_HK_STREET_KEEP_RE = re.compile(
    r"\b(community|street|road|lane|village|town|township|"
    r"road|rd\.?|avenue|ave\.?)\b",
    re.I,
)


def _empty_address() -> dict[str, str]:
    return {
        "director_address_flat": "",
        "director_address_building": "",
        "director_address_street": "",
        "director_address_region": "",
        "address_country": "",
        "address_is_hk": "0",
    }


def load_s03_district_options(
    *, path: Path | None = None, reload: bool = False
) -> list[dict[str, Any]]:
    """ICRIS s03 香港「郵遞區號」下拉原文（如 香港仔、天水圍）。"""
    global _S03_DISTRICTS
    target = path or S03_DISTRICTS_PATH
    if path is None and _S03_DISTRICTS is not None and not reload:
        return _S03_DISTRICTS
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        rows: list[Any] = []
    else:
        rows = raw.get("options") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        rows = []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    skip = re.compile(r"請選擇|请选择|^Select$|^--+", re.I)
    for item in rows:
        if isinstance(item, str):
            label, value, aliases = item.strip(), item.strip(), []
        elif isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            value = str(item.get("value") or label).strip() or label
            raw_aliases = item.get("aliases") or []
            aliases = (
                [str(a).strip() for a in raw_aliases if str(a or "").strip()]
                if isinstance(raw_aliases, list)
                else []
            )
        else:
            continue
        if not label or skip.search(label) or label in seen:
            continue
        seen.add(label)
        out.append({"label": label, "value": value, "aliases": aliases})
    if path is None:
        _S03_DISTRICTS = out
    return out


def s03_district_labels(*, path: Path | None = None) -> list[str]:
    return [
        str(row["label"])
        for row in load_s03_district_options(path=path)
        if row.get("label")
    ]


def english_address_is_hk(address_en: str) -> bool:
    """只根据董事个人英文住址关键字判断是否香港特区。"""
    return bool(_HK_EN_RE.search(address_en or ""))


def chinese_address_is_hk(address_cn: str) -> bool:
    return bool(_HK_CN_RE.search(address_cn or ""))


def english_address_looks_non_hk(address_en: str) -> bool:
    """董事英文住址明显是内地城市/省或国外（用于否决模型误报香港）。"""
    en = address_en or ""
    if english_address_is_hk(en):
        return False
    if _NON_HK_EN_RE.search(en):
        return True
    parts = [p.strip() for p in re.split(r"[,，]", en) if p.strip()]
    if parts:
        iso = normalize_address_country(parts[-1])
        if iso and iso not in ("CHN", "HKG", "MAC", "TWN"):
            return True
    return False


def _hk_district_blob(address_en: str, region: str = "", address_cn: str = "") -> str:
    return " ".join(p for p in (address_en, region, address_cn) if (p or "").strip())


def aberdeen_district_allowed(
    address_en: str, region: str = "", address_cn: str = ""
) -> bool:
    blob = _hk_district_blob(address_en, region, address_cn)
    return bool(re.search(r"aberdeen|香港仔|南區|南区", blob, re.I))


def hk_district_select_candidates(
    address_en: str, region: str = "", address_cn: str = ""
) -> list[str]:
    """董事住址 → ICRIS 繁体区/郵遞區號候选（不含万能香港仔）。"""
    blob = _hk_district_blob(address_en, region, address_cn)
    out: list[str] = []
    for pat, names in _HK_DISTRICT_RULES:
        if not pat.search(blob):
            continue
        for name in names:
            if name == "香港仔" and not aberdeen_district_allowed(
                address_en, region, address_cn
            ):
                continue
            if name not in out:
                out.append(name)
    return out


def classify_hk_district_user_prompt(
    address_en: str, options: list[str], address_cn: str = ""
) -> str:
    opts = "、".join(options[:80])
    addr = (address_en or "").strip() or (address_cn or "").strip() or "（无）"
    return (
        "请只根据下面董事个人住址，从选项里选一项。\n\n"
        f"住址：\n{addr}\n\n选项：\n{opts}"
    )


def _match_district_in_options(name: str, options: list[str]) -> str:
    raw = (name or "").strip()
    if not raw or not options:
        return ""
    if raw in options:
        return raw
    for o in options:
        if raw in o or o in raw:
            return o
    return ""


def resolve_s03_hk_district(
    raw: str = "",
    *,
    address_en: str = "",
    address_cn: str = "",
    region: str = "",
) -> str:
    """把区名/英文住址映射到 ICRIS 郵遞區號下拉原文。对不上则空，不默认香港仔。"""
    labels = s03_district_labels()
    if not labels:
        return ""
    allow_aberdeen = aberdeen_district_allowed(address_en, region or raw, address_cn)

    def _ok(name: str) -> str:
        hit = _match_district_in_options(name, labels)
        if hit and "香港仔" in hit and not allow_aberdeen:
            return ""
        return hit

    seed = (raw or region or "").strip()
    hit = _ok(seed)
    if hit:
        return hit

    for cand in hk_district_select_candidates(
        address_en, region or raw, address_cn
    ):
        hit = _ok(cand)
        if hit:
            return hit

    blob = _hk_district_blob(address_en, region or raw, address_cn).lower()
    if not blob:
        return ""
    best = ""
    best_len = 0
    for row in load_s03_district_options():
        label = str(row.get("label") or "").strip()
        if not label:
            continue
        if "香港仔" in label and not allow_aberdeen:
            continue
        aliases = [label, *(row.get("aliases") or [])]
        for alias in aliases:
            a = str(alias or "").strip()
            if len(a) < 3:
                continue
            if a.lower() in blob and len(a) > best_len:
                best = label
                best_len = len(a)
    return best


def pick_hk_district_from_options(
    address_en: str,
    options: list[str] | None = None,
    *,
    region: str = "",
    address_cn: str = "",
    llm: Any | None = None,
) -> str:
    """关键字命中且在下拉里的优先；否则 LLM 从选项里挑。不默认香港仔。"""
    opts = [str(o).strip() for o in (options or []) if str(o or "").strip()]
    opts = [o for o in opts if o and not re.search(r"請選擇|请选择|^Select$", o, re.I)]
    if not opts:
        opts = s03_district_labels()
    allow_aberdeen = aberdeen_district_allowed(address_en, region, address_cn)

    def _ok(name: str) -> str:
        hit = _match_district_in_options(name, opts) if opts else (name or "").strip()
        if hit and "香港仔" in hit and not allow_aberdeen:
            return ""
        return hit

    mapped = resolve_s03_hk_district(
        region, address_en=address_en, address_cn=address_cn, region=region
    )
    hit = _ok(mapped) if mapped else ""
    if hit:
        return hit

    for cand in hk_district_select_candidates(address_en, region, address_cn):
        hit = _ok(cand)
        if hit:
            return hit

    if not opts:
        return ""
    try:
        client = llm
        data: Any = None
        if client is not None and hasattr(client, "pick_hk_district"):
            data = client.pick_hk_district(address_en or address_cn, opts)
        elif client is not None and hasattr(client, "chat_json"):
            data = client.chat_json(
                CLASSIFY_HK_DISTRICT_SYSTEM,
                classify_hk_district_user_prompt(
                    address_en, opts, address_cn=address_cn
                ),
                temperature=0.0,
            )
        else:
            data = None
        if isinstance(data, dict):
            hit = _ok(str(data.get("district") or data.get("region") or ""))
            if hit:
                return hit
    except Exception as exc:
        logger.warning("香港区名 LLM 选择失败: %s", exc)
    return ""


def classify_address_user_prompt(address_en: str) -> str:
    en = (address_en or "").strip()
    labels = s03_district_labels()
    opts = "、".join(labels)
    if not en:
        return "请判定住址。证件相关行:（无英文住址）"
    return (
        "请只根据下面这一行英文住址拆分并判定类型，忽略其它资料。"
        "若是香港住址，region 必须从下列郵遞區號选项抄一项原文，禁止自造。\n\n"
        f"住址英文:\n{en}\n\n"
        f"香港郵遞區號选项:\n{opts}"
    )


def fit_non_hk_address_user_prompt(
    flat: str, building: str, street: str, region: str
) -> str:
    return (
        "请把下列四栏拆到每栏不超过 60 字，不要丢掉逗号段。\n\n"
        f"室／楼／座:\n{flat or '（空）'}\n\n"
        f"大厦:\n{building or '（空）'}\n\n"
        f"街道／屋苑／地段／村:\n{street or '（空）'}\n\n"
        f"区／市／省／州／邮递区号:\n{region or '（空）'}"
    )


def _english_address_body_parts(address_en: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"[,，]", address_en or "") if p.strip()]
    if not parts:
        return []
    country = normalize_address_country(parts[-1])
    return parts[:-1] if country and len(parts) > 1 else parts


def _part_starts_region(part: str) -> bool:
    """市县级及以上逗号段：District/City/Banner 等；Sub-district、2nd Area 不算。"""
    if _SUBDISTRICT_RE.search(part):
        return False
    if _NEW_AREA_RE.search(part):
        return True
    return bool(_ADMIN_PART_RE.search(part))


def split_english_street_region(address_en: str) -> tuple[str, str]:
    """英文住址：从第一段市县级及以上行政单位起到末尾为 region。"""
    body = _english_address_body_parts(address_en)
    if not body:
        return "", ""
    idx = next((i for i, p in enumerate(body) if _part_starts_region(p)), -1)
    if idx >= 0:
        return ", ".join(body[:idx]), ", ".join(body[idx:])
    if len(body) >= 3:
        return ", ".join(body[:-2]), ", ".join(body[-2:])
    if len(body) == 2:
        return body[0], body[1]
    return body[0], ""


def split_hk_english_four_way(address_en: str) -> tuple[str, str, str, str]:
    """香港英文住址弱拆：室/楼/座、大厦、街道、区（区为下拉原文或未映射原文）。"""
    address = (address_en or "").strip()
    if not address:
        return "", "", "", ""
    parts = [p.strip() for p in re.split(r"[,，]", address) if p.strip()]
    region_parts: list[str] = []
    while parts:
        last = parts[-1]
        if _STREET_PART_RE.search(last):
            break
        named = bool(resolve_s03_hk_district(last))
        if named or _HK_REGION_TAIL_RE.search(last):
            region_parts.insert(0, parts.pop())
            continue
        break
    region_raw = ", ".join(region_parts)
    region = resolve_s03_hk_district(
        region_raw, address_en=address, region=region_raw
    )

    flat_parts: list[str] = []
    while parts and _FLAT_PART_RE.search(parts[0]):
        flat_parts.append(parts.pop(0))

    building = ""
    bldg_idx = -1
    for i, part in enumerate(parts):
        if _BLDG_PART_RE.search(part) and not _STREET_PART_RE.search(part):
            bldg_idx = i
    if bldg_idx >= 0:
        building = parts[bldg_idx]
        parts = parts[:bldg_idx] + parts[bldg_idx + 1 :]
    street = ", ".join(parts)
    flat = ", ".join(flat_parts)
    return flat, building, street, region


def _is_truncated_vs_rule(llm_val: str, rule_val: str) -> bool:
    """LLM 字段是规则结果的前缀/真子集（缺了中间段或市省）。"""
    a = re.sub(r"\s+", " ", (llm_val or "").strip().lower()).rstrip(",")
    b = re.sub(r"\s+", " ", (rule_val or "").strip().lower()).rstrip(",")
    if not b or a == b:
        return False
    if not a:
        return True
    if b.startswith(a) and len(b) > len(a):
        return True
    a_parts = [p.strip() for p in a.split(",") if p.strip()]
    b_parts = [p.strip() for p in b.split(",") if p.strip()]
    if a_parts and all(p in b_parts for p in a_parts) and len(b_parts) > len(a_parts):
        return True
    return False


def _region_is_truncated(llm_region: str, rule_region: str) -> bool:
    """LLM 的 region 是规则 region 的前缀/真子集（缺了市省）。"""
    return _is_truncated_vs_rule(llm_region, rule_region)


def _street_is_truncated(llm_street: str, rule_street: str) -> bool:
    """LLM 的 street 是规则 street 的前缀/真子集（缺了镇/片区等）。"""
    return _is_truncated_vs_rule(llm_street, rule_street)


def _dropped_comma_parts(
    address_en: str,
    street: str,
    region: str,
    flat: str = "",
    building: str = "",
) -> bool:
    """原文（不含国名）有逗号段未出现在 flat+building+street+region。"""
    combined = f"{flat}, {building}, {street}, {region}".lower()
    combined = re.sub(r"\s+", " ", combined)
    for part in _english_address_body_parts(address_en):
        token = re.sub(r"\s+", " ", part.strip().lower())
        if token and token not in combined:
            return True
    return False


def _hk_region_tail_part(part: str) -> bool:
    """郵遞區／港九新界尾段，不算香港四段丢失。"""
    token = (part or "").strip()
    if not token:
        return True
    if _HK_REGION_TAIL_RE.search(token):
        return True
    return bool(resolve_s03_hk_district(token))


def _hk_dropped_address_parts(
    address_en: str, flat: str, building: str, street: str
) -> bool:
    """原文里非区名逗号段未出现在 flat+building+street。"""
    combined = re.sub(r"\s+", " ", f"{flat}, {building}, {street}".lower())
    parts = [p.strip() for p in re.split(r"[,，]", address_en or "") if p.strip()]
    for part in parts:
        if _hk_region_tail_part(part):
            continue
        token = re.sub(r"\s+", " ", part.strip().lower())
        if token and token not in combined:
            return True
    return False


def _truthy_hk(raw: Any) -> bool:
    if raw is True:
        return True
    s = str(raw or "").strip().lower()
    return s in ("1", "true", "yes", "hk")


def clip_icris_addr(value: str) -> str:
    """ICRIS 单栏截到 60 字（含空格）。"""
    return (value or "")[:ICRIS_ADDR_FIELD_MAX]


def clip_icris_addr_fields(
    flat: str, building: str, street: str, region: str
) -> tuple[str, str, str, str]:
    return (
        clip_icris_addr(flat),
        clip_icris_addr(building),
        clip_icris_addr(street),
        clip_icris_addr(region),
    )


def _csv_addr_parts(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,，]", text or "") if p.strip()]


def _join_addr_parts(parts: list[str]) -> str:
    return ", ".join(p for p in parts if p)


def _append_addr_part(dest: str, part: str) -> str:
    part = (part or "").strip()
    dest = (dest or "").strip()
    if not part:
        return dest
    dest_l = {p.lower() for p in _csv_addr_parts(dest)}
    if part.lower() in dest_l:
        return dest
    return f"{dest}, {part}" if dest else part


def _addr_part_fits(dest: str, part: str) -> bool:
    return len(_append_addr_part(dest, part)) <= ICRIS_ADDR_FIELD_MAX


def _is_non_hk_flat_part(part: str) -> bool:
    token = (part or "").strip()
    if not token:
        return False
    if _NON_HK_NO_FLAT_RE.match(token) or _NON_HK_HASH_FLAT_RE.match(token):
        return True
    if _STREET_PART_RE.search(token) and re.match(r"^no\.", token, re.I):
        return False
    return bool(_FLAT_PART_RE.search(token))


def _is_non_hk_building_part(part: str) -> bool:
    token = (part or "").strip()
    if not token:
        return False
    if _STREET_PART_RE.search(token) or _NON_HK_STREET_KEEP_RE.search(token):
        return False
    return bool(_NON_HK_BLDG_RE.search(token))


def _addr_fields_overflow(flat: str, building: str, street: str, region: str) -> bool:
    return any(
        len(x or "") > ICRIS_ADDR_FIELD_MAX
        for x in (flat, building, street, region)
    )


def _addr_parts_preserved(
    before: tuple[str, str, str, str], after: tuple[str, str, str, str]
) -> bool:
    blob = re.sub(r"\s+", " ", ", ".join(after).lower())
    for field in before:
        for part in _csv_addr_parts(field):
            token = re.sub(r"\s+", " ", part.lower())
            if token and token not in blob:
                return False
    return True


def _non_hk_llm_fit_fields_ok(
    flat: str, building: str, street: str, region: str
) -> bool:
    """LLM 不能把街道/社区或区市省写进室、大厦。"""
    del street, region
    for field in (flat, building):
        for part in _csv_addr_parts(field):
            if _NON_HK_STREET_KEEP_RE.search(part):
                return False
            if _ADMIN_PART_RE.search(part):
                return False
    return True


def _weak_fit_non_hk_address_fields(
    flat: str, building: str, street: str, region: str
) -> tuple[str, str, str, str]:
    """无 LLM：街道超长从前剥室/大厦；区市省超长从左并入街道。"""
    flat, building, street, region = (
        (flat or "").strip(),
        (building or "").strip(),
        (street or "").strip(),
        (region or "").strip(),
    )
    for _ in range(48):
        if not _addr_fields_overflow(flat, building, street, region):
            return flat, building, street, region

        if len(street) > ICRIS_ADDR_FIELD_MAX:
            parts = _csv_addr_parts(street)
            if not parts:
                street = clip_icris_addr(street)
                continue
            if _is_non_hk_flat_part(parts[0]) and _addr_part_fits(flat, parts[0]):
                flat = _append_addr_part(flat, parts.pop(0))
                street = _join_addr_parts(parts)
                continue
            moved = False
            for i, part in enumerate(parts):
                if _is_non_hk_building_part(part) and _addr_part_fits(
                    building, part
                ):
                    building = _append_addr_part(building, parts.pop(i))
                    street = _join_addr_parts(parts)
                    moved = True
                    break
            if moved:
                continue
            first = parts[0]
            if _addr_part_fits(flat, first):
                flat = _append_addr_part(flat, parts.pop(0))
                street = _join_addr_parts(parts)
                continue
            if _addr_part_fits(building, first):
                building = _append_addr_part(building, parts.pop(0))
                street = _join_addr_parts(parts)
                continue
            street = clip_icris_addr(street)
            continue

        if len(region) > ICRIS_ADDR_FIELD_MAX:
            parts = _csv_addr_parts(region)
            if len(parts) <= 1:
                region = clip_icris_addr(region)
                continue
            left = parts[0]
            rest = _join_addr_parts(parts[1:])
            if _addr_part_fits(street, left):
                street = _append_addr_part(street, left)
                region = rest
                continue
            if _addr_part_fits(building, left):
                building = _append_addr_part(building, left)
                region = rest
                continue
            if _addr_part_fits(flat, left):
                flat = _append_addr_part(flat, left)
                region = rest
                continue
            street = _append_addr_part(street, left)
            region = rest
            continue

        if len(flat) > ICRIS_ADDR_FIELD_MAX:
            parts = _csv_addr_parts(flat)
            if len(parts) <= 1:
                flat = clip_icris_addr(flat)
                continue
            last = parts[-1]
            if _addr_part_fits(building, last):
                building = _append_addr_part(building, last)
                flat = _join_addr_parts(parts[:-1])
                continue
            if _addr_part_fits(street, last):
                street = _append_addr_part(street, last)
                flat = _join_addr_parts(parts[:-1])
                continue
            flat = clip_icris_addr(flat)
            continue

        if len(building) > ICRIS_ADDR_FIELD_MAX:
            parts = _csv_addr_parts(building)
            if len(parts) <= 1:
                building = clip_icris_addr(building)
                continue
            last = parts[-1]
            if _addr_part_fits(street, last):
                street = _append_addr_part(street, last)
                building = _join_addr_parts(parts[:-1])
                continue
            if _addr_part_fits(flat, last):
                flat = _append_addr_part(flat, last)
                building = _join_addr_parts(parts[:-1])
                continue
            building = clip_icris_addr(building)
            continue

    return clip_icris_addr_fields(flat, building, street, region)


def _parse_fit_llm_payload(data: Any) -> tuple[str, str, str, str] | None:
    if not isinstance(data, dict) or not data:
        return None
    return (
        str(data.get("flat") or data.get("director_address_flat") or "").strip(),
        str(
            data.get("building") or data.get("director_address_building") or ""
        ).strip(),
        str(data.get("street") or data.get("director_address_street") or "").strip(),
        str(data.get("region") or data.get("director_address_region") or "").strip(),
    )


def _try_llm_fit_non_hk(
    llm: Any,
    flat: str,
    building: str,
    street: str,
    region: str,
) -> tuple[str, str, str, str] | None:
    if llm is None:
        return None
    data: Any = None
    try:
        fn = getattr(llm, "fit_non_hk_address_fields", None)
        if callable(fn):
            data = fn(flat, building, street, region)
            parsed = _parse_fit_llm_payload(data)
            if parsed is not None:
                return parsed
        chat = getattr(llm, "chat_json", None)
        if callable(chat):
            data = chat(
                FIT_NON_HK_SYSTEM,
                fit_non_hk_address_user_prompt(flat, building, street, region),
                temperature=0.0,
            )
            return _parse_fit_llm_payload(data)
    except Exception as exc:
        logger.warning("非香港住址 60 字拆分 LLM 失败: %s", exc)
        return None
    return _parse_fit_llm_payload(data)


def fit_non_hk_address_fields(
    flat: str,
    building: str,
    street: str,
    region: str,
    *,
    llm: Any | None = None,
) -> tuple[str, str, str, str]:
    """任一栏超过 60 字才拆；输出四段均 ≤60，尽量不丢逗号段。llm=None 只走弱兜底。"""
    orig = (
        (flat or "").strip(),
        (building or "").strip(),
        (street or "").strip(),
        (region or "").strip(),
    )
    if not _addr_fields_overflow(*orig):
        return orig
    llm_out = _try_llm_fit_non_hk(llm, *orig)
    if llm_out is not None:
        fitted = _weak_fit_non_hk_address_fields(*llm_out)
        if (
            not _addr_fields_overflow(*fitted)
            and _addr_parts_preserved(orig, fitted)
            and _non_hk_llm_fit_fields_ok(*fitted)
        ):
            return fitted
    return _weak_fit_non_hk_address_fields(*orig)


def apply_non_hk_fit_to_result(
    out: dict[str, str], *, llm: Any | None = None
) -> dict[str, str]:
    """非香港结果再按 60 字拆室/大厦；香港原样返回。"""
    if not out or str(out.get("address_is_hk") or "") == "1":
        return out
    flat, building, street, region = fit_non_hk_address_fields(
        str(out.get("director_address_flat") or ""),
        str(out.get("director_address_building") or ""),
        str(out.get("director_address_street") or ""),
        str(out.get("director_address_region") or ""),
        llm=llm,
    )
    updated = dict(out)
    updated["director_address_flat"] = flat
    updated["director_address_building"] = building
    updated["director_address_street"] = street
    updated["director_address_region"] = region
    return updated


def prepare_icris_fill_address(addr: dict[str, str] | None) -> dict[str, str]:
    """填表用：香港原样；非香港四栏仅硬截 60，不再拆。"""
    out = dict(addr or {})
    if str(out.get("address_is_hk") or "") == "1":
        return out
    out["flat"] = clip_icris_addr(str(out.get("flat") or ""))
    out["building"] = clip_icris_addr(str(out.get("building") or ""))
    out["street"] = clip_icris_addr(str(out.get("street") or ""))
    out["region"] = clip_icris_addr(str(out.get("region") or ""))
    return out


def coerce_address_result(
    data: Any, address_en: str = "", address_cn: str = ""
) -> dict[str, str]:
    en = (address_en or "").strip()
    cn = (address_cn or "").strip()
    if not isinstance(data, dict):
        data = {}
    flat = str(
        data.get("flat") or data.get("director_address_flat") or ""
    ).strip()
    building = str(
        data.get("building") or data.get("director_address_building") or ""
    ).strip()
    street = str(
        data.get("street") or data.get("director_address_street") or ""
    ).strip()
    region = str(
        data.get("region") or data.get("director_address_region") or ""
    ).strip()
    llm_hk = _truthy_hk(
        data.get("is_hk") if "is_hk" in data else data.get("address_is_hk")
    )
    keyword_hk = english_address_is_hk(en) or chinese_address_is_hk(cn)
    if not en and not cn:
        is_hk = False
    elif keyword_hk:
        is_hk = True
    elif english_address_looks_non_hk(en):
        is_hk = False
    else:
        is_hk = llm_hk

    country = normalize_address_country(str(data.get("address_country") or ""))
    if is_hk:
        country = "HKG"
    if not country and en:
        country = _guess_country_from_en(en)
    if is_hk:
        country = country or "HKG"
    elif english_address_looks_non_hk(en):
        guessed = _guess_country_from_en(en)
        if guessed:
            country = guessed
    elif not country and english_address_looks_greater_china(en):
        country = _guess_country_from_en(en) or "CHN"

    if is_hk:
        fb_flat, fb_bldg, fb_street, fb_region = split_hk_english_four_way(en)
        if not en:
            fb_region = resolve_s03_hk_district("", address_cn=cn) or fb_region
        if flat or building or street:
            # 模型已拆则保留；只给空栏补规则，禁止整段覆盖
            if _hk_dropped_address_parts(en, flat, building, street):
                flat = flat or fb_flat
                building = building or fb_bldg
                street = street or fb_street
        elif en:
            flat, building, street = fb_flat, fb_bldg, fb_street
        region = resolve_s03_hk_district(
            region or fb_region,
            address_en=en,
            address_cn=cn,
            region=region or fb_region,
        )
    else:
        if en and (not street and not region):
            street, region = split_english_street_region(en)
        elif en:
            rule_street, rule_region = split_english_street_region(en)
            if rule_street or rule_region:
                if (
                    (rule_region and _region_is_truncated(region, rule_region))
                    or _street_is_truncated(street, rule_street)
                    or _dropped_comma_parts(en, street, region, flat, building)
                ):
                    street, region = rule_street, rule_region
                    flat, building = "", ""
        region = _strip_country_token(region, country)
    return {
        "director_address_flat": flat,
        "director_address_building": building,
        "director_address_street": street,
        "director_address_region": region,
        "address_country": country,
        "address_is_hk": "1" if is_hk else "0",
    }


def english_address_looks_greater_china(address_en: str) -> bool:
    t = (address_en or "").lower()
    return bool(
        re.search(
            r"\bchina\b|taiwan|taipei|macao|macau|hong\s*kong|kowloon|"
            r"guangdong|shenzhen|beijing|shanghai|province",
            t,
        )
    )


def _guess_country_from_en(address_en: str) -> str:
    parts = [p.strip() for p in re.split(r"[,，]", address_en or "") if p.strip()]
    for token in reversed(parts):
        iso = normalize_address_country(token)
        if iso:
            return iso
    return normalize_address_country(address_en) or ""


def _strip_country_token(region: str, country_iso: str) -> str:
    if not region or not country_iso:
        return region
    names = [n.lower() for n in passport_country_option_names(country_iso) if n]
    names.extend([country_iso.lower(), "uzbekistan"])
    parts = [p.strip() for p in re.split(r"[,，]", region) if p.strip()]
    kept = [p for p in parts if p.lower() not in names and p.lower() not in {"prc"}]
    return ", ".join(kept)


def weak_fallback_address(address_en: str) -> dict[str, str]:
    en = (address_en or "").strip()
    if not en:
        return _empty_address()
    is_hk = english_address_is_hk(en)
    if is_hk:
        flat, building, street, region = split_hk_english_four_way(en)
        return {
            "director_address_flat": flat,
            "director_address_building": building,
            "director_address_street": street,
            "director_address_region": region,
            "address_country": "HKG",
            "address_is_hk": "1",
        }
    street, region = split_english_street_region(en)
    country = _guess_country_from_en(en)
    if not country and english_address_looks_greater_china(en):
        country = "CHN"
    region = _strip_country_token(region, country)
    return {
        "director_address_flat": "",
        "director_address_building": "",
        "director_address_street": street,
        "director_address_region": region,
        "address_country": country,
        "address_is_hk": "0",
    }


def classify_director_address(
    address_en: str,
    address_cn: str = "",
    *,
    llm: Any | None = None,
) -> dict[str, str]:
    """只把董事个人住址交给 LLM；不看注册地址。"""
    en = (address_en or "").strip()
    cn = (address_cn or "").strip()
    if not en:
        fb = weak_fallback_address("")
        if chinese_address_is_hk(cn):
            fb["address_is_hk"] = "1"
            fb["address_country"] = fb.get("address_country") or "HKG"
            fb["director_address_region"] = resolve_s03_hk_district(
                "", address_cn=cn
            )
        return fb
    try:
        client = llm
        data: Any = None
        if client is not None and hasattr(client, "classify_director_address"):
            data = client.classify_director_address(en)
        elif client is not None and hasattr(client, "chat_json"):
            data = client.chat_json(
                CLASSIFY_ADDRESS_SYSTEM,
                classify_address_user_prompt(en),
                temperature=0.0,
            )
        elif client is None:
            from src.llm.openai_client import LLMClient

            data = LLMClient().classify_director_address(en)
        if isinstance(data, dict) and data:
            out = coerce_address_result(data, en, cn)
            if (
                out.get("director_address_street")
                or out.get("director_address_region")
                or out.get("director_address_flat")
                or out.get("address_country")
                or out.get("address_is_hk") == "1"
            ):
                return out
        if data is not None:
            logger.warning("住址英文 LLM 分类结果无效: %s", data)
    except Exception as exc:
        logger.warning("住址英文 LLM 分类失败: %s", exc)
    fb = weak_fallback_address(en)
    if chinese_address_is_hk(cn):
        fb["address_is_hk"] = "1"
        fb["address_country"] = fb.get("address_country") or "HKG"
        if not fb.get("director_address_region"):
            fb["director_address_region"] = resolve_s03_hk_district(
                "", address_en=en, address_cn=cn
            )
    return fb


def stored_s03_address_fields(
    director: dict[str, Any] | None = None,
    applicant: dict[str, Any] | None = None,
) -> dict[str, str]:
    """s03 只读已入库字段，不拆分、不猜区。"""
    d = director if isinstance(director, dict) else {}
    a = applicant if isinstance(applicant, dict) else {}

    def _get(*keys: str) -> str:
        for src in (d, a):
            for key in keys:
                val = str(src.get(key) or "").strip()
                if val:
                    return val
        return ""

    hk_raw = _get("address_is_hk")
    country = _get("address_country")
    region = _get("address_region", "director_address_region")
    if hk_raw:
        is_hk = hk_raw.lower() in ("1", "true", "yes", "hk")
    elif country.upper() in ("HKG", "HK"):
        is_hk = True
    elif region in set(s03_district_labels()) or region.startswith(
        ("香港島-", "九龍-", "新界-")
    ):
        is_hk = True
    else:
        is_hk = False
    return {
        "flat": _get("address_flat", "director_address_flat"),
        "building": _get("address_building", "director_address_building"),
        "street": _get("address_street", "director_address_street"),
        "region": region,
        "country": country,
        "address_is_hk": "1" if is_hk else "0",
    }


def s03_address_fields_for_fill(
    director: dict[str, Any] | None = None,
    applicant: dict[str, Any] | None = None,
) -> dict[str, str]:
    """s03 填表：优先已入库四段；仅缺街道/国家时用英文住址规则拆分（不调 LLM）。"""
    stored = stored_s03_address_fields(director, applicant)
    d = director if isinstance(director, dict) else {}
    a = applicant if isinstance(applicant, dict) else {}
    addr_en = str(d.get("address_en") or a.get("address_en") or "").strip()
    addr_cn = str(d.get("address_cn") or a.get("address_cn") or "").strip()
    need_street = not stored["street"]
    need_country = stored["address_is_hk"] != "1" and not stored["country"]
    if not need_street and not need_country:
        return stored
    if not addr_en and not addr_cn:
        return stored
    fb = weak_fallback_address(addr_en)
    if chinese_address_is_hk(addr_cn):
        fb["address_is_hk"] = "1"
        fb["address_country"] = fb.get("address_country") or "HKG"
        if not fb.get("director_address_region"):
            fb["director_address_region"] = resolve_s03_hk_district(
                "", address_en=addr_en, address_cn=addr_cn
            )
    return {
        "flat": stored["flat"] or str(fb.get("director_address_flat") or ""),
        "building": stored["building"] or str(fb.get("director_address_building") or ""),
        "street": stored["street"] or str(fb.get("director_address_street") or ""),
        "region": stored["region"] or str(fb.get("director_address_region") or ""),
        "country": stored["country"] or str(fb.get("address_country") or ""),
        "address_is_hk": (
            stored["address_is_hk"]
            if stored["address_is_hk"] == "1" or stored["country"]
            else str(fb.get("address_is_hk") or "0")
        ),
    }
