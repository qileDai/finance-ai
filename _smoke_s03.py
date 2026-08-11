"""S03 姓名/地址/電子查冊 纯逻辑冒烟测试（不连浏览器）

运行（项目根目录下）：
    .venv/Scripts/python.exe _smoke_s03.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(label: str, got, expected) -> None:
    ok = got == expected
    flag = "OK " if ok else "FAIL"
    print(f"[{flag}] {label}: got={got!r} expected={expected!r}")
    if not ok:
        failures.append(label)


# ---------- 1. 姓名 CJK/Latin 拆分（aggregator 本地副本，逻辑与 icris 同款） ----------
from src.materials.aggregator import _split_cjk_latin_name

check("split 姚曉佳", _split_cjk_latin_name("姚曉佳"), ("姚曉佳", ""))
check("split Yau Siu Ka", _split_cjk_latin_name("Yau Siu Ka"), ("", "Yau Siu Ka"))
check("split 混合", _split_cjk_latin_name("姚曉佳 Yau Siu Ka"), ("姚曉佳", "Yau Siu Ka"))
check("split 空", _split_cjk_latin_name(""), ("", ""))

# 同时验证 icris_registration 中的同款函数（若 playwright 可导入）
try:
    from src.browser.icris_registration import split_cjk_latin_name as icris_split

    check("icris split 姚曉佳", icris_split("姚曉佳"), ("姚曉佳", ""))
    check("icris split 混合", icris_split("姚曉佳 Yau Siu Ka"), ("姚曉佳", "Yau Siu Ka"))
    HAVE_ICRIS = True
except Exception as e:  # playwright 未安装等
    print(f"[SKIP] 无法导入 icris_registration（{type(e).__name__}: {e}）")
    HAVE_ICRIS = False

# ---------- 2. detect_hk_address ----------
if HAVE_ICRIS:
    from src.browser.icris_registration import detect_hk_address

    check(
        "hk 深圳(非HK)",
        detect_hk_address(
            "Room 110, No. 8, Xili South Road, Nanshan District, Shenzhen City, Guangdong Province",
            "广东省深圳市南山区西丽南路8号110室",
        ),
        False,
    )
    check(
        "hk 香港注册地址",
        detect_hk_address(
            "909O 9/F., High Fashion Centre, 1-11 Kwai Hei Street, Kwai Chung, New Territories, Hong Kong",
            "香港新界葵涌葵喜街1-11号达利国际中心9楼909O",
        ),
        True,
    )

# ---------- 3. split_address_street_region ----------
if HAVE_ICRIS:
    from src.browser.icris_registration import split_address_street_region

    en_addr = "Room 110, No. 8, Xili South Road, Nanshan District, Shenzhen City, Guangdong Province"
    st, reg = split_address_street_region(en_addr)
    check("en 街道", st, "Room 110, No. 8, Xili South Road, Nanshan District")
    check("en 区/市/省", reg, "Shenzhen City, Guangdong Province")

    cn_addr = "广东省深圳市南山区西丽南路8号110室"
    st2, reg2 = split_address_street_region(cn_addr)
    check("cn 区/市/省含省+市", "广东省深圳市" in reg2 and reg2 == "广东省深圳市", True)
    check("cn 街道含南山", "南山区西丽南路8号110室" in st2 and st2 == "南山区西丽南路8号110室", True)

# ---------- 4. aggregator applicant 中英拆分 ----------
from src.materials.aggregator import aggregate_company_data

materials = {
    "company_name_cn": {"field_value": "撼世全球有限公司"},
    "company_name_en": {"field_value": "Humsienk Global Limited"},
    "director_name": {"field_value": "姚曉佳"},
    "director_address_cn": {"field_value": "广东省深圳市南山区西丽南路8号110室"},
    "director_address_en": {
        "field_value": "Room 110, No. 8, Xili South Road, Nanshan District, Shenzhen City, Guangdong Province"
    },
    "id_type": {"field_value": "PRC_ID"},
    "id_number": {"field_value": "44051420000318492X"},
}
data = aggregate_company_data(materials)
applicant = data.get("applicant", {})
check("aggregator name_cn", applicant.get("name_cn"), "姚曉佳")
check("aggregator name_en(空)", applicant.get("name_en"), "")
# 董事住址应进入 directors/founder_members
directors = data.get("directors") or []
if directors:
    d0 = directors[0]
    check(
        "directors address_en",
        d0.get("address_en", "").startswith("Room 110"),
        True,
    )
    check("directors address_cn", d0.get("address_cn"), "广东省深圳市南山区西丽南路8号110室")

# 英文姓名场景
materials_en = dict(materials)
materials_en["director_name"] = {"field_value": "Yau Siu Ka"}
data2 = aggregate_company_data(materials_en)
check("aggregator en name_en", data2["applicant"]["name_en"], "Yau Siu Ka")
check("aggregator en name_cn(空)", data2["applicant"]["name_cn"], "")

# 混合姓名场景
materials_mix = dict(materials)
materials_mix["director_name"] = {"field_value": "姚曉佳 Yau Siu Ka"}
data3 = aggregate_company_data(materials_mix)
check("aggregator mix name_cn", data3["applicant"]["name_cn"], "姚曉佳")
check("aggregator mix name_en", data3["applicant"]["name_en"], "Yau Siu Ka")

# ---------- 5. settings 开关 ----------
from config.settings import settings

check("settings icris_skip_esearch_principal", getattr(settings, "icris_skip_esearch_principal", None), True)
check("settings materials_default_contact_email 非空", bool(getattr(settings, "materials_default_contact_email", "")), True)

# ---------- 6. 跳过逻辑（桩 page 验证 _fill_account_profile_native 不勾 search/principal） ----------
if HAVE_ICRIS:
    import asyncio
    from src.browser.icris_registration import IcrisRegistrationBot

    class _StubPage:
        async def wait_for_timeout(self, *_a, **_k):
            return None

    calls: dict[str, int] = {"search": 0, "principal": 0, "filing": 0, "individual": 0}

    class _Bot(IcrisRegistrationBot):
        async def _select_native_user_type_individual(self, page):
            calls["individual"] += 1
            return True

        async def _check_native_checkbox_by_value(self, page, value):
            calls[value] = calls.get(value, 0) + 1
            return True

        async def _select_principal_account_after_search(self, page):
            calls["principal"] += 1
            return True

        async def _fill_native_input(self, page, sel, val):
            return True

    bot = _Bot.__new__(_Bot)
    page = _StubPage()
    asyncio.run(bot._fill_account_profile_native(page, {"applicant": {}, "icris_account": {"username": "u", "password": "p"}}))
    check("skip: 不勾 search", calls.get("search", 0), 0)
    check("skip: 不选 principal", calls.get("principal", 0), 0)
    check("skip: 仍勾 filing", calls.get("filing", 0), 1)
    check("skip: 仍选 individual", calls.get("individual", 0), 1)

print()
if failures:
    print(f"==== {len(failures)} 项失败 ====")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("==== 全部通过 ====")
