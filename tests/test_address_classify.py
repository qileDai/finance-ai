"""住址英文：香港判定、街道/区省市拆分、住址国家内地/港/澳/台分开。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from src.materials.address_classify import (
    classify_director_address,
    coerce_address_result,
    english_address_is_hk,
    hk_district_select_candidates,
    load_s03_district_options,
    pick_hk_district_from_options,
    resolve_s03_hk_district,
    s03_district_labels,
    split_english_street_region,
    split_hk_english_four_way,
    stored_s03_address_fields,
    weak_fallback_address,
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

    def test_llm_only_room_coerced_to_floor_block(self):
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
        self.assertEqual(out["director_address_flat"], "RM D, 11/F, BLK 5")
        self.assertEqual(out["director_address_building"], "LOCWOOD COURT")
        self.assertEqual(out["director_address_street"], "1 TIN WU ROAD")
        self.assertEqual(out["director_address_region"], "天水圍")
        self.assertEqual(out["address_is_hk"], "1")

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
        self.assertIn("stored_s03_address_fields", src)

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
