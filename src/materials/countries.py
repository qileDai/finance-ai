"""护照签发国：ISO 3166-1 alpha-3 + 中英名称。台湾护照映射为中国。"""

from __future__ import annotations

import json
import re
from pathlib import Path

# (iso3, en, zh-Hans) — 不含台湾作为独立签发国
PASSPORT_COUNTRIES: tuple[tuple[str, str, str], ...] = (
    ("CHN", "China", "中国"),
    ("HKG", "Hong Kong", "中国香港"),
    ("MAC", "Macao", "中国澳门"),
    ("AFG", "Afghanistan", "阿富汗"),
    ("ALB", "Albania", "阿尔巴尼亚"),
    ("DZA", "Algeria", "阿尔及利亚"),
    ("AND", "Andorra", "安道尔"),
    ("AGO", "Angola", "安哥拉"),
    ("ATG", "Antigua and Barbuda", "安提瓜和巴布达"),
    ("ARG", "Argentina", "阿根廷"),
    ("ARM", "Armenia", "亚美尼亚"),
    ("AUS", "Australia", "澳大利亚"),
    ("AUT", "Austria", "奥地利"),
    ("AZE", "Azerbaijan", "阿塞拜疆"),
    ("BHS", "Bahamas", "巴哈马"),
    ("BHR", "Bahrain", "巴林"),
    ("BGD", "Bangladesh", "孟加拉国"),
    ("BRB", "Barbados", "巴巴多斯"),
    ("BLR", "Belarus", "白俄罗斯"),
    ("BEL", "Belgium", "比利时"),
    ("BLZ", "Belize", "伯利兹"),
    ("BEN", "Benin", "贝宁"),
    ("BTN", "Bhutan", "不丹"),
    ("BOL", "Bolivia", "玻利维亚"),
    ("BIH", "Bosnia and Herzegovina", "波斯尼亚和黑塞哥维那"),
    ("BWA", "Botswana", "博茨瓦纳"),
    ("BRA", "Brazil", "巴西"),
    ("BRN", "Brunei", "文莱"),
    ("BGR", "Bulgaria", "保加利亚"),
    ("BFA", "Burkina Faso", "布基纳法索"),
    ("BDI", "Burundi", "布隆迪"),
    ("CPV", "Cabo Verde", "佛得角"),
    ("KHM", "Cambodia", "柬埔寨"),
    ("CMR", "Cameroon", "喀麦隆"),
    ("CAN", "Canada", "加拿大"),
    ("CAF", "Central African Republic", "中非"),
    ("TCD", "Chad", "乍得"),
    ("CHL", "Chile", "智利"),
    ("COL", "Colombia", "哥伦比亚"),
    ("COM", "Comoros", "科摩罗"),
    ("COG", "Congo", "刚果（布）"),
    ("COD", "Congo (DRC)", "刚果（金）"),
    ("CRI", "Costa Rica", "哥斯达黎加"),
    ("CIV", "Cote d'Ivoire", "科特迪瓦"),
    ("HRV", "Croatia", "克罗地亚"),
    ("CUB", "Cuba", "古巴"),
    ("CYP", "Cyprus", "塞浦路斯"),
    ("CZE", "Czechia", "捷克"),
    ("DNK", "Denmark", "丹麦"),
    ("DJI", "Djibouti", "吉布提"),
    ("DMA", "Dominica", "多米尼克"),
    ("DOM", "Dominican Republic", "多米尼加"),
    ("ECU", "Ecuador", "厄瓜多尔"),
    ("EGY", "Egypt", "埃及"),
    ("SLV", "El Salvador", "萨尔瓦多"),
    ("GNQ", "Equatorial Guinea", "赤道几内亚"),
    ("ERI", "Eritrea", "厄立特里亚"),
    ("EST", "Estonia", "爱沙尼亚"),
    ("SWZ", "Eswatini", "斯威士兰"),
    ("ETH", "Ethiopia", "埃塞俄比亚"),
    ("FJI", "Fiji", "斐济"),
    ("FIN", "Finland", "芬兰"),
    ("FRA", "France", "法国"),
    ("GAB", "Gabon", "加蓬"),
    ("GMB", "Gambia", "冈比亚"),
    ("GEO", "Georgia", "格鲁吉亚"),
    ("DEU", "Germany", "德国"),
    ("GHA", "Ghana", "加纳"),
    ("GRC", "Greece", "希腊"),
    ("GRD", "Grenada", "格林纳达"),
    ("GTM", "Guatemala", "危地马拉"),
    ("GIN", "Guinea", "几内亚"),
    ("GNB", "Guinea-Bissau", "几内亚比绍"),
    ("GUY", "Guyana", "圭亚那"),
    ("HTI", "Haiti", "海地"),
    ("HND", "Honduras", "洪都拉斯"),
    ("HUN", "Hungary", "匈牙利"),
    ("ISL", "Iceland", "冰岛"),
    ("IND", "India", "印度"),
    ("IDN", "Indonesia", "印度尼西亚"),
    ("IRN", "Iran", "伊朗"),
    ("IRQ", "Iraq", "伊拉克"),
    ("IRL", "Ireland", "爱尔兰"),
    ("ISR", "Israel", "以色列"),
    ("ITA", "Italy", "意大利"),
    ("JAM", "Jamaica", "牙买加"),
    ("JPN", "Japan", "日本"),
    ("JOR", "Jordan", "约旦"),
    ("KAZ", "Kazakhstan", "哈萨克斯坦"),
    ("KEN", "Kenya", "肯尼亚"),
    ("KIR", "Kiribati", "基里巴斯"),
    ("PRK", "North Korea", "朝鲜"),
    ("KOR", "South Korea", "韩国"),
    ("KWT", "Kuwait", "科威特"),
    ("KGZ", "Kyrgyzstan", "吉尔吉斯斯坦"),
    ("LAO", "Laos", "老挝"),
    ("LVA", "Latvia", "拉脱维亚"),
    ("LBN", "Lebanon", "黎巴嫩"),
    ("LSO", "Lesotho", "莱索托"),
    ("LBR", "Liberia", "利比里亚"),
    ("LBY", "Libya", "利比亚"),
    ("LIE", "Liechtenstein", "列支敦士登"),
    ("LTU", "Lithuania", "立陶宛"),
    ("LUX", "Luxembourg", "卢森堡"),
    ("MDG", "Madagascar", "马达加斯加"),
    ("MWI", "Malawi", "马拉维"),
    ("MYS", "Malaysia", "马来西亚"),
    ("MDV", "Maldives", "马尔代夫"),
    ("MLI", "Mali", "马里"),
    ("MLT", "Malta", "马耳他"),
    ("MHL", "Marshall Islands", "马绍尔群岛"),
    ("MRT", "Mauritania", "毛里塔尼亚"),
    ("MUS", "Mauritius", "毛里求斯"),
    ("MEX", "Mexico", "墨西哥"),
    ("FSM", "Micronesia", "密克罗尼西亚"),
    ("MDA", "Moldova", "摩尔多瓦"),
    ("MCO", "Monaco", "摩纳哥"),
    ("MNG", "Mongolia", "蒙古"),
    ("MNE", "Montenegro", "黑山"),
    ("MAR", "Morocco", "摩洛哥"),
    ("MOZ", "Mozambique", "莫桑比克"),
    ("MMR", "Myanmar", "缅甸"),
    ("NAM", "Namibia", "纳米比亚"),
    ("NRU", "Nauru", "瑙鲁"),
    ("NPL", "Nepal", "尼泊尔"),
    ("NLD", "Netherlands", "荷兰"),
    ("NZL", "New Zealand", "新西兰"),
    ("NIC", "Nicaragua", "尼加拉瓜"),
    ("NER", "Niger", "尼日尔"),
    ("NGA", "Nigeria", "尼日利亚"),
    ("MKD", "North Macedonia", "北马其顿"),
    ("NOR", "Norway", "挪威"),
    ("OMN", "Oman", "阿曼"),
    ("PAK", "Pakistan", "巴基斯坦"),
    ("PLW", "Palau", "帕劳"),
    ("PAN", "Panama", "巴拿马"),
    ("PNG", "Papua New Guinea", "巴布亚新几内亚"),
    ("PRY", "Paraguay", "巴拉圭"),
    ("PER", "Peru", "秘鲁"),
    ("PHL", "Philippines", "菲律宾"),
    ("POL", "Poland", "波兰"),
    ("PRT", "Portugal", "葡萄牙"),
    ("QAT", "Qatar", "卡塔尔"),
    ("ROU", "Romania", "罗马尼亚"),
    ("RUS", "Russia", "俄罗斯"),
    ("RWA", "Rwanda", "卢旺达"),
    ("KNA", "Saint Kitts and Nevis", "圣基茨和尼维斯"),
    ("LCA", "Saint Lucia", "圣卢西亚"),
    ("VCT", "Saint Vincent and the Grenadines", "圣文森特和格林纳丁斯"),
    ("WSM", "Samoa", "萨摩亚"),
    ("SMR", "San Marino", "圣马力诺"),
    ("STP", "Sao Tome and Principe", "圣多美和普林西比"),
    ("SAU", "Saudi Arabia", "沙特阿拉伯"),
    ("SEN", "Senegal", "塞内加尔"),
    ("SRB", "Serbia", "塞尔维亚"),
    ("SYC", "Seychelles", "塞舌尔"),
    ("SLE", "Sierra Leone", "塞拉利昂"),
    ("SGP", "Singapore", "新加坡"),
    ("SVK", "Slovakia", "斯洛伐克"),
    ("SVN", "Slovenia", "斯洛文尼亚"),
    ("SLB", "Solomon Islands", "所罗门群岛"),
    ("SOM", "Somalia", "索马里"),
    ("ZAF", "South Africa", "南非"),
    ("SSD", "South Sudan", "南苏丹"),
    ("ESP", "Spain", "西班牙"),
    ("LKA", "Sri Lanka", "斯里兰卡"),
    ("SDN", "Sudan", "苏丹"),
    ("SUR", "Suriname", "苏里南"),
    ("SWE", "Sweden", "瑞典"),
    ("CHE", "Switzerland", "瑞士"),
    ("SYR", "Syria", "叙利亚"),
    ("TJK", "Tajikistan", "塔吉克斯坦"),
    ("TZA", "Tanzania", "坦桑尼亚"),
    ("THA", "Thailand", "泰国"),
    ("TLS", "Timor-Leste", "东帝汶"),
    ("TGO", "Togo", "多哥"),
    ("TON", "Tonga", "汤加"),
    ("TTO", "Trinidad and Tobago", "特立尼达和多巴哥"),
    ("TUN", "Tunisia", "突尼斯"),
    ("TUR", "Turkiye", "土耳其"),
    ("TKM", "Turkmenistan", "土库曼斯坦"),
    ("TUV", "Tuvalu", "图瓦卢"),
    ("UGA", "Uganda", "乌干达"),
    ("UKR", "Ukraine", "乌克兰"),
    ("ARE", "United Arab Emirates", "阿联酋"),
    ("GBR", "United Kingdom", "英国"),
    ("USA", "United States", "美国"),
    ("URY", "Uruguay", "乌拉圭"),
    ("UZB", "Uzbekistan", "乌兹别克斯坦"),
    ("VUT", "Vanuatu", "瓦努阿图"),
    ("VAT", "Vatican City", "梵蒂冈"),
    ("VEN", "Venezuela", "委内瑞拉"),
    ("VNM", "Vietnam", "越南"),
    ("YEM", "Yemen", "也门"),
    ("ZMB", "Zambia", "赞比亚"),
    ("ZWE", "Zimbabwe", "津巴布韦"),
)

_BY_ISO = {c[0]: c for c in PASSPORT_COUNTRIES}
_BY_EN = {c[1].upper(): c[0] for c in PASSPORT_COUNTRIES}
_BY_CN = {c[2]: c[0] for c in PASSPORT_COUNTRIES}

_TAIWAN_TOKENS = frozenset(
    {
        "TWN",
        "TW",
        "TAIWAN",
        "ROC",
        "R.O.C.",
        "中華民國",
        "中华民国",
        "台灣",
        "台湾",
        "臺湾",
    }
)

_CHINA_TOKENS = frozenset(
    {
        "CHN",
        "CN",
        "CHINA",
        "PRC",
        "中国",
        "中國",
        "中华人民共和国",
        "中華人民共和國",
    }
)


def is_taiwan_issuing(raw: str) -> bool:
    s = (raw or "").strip()
    if not s:
        return False
    key = s.upper().replace(" ", "")
    if key in _TAIWAN_TOKENS or s in _TAIWAN_TOKENS:
        return True
    return "台湾" in s or "台灣" in s or "臺灣" in s or "中華民國" in s or "中华民国" in s


_HK_MO_TW_ISO = frozenset({"HKG", "MAC", "TWN"})
_MACAU_TOKENS = frozenset(
    {
        "MAC",
        "MACAO",
        "MACAU",
        "MO",
        "澳门",
        "澳門",
        "中国澳门",
        "中國澳門",
    }
)


def is_macao_address_country(raw: str) -> bool:
    s = (raw or "").strip()
    if not s:
        return False
    key = s.upper().replace(" ", "")
    if key in _MACAU_TOKENS or s in _MACAU_TOKENS:
        return True
    return "澳门" in s or "澳門" in s or "macao" in s.lower() or "macau" in s.lower()


def normalize_address_country(raw: str) -> str:
    """住址国家 → ISO3。内地/香港/澳门/台湾一律 CHN。"""
    s = (raw or "").strip()
    if not s:
        return ""
    if is_taiwan_issuing(s) or is_macao_address_country(s):
        return "CHN"
    key = s.upper().replace(" ", "")
    if key in _HK_MO_TW_ISO:
        return "CHN"
    if "HONGKONG" in key or "HONG KONG" in s.upper() or "香港" in s:
        # 单独「香港」当住址国 → CHN；整段英文住址另由 is_hk 判定
        if key in ("HK", "HKG", "HONGKONG") or s.strip() in (
            "香港",
            "Hong Kong",
            "hong kong",
        ):
            return "CHN"
    iso = normalize_issuing_iso(s)
    if iso in _HK_MO_TW_ISO:
        return "CHN"
    return iso


def normalize_issuing_iso(raw: str) -> str:
    """签发国 → ISO3。台湾映射 CHN。无法识别返回空。"""
    s = (raw or "").strip()
    if not s:
        return ""
    if is_taiwan_issuing(s):
        return "CHN"
    key = s.upper().replace(" ", "")
    if key in _CHINA_TOKENS or s in _CHINA_TOKENS:
        return "CHN"
    if key in _BY_ISO:
        return key
    if key in _BY_EN:
        return _BY_EN[key]
    if s in _BY_CN:
        return _BY_CN[s]
    for iso, en, cn in PASSPORT_COUNTRIES:
        if s.lower() == en.lower() or s == cn:
            return iso
    return ""


def country_names(iso3: str) -> tuple[str, str]:
    row = _BY_ISO.get((iso3 or "").strip().upper())
    if not row:
        return "", ""
    return row[2], row[1]


def _to_zh_hk(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    try:
        import zhconv

        return zhconv.convert(s, "zh-hk")
    except Exception:
        return s


def passport_country_option_names(iso3: str) -> list[str]:
    """护照/住址国家下拉候选：香港繁体优先，再简体/英文。台湾与空/CHN 均选中国。"""
    code = normalize_issuing_iso(iso3) if iso3 else ""
    names: list[str] = []
    if not code or code == "CHN":
        names = ["中國", "中国", "China", "中華人民共和國", "中华人民共和国"]
    else:
        cn, en = country_names(code)
        trad = _to_zh_hk(cn)
        for n in (trad, cn, en):
            if n and n not in names:
                names.append(n)
        if code == "HKG":
            for n in ("香港", "Hong Kong", "中國香港"):
                if n not in names:
                    names.append(n)
        if code == "UZB":
            names = ["烏茲別克斯坦"] + [n for n in names if n != "烏茲別克斯坦"]
            for n in ("乌兹别克斯坦", "乌兹别克", "Uzbekistan"):
                if n not in names:
                    names.append(n)
    return names


S03_COUNTRIES_PATH = Path(__file__).resolve().parents[2] / "data" / "icris_s03_countries.json"
_S03_OPTIONS: list[dict[str, str]] | None = None


def _read_s03_country_file(path: Path) -> list[dict[str, str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = raw.get("options") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    skip = re.compile(r"請選擇|请选择|^Select$|^--+", re.I)
    for item in rows:
        if isinstance(item, str):
            label, value = item.strip(), item.strip()
        elif isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            value = str(item.get("value") or label).strip() or label
        else:
            continue
        if not label or skip.search(label) or label in seen:
            continue
        seen.add(label)
        out.append({"label": label, "value": value})
    return out


def load_s03_country_options(
    *, path: Path | None = None, reload: bool = False
) -> list[dict[str, str]]:
    """ICRIS s03 非香港「國家／地區」下拉原文。"""
    global _S03_OPTIONS
    if path is not None:
        return _read_s03_country_file(path)
    if _S03_OPTIONS is not None and not reload:
        return _S03_OPTIONS
    _S03_OPTIONS = _read_s03_country_file(S03_COUNTRIES_PATH)
    return _S03_OPTIONS


def s03_country_labels(*, path: Path | None = None) -> list[str]:
    return [row["label"] for row in load_s03_country_options(path=path) if row.get("label")]


def resolve_s03_address_country(
    raw: str,
    *,
    options: list[dict[str, str]] | None = None,
) -> str:
    """住址国家 → ICRIS 下拉原文。已是原文则原样；ISO3/英文/简体映射到采标 label。"""
    s = (raw or "").strip()
    if not s:
        return ""
    rows = options if options is not None else load_s03_country_options()
    labels = [str(r.get("label") or "").strip() for r in rows if str(r.get("label") or "").strip()]
    label_set = set(labels)
    by_value = {
        str(r.get("value") or "").strip().upper(): str(r.get("label") or "").strip()
        for r in rows
        if str(r.get("label") or "").strip() and str(r.get("value") or "").strip()
    }
    if s in label_set:
        return s
    trad = _to_zh_hk(s)
    if trad in label_set:
        return trad
    iso = normalize_address_country(s) or (s.upper() if len(s) == 3 and s.isalpha() else "")
    if iso and iso.upper() in by_value:
        return by_value[iso.upper()]
    key = s.upper().replace(" ", "")
    if key in by_value:
        return by_value[key]
    names = list(passport_country_option_names(iso or s))
    if s not in names:
        names.insert(0, s)
    if trad and trad not in names:
        names.insert(0, trad)
    for cand in names:
        if cand in label_set:
            return cand
        t = _to_zh_hk(cand)
        if t in label_set:
            return t
    if not labels:
        return names[0] if names else s
    s_l = s.lower()
    for lab in labels:
        if s_l == lab.lower() or s in lab or lab in s:
            return lab
    if iso == "CHN" and "中國" in label_set:
        return "中國"
    return names[0] if names else s
