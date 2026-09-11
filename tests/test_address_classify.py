"""住址英文：香港判定、街道/区省市拆分、住址国家内地/港/澳/台分开。"""

from __future__ import annotations

import inspect
import re
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.materials.address_classify import (
    ICRIS_ADDR_FIELD_MAX,
    apply_non_hk_fit_to_result,
    classify_director_address,
    clip_icris_addr,
    coerce_address_result,
    english_address_is_hk,
    fit_non_hk_address_fields,
    hk_district_select_candidates,
    load_s03_district_options,
    pick_hk_district_from_options,
    prepare_icris_fill_address,
    resolve_s03_hk_district,
    s03_district_labels,
    split_english_street_region,
    split_hk_english_four_way,
    stored_s03_address_fields,
    s03_address_fields_for_fill,
    weak_fallback_address,
    _english_address_body_parts,
)
from src.materials.countries import normalize_address_country
from src.materials.quick_register_parse import parse_quick_register_text


UZB_EN = (
    "39-uy, Zevarsoy ko'chasi, Xamkorobod MFY, "
    "Yunusabad district, Tashkent city, Uzbekistan"
)
SZ_EN = "Kejiyuan South Road, Nanshan District, Shenzhen City, Guangdong Province, China"
SZ_ROOM_EN = (
    "Room 110, No. 8, Xili South Road, Nanshan District, "
    "Shenzhen City, Guangdong Province"
)
SHANTOU_EN = (
    "No. 5, Xizhi Lane, Qiaoqian 2nd Area, Jinzao Town, "
    "Chaoyang District, Shantou City, Guangdong Province"
)
XJ_CORPS_EN = (
    "House 8, 2nd Company, 14th Regiment, "
    "Alar City, Xinjiang Uyghur Autonomous Region, China"
)
XJ_PREFECTURE_EN = (
    "No. 12 Tuanjie Road, Yining City, "
    "Ili Kazakh Autonomous Prefecture, Xinjiang Uyghur Autonomous Region, China"
)
NM_EN = (
    "Gacha 5, East Ujimqin Banner, Xilingol League, "
    "Inner Mongolia Autonomous Region, China"
)
SUBDISTRICT_EN = (
    "No. 8 Foo Road, Bar Sub-district, "
    "Chaoyang District, Beijing City, China"
)
PUDONG_EN = "No. 1 Century Avenue, Pudong New Area, Shanghai City, China"
US_EN = "123 Main Street, Springfield, IL 62704, United States"
JP_EN = "Apt 5, 1-2-3 Jingumae, Shibuya-ku, Tokyo, Japan"
AE_EN = "Villa 12, Al Barsha Community, Dubai Emirate, United Arab Emirates"
HK_EN = "Flat A, 9/F, Tai Yip Street, Kwun Tong, Kowloon, Hong Kong"
NT_EN = "RM D, 11/F, BLK 5, LOCWOOD COURT, 1 TIN WU ROAD, TIN SHUI WAI NT"
HK_GF_EN = (
    "Shop 3, G/F, Hang Seng Building, "
    "83 Des Voeux Road Central, Central, Hong Kong"
)
HK_PHASE_EN = "Flat B, 12/F, Wing A, Phase 2, Foo Court, 1 Bar Road, Tsuen Wan"
TW_EN = "No. 1 Zhongxiao E Rd, Taipei City, Taiwan"
HUANGSHI_EN = (
    "Room 38, No. 9, Xiejiafan Community, Yangjiashan Community, "
    "Laoxialu Street, Xialu District, Huangshi City, Hubei Province"
)
PLAZA_EN = (
    "Room 1, 18/F, Sunshine International Commercial Plaza Tower A, "
    "No. 88 Zhongshan Road East, Chaoyang District, Beijing City"
)
BOTH_OVER_EN = (
    "Room 38, No. 9, Xiejiafan Community, Yangjiashan Community, "
    "Laoxialu Street, Yining City, Ili Kazakh Autonomous Prefecture, "
    "Xinjiang Uyghur Autonomous Region"
)
STREET_EXACT_60 = "A" * ICRIS_ADDR_FIELD_MAX
STREET_61_ROOM = "Room 9, " + ("X" * 53)
HK_MIRA_EN = (
    "Shop 12, 2/F, Mira Place, 132 Nathan Road, "
    "Tsim Sha Tsui, Kowloon, Hong Kong"
)
HK_LAGUNA_EN = (
    "Unit 8, 15/F, Block C, Laguna City, "
    "8 Laguna Street, Kwun Tong, Kowloon"
)
HK_QUEENS_EN = "G/F, 28 Queen's Road Central, Central, Hong Kong"
HK_CHAI_WAN_ESTATE_EN = (
    "FLT 2505, 25/F, MAN FU HOUSE, HING MAN ESTATE, CHAI WAN, HK"
)
HK_HSE_EST_EN = "RM 1, 3/F, Foo HSE, Bar EST, Chai Wan, H.K."
HK_BLDG_KLN_EN = "Flat A, 8/F, Foo BLDG, 88 Bar Rd, Mong Kok, KLN"
HK_FLT_ESTATE_NT_EN = "FLT 12, 6/F, Sun HSE, Foo Estate, Sha Tin, N.T."
HK_TSUEN_EN = "No. 12, Foo Tsuen, Yuen Long, NT"
HK_HUNG_HOM_EN = "RM 5, 10/F, Bar Court, 1 Foo Road, Hung Hom, Kowloon"
HK_WANCHAI_LGF_EN = (
    "Shop 1, LG/F, Foo Centre, 9 Bar Street, Wan Chai, Hong Kong"
)
HK_TKO_TOWER_EN = "Flat C, 20/F, Tower 2, Foo Plaza, Tseung Kwan O, NT"

# mock LLM 目标四段：纠偏必须原样保留（屋苑进街道，HK/KLN/NT 不进街道）
HK_LLM_KEEP_CASES = [
    (
        HK_CHAI_WAN_ESTATE_EN,
        "FLT 2505, 25/F",
        "MAN FU HOUSE",
        "HING MAN ESTATE",
        "柴灣",
    ),
    (
        HK_HSE_EST_EN,
        "RM 1, 3/F",
        "Foo HSE",
        "Bar EST",
        "柴灣",
    ),
    (
        HK_BLDG_KLN_EN,
        "Flat A, 8/F",
        "Foo BLDG",
        "88 Bar Rd",
        "旺角",
    ),
    (
        HK_FLT_ESTATE_NT_EN,
        "FLT 12, 6/F",
        "Sun HSE",
        "Foo Estate",
        "沙田",
    ),
    (
        NT_EN,
        "RM D, 11/F, BLK 5",
        "LOCWOOD COURT",
        "1 TIN WU ROAD",
        "天水圍",
    ),
    (
        HK_EN,
        "Flat A, 9/F",
        "",
        "Tai Yip Street",
        "觀塘",
    ),
    (
        HK_GF_EN,
        "Shop 3, G/F",
        "Hang Seng Building",
        "83 Des Voeux Road Central",
        "中環",
    ),
    (
        HK_PHASE_EN,
        "Flat B, 12/F, Wing A, Phase 2",
        "Foo Court",
        "1 Bar Road",
        "荃灣",
    ),
    (
        HK_MIRA_EN,
        "Shop 12, 2/F",
        "Mira Place",
        "132 Nathan Road",
        "尖沙咀",
    ),
    (
        HK_LAGUNA_EN,
        "Unit 8, 15/F, Block C",
        "Laguna City",
        "8 Laguna Street",
        "觀塘",
    ),
    (
        HK_QUEENS_EN,
        "G/F",
        "",
        "28 Queen's Road Central",
        "中環",
    ),
    (
        HK_TSUEN_EN,
        "No. 12",
        "",
        "Foo Tsuen",
        "元朗",
    ),
    (
        HK_HUNG_HOM_EN,
        "RM 5, 10/F",
        "Bar Court",
        "1 Foo Road",
        "紅磡",
    ),
    (
        HK_WANCHAI_LGF_EN,
        "Shop 1, LG/F",
        "Foo Centre",
        "9 Bar Street",
        "灣仔",
    ),
    (
        HK_TKO_TOWER_EN,
        "Flat C, 20/F, Tower 2",
        "Foo Plaza",
        "",
        "將軍澳",
    ),
]
GZ_TEEM_EN = (
    "Room 1208, 28/F, Teemtower, 208 Tianhe Road, "
    "Tianhe District, Guangzhou City, Guangdong Province"
)
CD_EN = (
    "No. 16, Building 3, Section 4, Renmin South Road, "
    "Wuhou District, Chengdu City, Sichuan Province"
)
MO_EN = "Rua de Pequim, Edificio Centro Internacional, Macau"
SG_EN = (
    "12 Marina Boulevard, Marina Bay Financial Centre Tower 3, "
    "Singapore 018982"
)
UK_EN = "Flat 4, 10 Downing Court, Baker Street, London, NW1 6XE, United Kingdom"
KR_EN = "Apt 1503, 88 Teheran-ro, Gangnam-gu, Seoul, Republic of Korea"
VN_EN = (
    "House 21, Alley 15, Nguyen Trai Street, "
    "Thanh Xuan District, Hanoi City, Vietnam"
)
NON_HK_2_SH_EN = "88 Huaihai Middle Road, Huangpu District, Shanghai City"
NON_HK_2_TW_EN = "88 Sec 2 Zhongshan N Rd, Taipei City, Taiwan"
NON_HK_2_US_EN = "350 Fifth Avenue, New York, NY 10118"
NON_HK_2_AU_EN = (
    "10 George Street, Sydney, New South Wales 2000, Australia"
)
NON_HK_3_BJ_FLAT_EN = (
    "Room 2101, No. 88, East Jinsong Third Community, "
    "Panjiayuan Sub-district, Chaoyang District, Beijing City"
)
NON_HK_3_BJ_BLDG_EN = (
    "Sunshine International Commercial Plaza, "
    "No. 88 Zhongshan Road East, Chaoyang District, Beijing City"
)
NON_HK_3_VN_EN = (
    "House 21, Alley 15, Nguyen Trai Street, "
    "Thanh Xuan Ward Community, Thanh Xuan District, Hanoi City, Vietnam"
)
NON_HK_4_SH_EN = (
    "Unit 3601, 36/F, Shanghai World Financial Center Tower, "
    "100 Century Avenue West, Pudong New Area, Shanghai City"
)
NON_HK_4_SZ_EN = (
    "Room 88, 12/F, Ping An International Finance Centre Tower, "
    "5033 Yitian Road East, Futian District, Shenzhen City, "
    "Guangdong Province"
)
NON_HK_4_UK_EN = (
    "Flat 12, 3rd Floor, Westminster International Business Centre, "
    "221B Baker Street West, City of Westminster, London, "
    "United Kingdom"
)


class TestNormalizeAddressCountry(unittest.TestCase):
    def test_greater_china_regions_separate(self):
        self.assertEqual(normalize_address_country("China"), "CHN")
        self.assertEqual(normalize_address_country("Hong Kong"), "HKG")
        self.assertEqual(normalize_address_country("HKG"), "HKG")
        self.assertEqual(normalize_address_country("Macao"), "MAC")
        self.assertEqual(normalize_address_country("台湾"), "TWN")
        self.assertEqual(normalize_address_country("TWN"), "TWN")

    def test_foreign_keeps_iso(self):
        self.assertEqual(normalize_address_country("Uzbekistan"), "UZB")
        self.assertEqual(normalize_address_country("UZB"), "UZB")


class TestEnglishSplitAndHk(unittest.TestCase):
    def test_uzbekistan_split(self):
        street, region = split_english_street_region(UZB_EN)
        self.assertIn("Zevarsoy", street)
        self.assertIn("MFY", street)
        self.assertIn("Yunusabad", region)
        self.assertIn("Tashkent", region)
        self.assertNotIn("Uzbekistan", region)
        fb = weak_fallback_address(UZB_EN)
        self.assertEqual(fb["address_is_hk"], "0")
        self.assertEqual(fb["address_country"], "UZB")
        self.assertIn("Zevarsoy", fb["director_address_street"])

    def test_shenzhen_room_region_includes_city_province(self):
        street, region = split_english_street_region(SZ_ROOM_EN)
        self.assertEqual(street, "Room 110, No. 8, Xili South Road")
        self.assertEqual(
            region,
            "Nanshan District, Shenzhen City, Guangdong Province",
        )
        fb = weak_fallback_address(SZ_ROOM_EN)
        self.assertEqual(fb["director_address_street"], street)
        self.assertEqual(fb["director_address_region"], region)
        self.assertEqual(fb["address_is_hk"], "0")
        self.assertEqual(fb["address_country"], "CHN")
        self.assertEqual(fb["director_address_flat"], "")
        self.assertEqual(fb["director_address_building"], "")

    def test_llm_district_only_completed_to_city_province(self):
        llm = MagicMock()
        llm.classify_director_address.return_value = {
            "is_hk": False,
            "street": "Room 110, No. 8, Xili South Road",
            "region": "Nanshan District",
            "address_country": "CHN",
        }
        out = classify_director_address(SZ_ROOM_EN, llm=llm)
        self.assertEqual(out["director_address_street"], "Room 110, No. 8, Xili South Road")
        self.assertEqual(
            out["director_address_region"],
            "Nanshan District, Shenzhen City, Guangdong Province",
        )
        self.assertEqual(out["director_address_flat"], "")
        self.assertEqual(out["director_address_building"], "")

    def test_hk_keywords(self):
        self.assertTrue(english_address_is_hk(HK_EN))
        self.assertTrue(english_address_is_hk(NT_EN))
        self.assertTrue(english_address_is_hk("TIN SHUI WAI NT"))
        self.assertTrue(english_address_is_hk("1 Foo Road, N.T."))
        self.assertTrue(english_address_is_hk(HK_CHAI_WAN_ESTATE_EN))
        self.assertTrue(english_address_is_hk("Chai Wan, H.K."))
        self.assertTrue(english_address_is_hk("Mong Kok, KLN"))
        self.assertFalse(english_address_is_hk(SZ_EN))
        self.assertFalse(english_address_is_hk(UZB_EN))
        hk = weak_fallback_address(HK_EN)
        self.assertEqual(hk["address_is_hk"], "1")
        self.assertEqual(hk["address_country"], "HKG")
        nt = weak_fallback_address(NT_EN)
        self.assertEqual(nt["address_is_hk"], "1")
        self.assertEqual(nt["address_country"], "HKG")
        self.assertEqual(nt["director_address_flat"], "RM D, 11/F, BLK 5")
        self.assertEqual(nt["director_address_building"], "LOCWOOD COURT")
        self.assertEqual(nt["director_address_street"], "1 TIN WU ROAD")
        self.assertEqual(nt["director_address_region"], "天水圍")
        hk = weak_fallback_address(HK_EN)
        self.assertEqual(hk["director_address_flat"], "Flat A, 9/F")
        self.assertEqual(hk["director_address_building"], "")
        self.assertEqual(hk["director_address_street"], "Tai Yip Street")
        self.assertEqual(hk["director_address_region"], "觀塘")

    def test_taiwan_and_mainland_not_hk(self):
        tw = weak_fallback_address(TW_EN)
        self.assertEqual(tw["address_is_hk"], "0")
        self.assertEqual(tw["address_country"], "TWN")
        sz = weak_fallback_address(SZ_EN)
        self.assertEqual(sz["address_is_hk"], "0")
        self.assertEqual(sz["address_country"], "CHN")

    def test_no_english_no_split(self):
        empty = classify_director_address("", llm=object())
        self.assertEqual(empty["director_address_street"], "")
        self.assertEqual(empty["address_is_hk"], "0")

    def test_llm_hk_overridden_without_keywords(self):
        llm = MagicMock()
        llm.classify_director_address.return_value = {
            "is_hk": True,
            "street": "Kejiyuan South Road",
            "region": "Nanshan District, Shenzhen",
            "address_country": "HKG",
        }
        out = classify_director_address(SZ_EN, llm=llm)
        self.assertEqual(out["address_is_hk"], "0")
        self.assertEqual(out["address_country"], "CHN")
        fed = llm.classify_director_address.call_args[0][0]
        self.assertNotIn("注册", fed)
        self.assertIn("Shenzhen", fed)

    def test_coerce_uses_english_only(self):
        out = coerce_address_result(
            {"is_hk": True, "address_country": "HKG"},
            SZ_EN,
        )
        self.assertEqual(out["address_is_hk"], "0")
        self.assertEqual(out["address_country"], "CHN")

    def test_nt_keyword_overrides_llm_non_hk(self):
        out = coerce_address_result(
            {"is_hk": False, "address_country": "CHN"},
            NT_EN,
        )
        self.assertEqual(out["address_is_hk"], "1")
        self.assertEqual(out["address_country"], "HKG")
        self.assertEqual(out["director_address_flat"], "RM D, 11/F, BLK 5")
        self.assertEqual(out["director_address_building"], "LOCWOOD COURT")
        self.assertEqual(out["director_address_street"], "1 TIN WU ROAD")
        self.assertEqual(out["director_address_region"], "天水圍")

    def test_chinese_nt_residence_is_hk_without_english(self):
        out = classify_director_address("", "新界天水圍天湖路", llm=object())
        self.assertEqual(out["address_is_hk"], "1")
        self.assertEqual(out["address_country"], "HKG")
        self.assertEqual(out["director_address_region"], "天水圍")


class TestHkFourWay(unittest.TestCase):
    def test_locwood_flat_includes_floor_and_block(self):
        flat, building, street, region = split_hk_english_four_way(NT_EN)
        self.assertEqual(flat, "RM D, 11/F, BLK 5")
        self.assertEqual(building, "LOCWOOD COURT")
        self.assertEqual(street, "1 TIN WU ROAD")
        self.assertEqual(region, "天水圍")

    def test_llm_partial_flat_kept_when_other_fields_complete(self):
        llm = MagicMock()
        llm.classify_director_address.return_value = {
            "is_hk": True,
            "flat": "RM D",
            "building": "LOCWOOD COURT",
            "street": "1 TIN WU ROAD",
            "region": "天水圍",
            "address_country": "HKG",
        }
        out = classify_director_address(NT_EN, llm=llm)
        self.assertEqual(out["director_address_flat"], "RM D")
        self.assertEqual(out["director_address_building"], "LOCWOOD COURT")
        self.assertEqual(out["director_address_street"], "1 TIN WU ROAD")
        self.assertEqual(out["director_address_region"], "天水圍")
        self.assertEqual(out["address_is_hk"], "1")

    def test_llm_keep_four_way_table(self):
        tails = ("HK", "H.K.", "HKG", "KLN", "NT", "N.T.", "Hong Kong", "Kowloon")
        for en, flat, building, street, region in HK_LLM_KEEP_CASES:
            with self.subTest(keep=en[:48]):
                llm = MagicMock()
                llm.classify_director_address.return_value = {
                    "is_hk": True,
                    "flat": flat,
                    "building": building,
                    "street": street,
                    "region": region,
                    "address_country": "HKG",
                }
                out = classify_director_address(en, llm=llm)
                self.assertEqual(out["director_address_flat"], flat)
                self.assertEqual(out["director_address_building"], building)
                self.assertEqual(out["director_address_street"], street)
                self.assertEqual(out["director_address_region"], region)
                self.assertEqual(out["address_is_hk"], "1")
                self.assertEqual(out["address_country"], "HKG")
                got_street = out["director_address_street"]
                for tail in tails:
                    if tail.casefold() not in street.casefold():
                        self.assertNotIn(
                            tail,
                            got_street,
                            f"{tail!r} leaked into street for {en!r}",
                        )
            with self.subTest(fallback=en[:48]):
                fb = weak_fallback_address(en)
                self.assertEqual(fb["address_is_hk"], "1")
                self.assertEqual(fb["director_address_region"], region)
                self.assertEqual(fb["address_country"], "HKG")

    def test_chai_wan_estate_rule_fallback_four_way(self):
        flat, building, street, region = split_hk_english_four_way(
            HK_CHAI_WAN_ESTATE_EN
        )
        self.assertEqual(flat, "FLT 2505, 25/F")
        self.assertEqual(building, "MAN FU HOUSE")
        self.assertEqual(street, "HING MAN ESTATE")
        self.assertEqual(region, "柴灣")
        self.assertNotIn("HK", street)

    def test_gf_shop_in_flat(self):
        flat, building, street, region = split_hk_english_four_way(HK_GF_EN)
        self.assertIn("Shop 3", flat)
        self.assertIn("G/F", flat)
        self.assertEqual(building, "Hang Seng Building")
        self.assertIn("Des Voeux Road", street)
        self.assertEqual(region, "中環")
        fb = weak_fallback_address(HK_GF_EN)
        self.assertEqual(fb["director_address_flat"], flat)
        self.assertEqual(fb["director_address_region"], "中環")

    def test_phase_wing_in_flat(self):
        flat, building, street, region = split_hk_english_four_way(HK_PHASE_EN)
        self.assertIn("Flat B", flat)
        self.assertIn("12/F", flat)
        self.assertIn("Wing A", flat)
        self.assertIn("Phase 2", flat)
        self.assertEqual(building, "Foo Court")
        self.assertEqual(street, "1 Bar Road")
        self.assertEqual(region, "荃灣")


class TestChinaAndWorldStreetRegion(unittest.TestCase):
    def test_shantou_town_area_stay_in_street(self):
        street, region = split_english_street_region(SHANTOU_EN)
        self.assertIn("Xizhi Lane", street)
        self.assertIn("Qiaoqian", street)
        self.assertIn("Jinzao Town", street)
        self.assertTrue(region.startswith("Chaoyang District"))
        self.assertIn("Shantou City", region)
        self.assertIn("Guangdong Province", region)
        self.assertNotIn("Jinzao", region)
        fb = weak_fallback_address(SHANTOU_EN)
        self.assertEqual(fb["address_is_hk"], "0")
        self.assertEqual(fb["address_country"], "CHN")
        self.assertEqual(fb["director_address_street"], street)
        self.assertEqual(fb["director_address_region"], region)

    def test_llm_drops_shantou_town_coerced_back(self):
        llm = MagicMock()
        llm.classify_director_address.return_value = {
            "is_hk": False,
            "street": "No. 5, Xizhi Lane",
            "region": "Chaoyang District, Shantou City, Guangdong Province",
            "address_country": "CHN",
        }
        out = classify_director_address(SHANTOU_EN, llm=llm)
        self.assertIn("Qiaoqian", out["director_address_street"])
        self.assertIn("Jinzao Town", out["director_address_street"])
        self.assertTrue(out["director_address_region"].startswith("Chaoyang District"))
        self.assertEqual(out["director_address_flat"], "")
        self.assertEqual(out["director_address_building"], "")

    def test_xinjiang_corps_in_street(self):
        street, region = split_english_street_region(XJ_CORPS_EN)
        self.assertIn("2nd Company", street)
        self.assertIn("14th Regiment", street)
        self.assertIn("Alar City", region)
        self.assertIn("Xinjiang", region)
        self.assertNotIn("China", region)
        fb = weak_fallback_address(XJ_CORPS_EN)
        self.assertEqual(fb["address_country"], "CHN")
        self.assertIn("Regiment", fb["director_address_street"])

    def test_xinjiang_autonomous_prefecture_in_region(self):
        street, region = split_english_street_region(XJ_PREFECTURE_EN)
        self.assertIn("Tuanjie Road", street)
        self.assertIn("Yining City", region)
        self.assertIn("Prefecture", region)
        self.assertIn("Xinjiang", region)
        self.assertNotIn("China", region)

    def test_inner_mongolia_banner_league_in_region(self):
        street, region = split_english_street_region(NM_EN)
        self.assertIn("Gacha", street)
        self.assertIn("Banner", region)
        self.assertIn("League", region)
        self.assertIn("Inner Mongolia", region)
        self.assertNotIn("China", region)

    def test_subdistrict_stays_in_street(self):
        street, region = split_english_street_region(SUBDISTRICT_EN)
        self.assertIn("Foo Road", street)
        self.assertIn("Sub-district", street)
        self.assertTrue(region.startswith("Chaoyang District"))
        self.assertIn("Beijing City", region)

    def test_pudong_new_area_starts_region(self):
        street, region = split_english_street_region(PUDONG_EN)
        self.assertEqual(street, "No. 1 Century Avenue")
        self.assertTrue(region.startswith("Pudong New Area"))
        self.assertIn("Shanghai City", region)

    def test_us_city_state_zip(self):
        street, region = split_english_street_region(US_EN)
        self.assertEqual(street, "123 Main Street")
        self.assertIn("Springfield", region)
        self.assertIn("IL 62704", region)
        self.assertNotIn("United States", region)
        fb = weak_fallback_address(US_EN)
        self.assertEqual(fb["address_country"], "USA")
        self.assertEqual(fb["address_is_hk"], "0")
        self.assertEqual(fb["director_address_street"], street)

    def test_japan_ku_tokyo(self):
        street, region = split_english_street_region(JP_EN)
        self.assertIn("Jingumae", street)
        self.assertIn("Shibuya-ku", region)
        self.assertIn("Tokyo", region)
        self.assertNotIn("Japan", region)
        fb = weak_fallback_address(JP_EN)
        self.assertEqual(fb["address_country"], "JPN")

    def test_uae_community_street_emirate_region(self):
        street, region = split_english_street_region(AE_EN)
        self.assertIn("Al Barsha Community", street)
        self.assertIn("Dubai Emirate", region)
        self.assertNotIn("United Arab Emirates", region)
        fb = weak_fallback_address(AE_EN)
        self.assertEqual(fb["address_country"], "ARE")
        self.assertEqual(fb["address_is_hk"], "0")


class TestNonHkFitSixty(unittest.TestCase):
    def _occupied_count(self, out: dict) -> int:
        return sum(
            1
            for key in (
                "director_address_flat",
                "director_address_building",
                "director_address_street",
                "director_address_region",
            )
            if str(out.get(key) or "").strip()
        )

    def _assert_four_le60(self, flat, building, street, region, msg=""):
        for name, val in (
            ("flat", flat),
            ("building", building),
            ("street", street),
            ("region", region),
        ):
            self.assertLessEqual(
                len(val),
                ICRIS_ADDR_FIELD_MAX,
                f"{msg} {name}={val!r} len={len(val)}",
            )

    def _assert_parts_kept(self, address_en, flat, building, street, region):
        blob = re.sub(r"\s+", " ", f"{flat}, {building}, {street}, {region}".lower())
        for part in _english_address_body_parts(address_en):
            token = re.sub(r"\s+", " ", part.strip().lower())
            self.assertIn(token, blob, f"丢失逗号段 {part!r}")

    def _parse_fit(self, en, *, llm=None):
        street, region = split_english_street_region(en)
        if llm is None:
            llm = MagicMock()
            llm.classify_director_address.return_value = {
                "is_hk": False,
                "street": street,
                "region": region,
                "address_country": "CHN",
            }
        out = classify_director_address(en, llm=llm)
        return apply_non_hk_fit_to_result(out, llm=None)

    def test_short_addresses_keep_empty_flat_building(self):
        cases = [
            ("sz_room", SZ_ROOM_EN),
            ("shantou", SHANTOU_EN),
            ("pudong", PUDONG_EN),
            ("subdistrict", SUBDISTRICT_EN),
            ("us", US_EN),
            ("jp", JP_EN),
            ("ae", AE_EN),
            ("uzb", UZB_EN),
            ("tw", TW_EN),
        ]
        for name, en in cases:
            with self.subTest(name=name, llm="none"):
                fb = weak_fallback_address(en)
                self.assertEqual(fb["address_is_hk"], "0")
                self.assertEqual(fb["director_address_flat"], "")
                self.assertEqual(fb["director_address_building"], "")
                self._assert_parts_kept(
                    en,
                    fb["director_address_flat"],
                    fb["director_address_building"],
                    fb["director_address_street"],
                    fb["director_address_region"],
                )
            with self.subTest(name=name, parse="fit"):
                out = self._parse_fit(en)
                self.assertEqual(out["director_address_flat"], "")
                self.assertEqual(out["director_address_building"], "")
                self._assert_four_le60(
                    out["director_address_flat"],
                    out["director_address_building"],
                    out["director_address_street"],
                    out["director_address_region"],
                    name,
                )

    def test_classify_and_weak_fallback_do_not_peel_huangshi(self):
        street, region = split_english_street_region(HUANGSHI_EN)
        fb = weak_fallback_address(HUANGSHI_EN)
        self.assertEqual(fb["director_address_flat"], "")
        self.assertEqual(fb["director_address_street"], street)
        llm = MagicMock()
        llm.classify_director_address.return_value = {
            "is_hk": False,
            "street": street,
            "region": region,
            "address_country": "CHN",
        }
        out = classify_director_address(HUANGSHI_EN, llm=llm)
        self.assertEqual(out["director_address_flat"], "")
        self.assertEqual(out["director_address_street"], street)

    def test_huangshi_peels_on_parse_fit(self):
        out = self._parse_fit(HUANGSHI_EN)
        self.assertEqual(out["director_address_flat"], "Room 38, No. 9")
        self.assertEqual(out["director_address_building"], "")
        self.assertIn("Xiejiafan Community", out["director_address_street"])
        self.assertIn("Yangjiashan Community", out["director_address_street"])
        self.assertIn("Laoxialu Street", out["director_address_street"])
        self.assertEqual(
            out["director_address_region"],
            "Xialu District, Huangshi City, Hubei Province",
        )
        self._assert_four_le60(
            out["director_address_flat"],
            out["director_address_building"],
            out["director_address_street"],
            out["director_address_region"],
        )
        self._assert_parts_kept(
            HUANGSHI_EN,
            out["director_address_flat"],
            out["director_address_building"],
            out["director_address_street"],
            out["director_address_region"],
        )

    def test_huangshi_llm_community_in_building_rejected(self):
        street, region = split_english_street_region(HUANGSHI_EN)

        class BadLLM:
            def fit_non_hk_address_fields(self, *args, **kwargs):
                return {
                    "flat": "Room 38, No. 9",
                    "building": "Xiejiafan Community, Yangjiashan Community",
                    "street": "Laoxialu Street",
                    "region": region,
                }

        flat, building, street_out, region_out = fit_non_hk_address_fields(
            "", "", street, region, llm=BadLLM()
        )
        self.assertEqual(flat, "Room 38, No. 9")
        self.assertEqual(building, "")
        self.assertIn("Xiejiafan Community", street_out)
        self.assertIn("Yangjiashan Community", street_out)
        self.assertIn("Laoxialu Street", street_out)
        self.assertEqual(region_out, region)

    def test_plaza_tower_peels_on_parse_fit(self):
        out = self._parse_fit(PLAZA_EN)
        self.assertIn("Room 1", out["director_address_flat"])
        self.assertIn("18/F", out["director_address_flat"])
        self.assertIn("Plaza", out["director_address_building"])
        self.assertIn("Tower", out["director_address_building"])
        self.assertIn("Zhongshan Road", out["director_address_street"])
        self._assert_four_le60(
            out["director_address_flat"],
            out["director_address_building"],
            out["director_address_street"],
            out["director_address_region"],
        )
        self._assert_parts_kept(
            PLAZA_EN,
            out["director_address_flat"],
            out["director_address_building"],
            out["director_address_street"],
            out["director_address_region"],
        )

    def test_street_exact_60_not_peeled(self):
        flat, building, street, region = fit_non_hk_address_fields(
            "", "", STREET_EXACT_60, "Nanshan District", llm=None
        )
        self.assertEqual(street, STREET_EXACT_60)
        self.assertEqual(flat, "")
        self.assertEqual(building, "")
        self.assertEqual(len(street), 60)

    def test_street_61_peels_room(self):
        self.assertEqual(len(STREET_61_ROOM), 61)
        flat, building, street, region = fit_non_hk_address_fields(
            "", "", STREET_61_ROOM, "Nanshan District", llm=None
        )
        self.assertEqual(flat, "Room 9")
        self.assertEqual(len(street), 53)
        self._assert_four_le60(flat, building, street, region)

    def test_long_region_yili_inner_mongolia_corps_parse_fit(self):
        cases = [
            (XJ_PREFECTURE_EN, "Xinjiang"),
            (NM_EN, "Inner Mongolia"),
            (XJ_CORPS_EN, "Xinjiang"),
        ]
        for en, keep in cases:
            with self.subTest(en=en[:40]):
                out = self._parse_fit(en)
                self.assertIn(keep, out["director_address_region"])
                if "Regiment" in en:
                    self.assertIn("Regiment", out["director_address_street"])
                self._assert_four_le60(
                    out["director_address_flat"],
                    out["director_address_building"],
                    out["director_address_street"],
                    out["director_address_region"],
                )
                self._assert_parts_kept(
                    en,
                    out["director_address_flat"],
                    out["director_address_building"],
                    out["director_address_street"],
                    out["director_address_region"],
                )

    def test_street_and_region_both_over_60_parse_fit(self):
        out = self._parse_fit(BOTH_OVER_EN)
        self._assert_four_le60(
            out["director_address_flat"],
            out["director_address_building"],
            out["director_address_street"],
            out["director_address_region"],
        )
        self._assert_parts_kept(
            BOTH_OVER_EN,
            out["director_address_flat"],
            out["director_address_building"],
            out["director_address_street"],
            out["director_address_region"],
        )
        self.assertIn("Room 38", out["director_address_flat"])
        self.assertIn("Xinjiang", out["director_address_region"])

    def test_ten_extra_addresses(self):
        hk_cases = [
            (HK_MIRA_EN, "Shop 12, 2/F", "Mira Place", "Nathan Road", "尖沙咀"),
            (HK_LAGUNA_EN, "Unit 8, 15/F, Block C", "Laguna City", "Laguna Street", "觀塘"),
            (HK_QUEENS_EN, "G/F", "", "Queen's Road Central", "中環"),
        ]
        for en, flat_part, bldg_part, street_part, region in hk_cases:
            with self.subTest(en=en[:40]):
                fb = weak_fallback_address(en)
                rule = split_hk_english_four_way(en)
                self.assertEqual(fb["address_is_hk"], "1")
                self.assertEqual(fb["director_address_flat"], rule[0])
                self.assertEqual(fb["director_address_building"], rule[1])
                self.assertEqual(fb["director_address_street"], rule[2])
                self.assertEqual(fb["director_address_region"], rule[3])
                self.assertIn(flat_part, fb["director_address_flat"])
                combined_bldg_street = (
                    f"{fb['director_address_building']}, "
                    f"{fb['director_address_street']}"
                )
                if bldg_part:
                    self.assertIn(bldg_part, combined_bldg_street)
                self.assertIn(street_part, fb["director_address_street"])
                self.assertEqual(fb["director_address_region"], region)
                with patch(
                    "src.materials.address_classify.fit_non_hk_address_fields"
                ) as fit:
                    llm = MagicMock()
                    llm.classify_director_address.return_value = {
                        "is_hk": True,
                        "flat": rule[0],
                        "building": rule[1],
                        "street": rule[2],
                        "region": region,
                        "address_country": "HKG",
                    }
                    classify_director_address(en, llm=llm)
                    apply_non_hk_fit_to_result(
                        {
                            "director_address_flat": rule[0],
                            "director_address_building": rule[1],
                            "director_address_street": rule[2],
                            "director_address_region": region,
                            "address_is_hk": "1",
                        }
                    )
                    fit.assert_not_called()

        non_hk = [
            GZ_TEEM_EN,
            CD_EN,
            MO_EN,
            SG_EN,
            UK_EN,
            KR_EN,
            VN_EN,
        ]
        for en in non_hk:
            with self.subTest(en=en[:40]):
                fb = weak_fallback_address(en)
                self.assertEqual(fb["address_is_hk"], "0")
                out = self._parse_fit(en)
                self.assertEqual(out["address_is_hk"], "0")
                self._assert_four_le60(
                    out["director_address_flat"],
                    out["director_address_building"],
                    out["director_address_street"],
                    out["director_address_region"],
                    en[:20],
                )
                self._assert_parts_kept(
                    en,
                    out["director_address_flat"],
                    out["director_address_building"],
                    out["director_address_street"],
                    out["director_address_region"],
                )
        mo = weak_fallback_address(MO_EN)
        self.assertEqual(mo["address_country"], "MAC")

    def test_ten_non_hk_segment_mix(self):
        cases = [
            (2, NON_HK_2_SH_EN, "CHN"),
            (2, NON_HK_2_TW_EN, "TWN"),
            (2, NON_HK_2_US_EN, ""),
            (2, NON_HK_2_AU_EN, "AUS"),
            (3, NON_HK_3_BJ_FLAT_EN, "CHN"),
            (3, NON_HK_3_BJ_BLDG_EN, "CHN"),
            (3, NON_HK_3_VN_EN, "VNM"),
            (4, NON_HK_4_SH_EN, "CHN"),
            (4, NON_HK_4_SZ_EN, "CHN"),
            (4, NON_HK_4_UK_EN, "GBR"),
        ]
        self.assertEqual(len(cases), 10)
        for occupied, en, country in cases:
            with self.subTest(en=en[:50], occupied=occupied):
                fb = weak_fallback_address(en)
                self.assertEqual(fb["address_is_hk"], "0")
                self.assertNotEqual(fb.get("address_country"), "HKG")
                if country:
                    self.assertEqual(fb["address_country"], country)
                rule_street, _rule_region = split_english_street_region(en)
                if occupied >= 3:
                    self.assertEqual(fb["director_address_flat"], "")
                    self.assertEqual(fb["director_address_street"], rule_street)
                out = self._parse_fit(en)
                self.assertEqual(out["address_is_hk"], "0")
                self._assert_four_le60(
                    out["director_address_flat"],
                    out["director_address_building"],
                    out["director_address_street"],
                    out["director_address_region"],
                    en[:20],
                )
                self._assert_parts_kept(
                    en,
                    out["director_address_flat"],
                    out["director_address_building"],
                    out["director_address_street"],
                    out["director_address_region"],
                )
                self.assertEqual(self._occupied_count(out), occupied)
                if occupied == 2:
                    self.assertEqual(out["director_address_flat"], "")
                    self.assertEqual(out["director_address_building"], "")
                elif occupied == 4:
                    self.assertTrue(out["director_address_flat"])
                    self.assertTrue(out["director_address_building"])
                    self.assertTrue(out["director_address_street"])
                    self.assertTrue(out["director_address_region"])

    def test_hk_does_not_call_non_hk_fit(self):
        llm = MagicMock()
        llm.classify_director_address.return_value = {
            "is_hk": True,
            "flat": "RM D, 11/F, BLK 5",
            "building": "LOCWOOD COURT",
            "street": "1 TIN WU ROAD",
            "region": "天水圍",
            "address_country": "HKG",
        }
        with patch(
            "src.materials.address_classify.fit_non_hk_address_fields"
        ) as fit:
            out = classify_director_address(NT_EN, llm=llm)
            fit.assert_not_called()
        self.assertEqual(out["director_address_flat"], "RM D, 11/F, BLK 5")
        self.assertEqual(out["director_address_building"], "LOCWOOD COURT")
        gf = weak_fallback_address(HK_GF_EN)
        self.assertEqual(gf["address_is_hk"], "1")
        self.assertIn("Hang Seng", gf["director_address_building"])
        hk = weak_fallback_address(HK_EN)
        self.assertEqual(hk["director_address_flat"], "Flat A, 9/F")

    def test_mock_llm_overflow_still_clamped(self):
        class OverflowLLM:
            def fit_non_hk_address_fields(self, *args, **kwargs):
                return {
                    "flat": "Z" * 70,
                    "building": "Y" * 70,
                    "street": "X" * 70,
                    "region": "W" * 70,
                }

        street, region = split_english_street_region(HUANGSHI_EN)
        flat, building, street, region = fit_non_hk_address_fields(
            "", "", street, region, llm=OverflowLLM()
        )
        self._assert_four_le60(flat, building, street, region)
        self._assert_parts_kept(HUANGSHI_EN, flat, building, street, region)
        self.assertEqual(flat, "Room 38, No. 9")

    def test_prepare_fill_clips_only_non_hk(self):
        addr = prepare_icris_fill_address(
            {
                "flat": "",
                "building": "",
                "street": STREET_61_ROOM,
                "region": "Nanshan District",
                "address_is_hk": "0",
            }
        )
        self.assertEqual(addr["flat"], "")
        self.assertEqual(addr["street"], STREET_61_ROOM[:60])
        self.assertEqual(clip_icris_addr("A" * 80), "A" * 60)
        hk = prepare_icris_fill_address(
            {
                "flat": "RM D, 11/F, BLK 5",
                "building": "LOCWOOD COURT",
                "street": "1 TIN WU ROAD",
                "region": "天水圍",
                "address_is_hk": "1",
            }
        )
        self.assertEqual(hk["flat"], "RM D, 11/F, BLK 5")
        self.assertEqual(hk["region"], "天水圍")

    def test_s03_fill_does_not_peel_stored_street(self):
        street, region = split_english_street_region(HUANGSHI_EN)
        parts = s03_address_fields_for_fill(
            {
                "address_street": street,
                "address_region": region,
                "address_country": "CHN",
                "address_is_hk": "0",
            }
        )
        self.assertEqual(parts["flat"], "")
        self.assertEqual(parts["street"], street)

    def test_s03_nnc1_fill_non_hk_flat_and_clip(self):
        from src.browser.icris_nnc1_form import IcrisNnc1FormBot
        from src.browser.icris_registration import IcrisRegistrationBot

        s03_src = inspect.getsource(IcrisRegistrationBot._fill_user_info_step)
        self.assertNotIn("prepare_icris_fill_address", s03_src)
        self.assertIn("if is_hk:", s03_src)
        self.assertIn("clip_icris_addr", s03_src)
        nnc1_src = inspect.getsource(IcrisNnc1FormBot._fill_nnc1_address_section)
        self.assertNotIn("prepare_icris_fill_address", nnc1_src)
        non_hk = nnc1_src.split("await self._wait_country_region_options")[1]
        self.assertIn("室.*樓", non_hk)
        self.assertIn("大廈|大厦|Building", non_hk)
        order_src = inspect.getsource(
            IcrisNnc1FormBot._fill_address_fields_by_order
        )
        self.assertIn('clip_icris_addr(addr.get("flat"', order_src)
        self.assertIn("(0, clip_icris_addr", order_src)

    def test_admin_parse_page_not_submit_fit(self):
        runner = Path("src/web/admin_runner.py").read_text(encoding="utf-8")
        self.assertNotIn("fit_non_hk_address_fields", runner)
        page = Path("web/admin/src/pages/RegisterPage.tsx").read_text(
            encoding="utf-8"
        )
        self.assertGreaterEqual(page.count("室／楼／座"), 2)
        self.assertIn("director_address_building", page)

    def test_parse_paste_huangshi_fits(self):
        class Fake:
            def parse_quick_register_text(self, text: str) -> dict:
                return {"director_address_en": HUANGSHI_EN}

        result = parse_quick_register_text("x", llm=Fake())
        self.assertEqual(result.get("director_address_flat"), "Room 38, No. 9")
        self.assertLessEqual(
            len(result.get("director_address_street") or ""),
            ICRIS_ADDR_FIELD_MAX,
        )


class TestParseAttachesAddress(unittest.TestCase):
    def test_parse_uzbekistan_and_keeps_issuing_country(self):
        class Fake:
            def parse_quick_register_text(self, text: str) -> dict:
                return {
                    "director_address_en": UZB_EN,
                    "id_type": "PASSPORT",
                    "id_number": "FA0266712",
                    "issuing_country": "UZB",
                    "registered_office_cn": "香港新界葵涌",
                }

        result = parse_quick_register_text("任意", llm=Fake())
        self.assertEqual(result.get("address_is_hk"), "0")
        self.assertEqual(result.get("address_country"), "UZB")
        self.assertEqual(result.get("issuing_country"), "UZB")
        self.assertIn("Zevarsoy", result.get("director_address_street") or "")
        self.assertIn("Yunusabad", result.get("director_address_region") or "")
        self.assertEqual(result.get("director_address_flat") or "", "")
        self.assertIn("香港新界", result.get("registered_office_cn") or "")

    def test_registered_office_hong_kong_does_not_make_sz_address_hk(self):
        class Fake:
            def parse_quick_register_text(self, text: str) -> dict:
                return {
                    "director_address_en": SZ_EN,
                    "registered_office_en": "Kwai Chung, New Territories, Hong Kong",
                }

        result = parse_quick_register_text("x", llm=Fake())
        self.assertEqual(result.get("address_is_hk"), "0")
        self.assertEqual(result.get("address_country"), "CHN")

    def test_address_label_nt_is_hk_country_chn(self):
        class Fake:
            def parse_quick_register_text(self, text: str) -> dict:
                return {"director_address_en": NT_EN}

        result = parse_quick_register_text(
            "地址：RM D, 11/F, BLK 5, LOCWOOD COURT, 1 TIN WU ROAD, TIN SHUI WAI NT",
            llm=Fake(),
        )
        self.assertEqual(result.get("address_is_hk"), "1")
        self.assertEqual(result.get("address_country"), "HKG")
        self.assertIn("TIN SHUI WAI", result.get("director_address_en") or "")
        self.assertEqual(result.get("director_address_flat"), "RM D, 11/F, BLK 5")
        self.assertEqual(result.get("director_address_building"), "LOCWOOD COURT")
        self.assertEqual(result.get("director_address_street"), "1 TIN WU ROAD")
        self.assertEqual(result.get("director_address_region"), "天水圍")

    def test_director_nt_office_mainland_still_hk(self):
        class Fake:
            def parse_quick_register_text(self, text: str) -> dict:
                return {
                    "director_address_en": NT_EN,
                    "registered_office_cn": "广东省深圳市南山区西丽南路8号",
                }

        result = parse_quick_register_text("x", llm=Fake())
        self.assertEqual(result.get("address_is_hk"), "1")
        self.assertEqual(result.get("address_country"), "HKG")

    def test_regex_address_label_nt_then_classify(self):
        class Boom:
            def parse_quick_register_text(self, text: str) -> dict:
                raise RuntimeError("no llm")

        text = (
            "地址英文：RM D, 11/F, BLK 5, LOCWOOD COURT, "
            "1 TIN WU ROAD, TIN SHUI WAI NT"
        )
        result = parse_quick_register_text(text, llm=Boom())
        self.assertEqual(result.get("source"), "regex")
        self.assertIn("TIN SHUI WAI NT", result.get("director_address_en") or "")
        self.assertEqual(result.get("address_is_hk"), "1")
        self.assertEqual(result.get("address_country"), "HKG")
        self.assertEqual(result.get("director_address_region"), "天水圍")


class TestHkDistrictPick(unittest.TestCase):
    def test_district_json_has_dropdown_labels(self):
        labels = s03_district_labels()
        self.assertGreaterEqual(len(labels), 80)
        self.assertIn("香港仔", labels)
        self.assertIn("紅磡", labels)
        self.assertIn("天水圍", labels)
        rows = load_s03_district_options()
        self.assertTrue(any("Tin Shui Wai" in (r.get("aliases") or []) for r in rows))

    def test_s03_districts_api_lists_dropdown_labels(self):
        from unittest.mock import MagicMock

        from src.web.admin_api import handle_admin_api

        resp, code = handle_admin_api(
            method="GET",
            path="/admin/api/s03-districts",
            store=MagicMock(),
        )
        self.assertEqual(code, 200)
        self.assertTrue(resp.get("ok"))
        self.assertGreaterEqual(int(resp.get("count") or 0), 80)
        labels = [row["label"] for row in resp.get("items") or []]
        self.assertIn("天水圍", labels)
        self.assertIn("香港仔", labels)

    def test_tin_shui_wai_candidates_yuen_long(self):
        cands = hk_district_select_candidates(NT_EN)
        self.assertIn("元朗", cands)
        self.assertIn("天水圍", cands)
        self.assertNotIn("香港仔", cands)
        self.assertEqual(resolve_s03_hk_district("", address_en=NT_EN), "天水圍")

    def test_keyword_beats_llm_aberdeen(self):
        llm = MagicMock()
        llm.pick_hk_district.return_value = {"district": "香港仔"}
        opts = s03_district_labels()
        picked = pick_hk_district_from_options(NT_EN, opts, llm=llm)
        self.assertEqual(picked, "天水圍")
        self.assertNotEqual(picked, "香港仔")
        llm.pick_hk_district.assert_not_called()

    def test_llm_picks_yuen_long_from_options(self):
        llm = MagicMock()
        llm.pick_hk_district.return_value = {"district": "元朗"}
        opts = ["香港仔", "元朗", "灣仔"]
        picked = pick_hk_district_from_options(
            "1 SOME UNKNOWN STREET NT", opts, llm=llm
        )
        self.assertEqual(picked, "元朗")

    def test_llm_aberdeen_rejected_without_keyword(self):
        llm = MagicMock()
        llm.pick_hk_district.return_value = {"district": "香港仔"}
        opts = ["香港仔", "元朗"]
        picked = pick_hk_district_from_options("1 UNKNOWN STREET NT", opts, llm=llm)
        self.assertEqual(picked, "")


class TestStoredS03Address(unittest.TestCase):
    def test_does_not_split_raw_english(self):
        parts = stored_s03_address_fields(
            {"address_en": NT_EN, "address_is_hk": "1"},
            {},
        )
        self.assertEqual(parts["street"], "")
        self.assertEqual(parts["region"], "")
        self.assertEqual(parts["flat"], "")
        self.assertEqual(parts["address_is_hk"], "1")

    def test_reads_persisted_four_way(self):
        parts = stored_s03_address_fields(
            {
                "address_flat": "RM D, 11/F, BLK 5",
                "address_building": "LOCWOOD COURT",
                "address_street": "1 TIN WU ROAD",
                "address_region": "天水圍",
                "address_country": "HKG",
                "address_is_hk": "1",
            }
        )
        self.assertEqual(parts["flat"], "RM D, 11/F, BLK 5")
        self.assertEqual(parts["building"], "LOCWOOD COURT")
        self.assertEqual(parts["street"], "1 TIN WU ROAD")
        self.assertEqual(parts["region"], "天水圍")

    def test_s03_fill_does_not_split_or_llm_pick(self):
        import inspect

        from src.browser.icris_registration import IcrisRegistrationBot

        src = inspect.getsource(IcrisRegistrationBot._fill_user_info_step)
        self.assertNotIn("split_address_street_region(", src)
        self.assertNotIn("pick_hk_district_from_options(", src)
        self.assertIn("s03_address_fields_for_fill", src)

    def test_s03_fill_fallback_from_english_address(self):
        parts = s03_address_fields_for_fill(
            {
                "address_en": SZ_ROOM_EN,
                "address_cn": "广东省深圳市南山区西丽南路8号110室",
            }
        )
        self.assertEqual(parts["street"], "Room 110, No. 8, Xili South Road")
        self.assertEqual(
            parts["region"],
            "Nanshan District, Shenzhen City, Guangdong Province",
        )
        self.assertEqual(parts["country"], "CHN")
        self.assertEqual(parts["address_is_hk"], "0")

    def test_s03_fill_keeps_persisted_fields(self):
        parts = s03_address_fields_for_fill(
            {
                "address_en": SZ_ROOM_EN,
                "address_street": "1 TIN WU ROAD",
                "address_region": "天水圍",
                "address_country": "HKG",
                "address_is_hk": "1",
            }
        )
        self.assertEqual(parts["street"], "1 TIN WU ROAD")
        self.assertEqual(parts["region"], "天水圍")
        self.assertEqual(parts["country"], "HKG")
        self.assertEqual(parts["address_is_hk"], "1")

    def test_aggregator_persists_four_way(self):
        from src.materials.aggregator import aggregate_company_data

        data = aggregate_company_data(
            {
                "company_name_en": {"field_value": "Foo Ltd"},
                "director_name": {"field_value": "CHAN TAI MAN"},
                "director_address_en": {"field_value": NT_EN},
                "director_address_flat": {"field_value": "RM D, 11/F, BLK 5"},
                "director_address_building": {"field_value": "LOCWOOD COURT"},
                "director_address_street": {"field_value": "1 TIN WU ROAD"},
                "director_address_region": {"field_value": "天水圍"},
                "address_country": {"field_value": "HKG"},
                "address_is_hk": {"field_value": "1"},
            }
        )
        director = (data.get("directors") or [{}])[0]
        applicant = data.get("applicant") or {}
        self.assertEqual(director.get("address_flat"), "RM D, 11/F, BLK 5")
        self.assertEqual(director.get("address_building"), "LOCWOOD COURT")
        self.assertEqual(director.get("address_street"), "1 TIN WU ROAD")
        self.assertEqual(director.get("address_region"), "天水圍")
        self.assertEqual(applicant.get("address_region"), "天水圍")
        self.assertEqual(applicant.get("address_is_hk"), "1")


class TestNnc1AddressStored(unittest.TestCase):
    def test_resolve_matches_s03_stored(self):
        from src.browser.icris_nnc1_form import resolve_nnc1_person_address

        person = {
            "address_flat": "RM D, 11/F, BLK 5",
            "address_building": "LOCWOOD COURT",
            "address_street": "1 TIN WU ROAD",
            "address_region": "天水圍",
            "address_country": "HKG",
            "address_is_hk": "1",
        }
        got = resolve_nnc1_person_address(person, {"applicant": {}})
        self.assertEqual(got, stored_s03_address_fields(person, {}))
        self.assertEqual(got["address_is_hk"], "1")

    def test_does_not_split_raw_english(self):
        from src.browser.icris_nnc1_form import resolve_nnc1_person_address

        got = resolve_nnc1_person_address(
            {"address_en": NT_EN, "address_is_hk": "1"},
            {},
        )
        self.assertEqual(got["street"], "")
        self.assertEqual(got["flat"], "")
        self.assertEqual(got["address_is_hk"], "1")

    def test_hk_radio_excludes_non_hk_label(self):
        from src.browser.icris_nnc1_form import (
            nnc1_address_radio_is_hk,
            nnc1_address_radio_is_non_hk,
        )

        self.assertTrue(nnc1_address_radio_is_hk("香港地址"))
        self.assertTrue(nnc1_address_radio_is_hk("本港地址"))
        self.assertFalse(nnc1_address_radio_is_hk("非香港地址"))
        self.assertFalse(nnc1_address_radio_is_hk("非本地地址"))
        self.assertTrue(nnc1_address_radio_is_non_hk("非香港地址"))
        self.assertFalse(nnc1_address_radio_is_non_hk("香港地址"))

    def test_hk_district_label_excludes_region_field(self):
        from src.browser.icris_nnc1_form import nnc1_hk_district_label

        self.assertTrue(nnc1_hk_district_label("区"))
        self.assertTrue(nnc1_hk_district_label("區"))
        self.assertTrue(nnc1_hk_district_label("區 *"))
        self.assertTrue(nnc1_hk_district_label("District"))
        self.assertTrue(nnc1_hk_district_label("区\n請選擇"))
        self.assertFalse(nnc1_hk_district_label("地区"))
        self.assertFalse(nnc1_hk_district_label("地區"))
        self.assertFalse(nnc1_hk_district_label("国家／地区"))
        self.assertFalse(nnc1_hk_district_label("區／市／省／州／郵遞區號"))

    def test_district_select_keys_include_value_alias(self):
        from src.browser.icris_nnc1_form import nnc1_district_select_keys

        keys = nnc1_district_select_keys("天水圍")
        self.assertIn("天水圍", keys)
        self.assertIn("天水围", keys)
        self.assertIn("TINSHUIWAI", keys)

    def test_nnc1_fill_uses_stored_not_split(self):
        import inspect

        from src.browser.icris_nnc1_form import IcrisNnc1FormBot, resolve_nnc1_person_address

        self.assertIn("stored_s03_address_fields", inspect.getsource(resolve_nnc1_person_address))
        self.assertNotIn(
            "_split_non_hk_address_en",
            inspect.getsource(IcrisNnc1FormBot._resolve_person_address),
        )
        src = inspect.getsource(IcrisNnc1FormBot._fill_nnc1_address_section)
        self.assertIn("_select_hk_in_block", src)
        self.assertIn("_select_non_hk_in_block", src)
        self.assertNotIn("_split_non_hk_address_en", src)
        hk_fill = src.split("filled = 0")[1].split(
            "await self._wait_country_region_options"
        )[0]
        self.assertIn("_select_district_in_block(page, block, region)", hk_fill)
        self.assertNotIn("郵遞區號", hk_fill)
        dist_src = inspect.getsource(IcrisNnc1FormBot._select_district_in_block)
        self.assertIn("data-nnc1-hk-district", dist_src)
        self.assertIn("nnc1_district_select_keys", dist_src)


if __name__ == "__main__":
    unittest.main()
