"""住址英文：香港判定、街道/区省市拆分、住址国家 CHN 含港澳台。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from src.materials.address_classify import (
    classify_director_address,
    coerce_address_result,
    english_address_is_hk,
    hk_district_select_candidates,
    pick_hk_district_from_options,
    split_english_street_region,
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
HK_EN = "Flat A, 9/F, Tai Yip Street, Kwun Tong, Kowloon, Hong Kong"
NT_EN = "RM D, 11/F, BLK 5, LOCWOOD COURT, 1 TIN WU ROAD, TIN SHUI WAI NT"
TW_EN = "No. 1 Zhongxiao E Rd, Taipei City, Taiwan"


class TestNormalizeAddressCountry(unittest.TestCase):
    def test_greater_china_to_chn(self):
        self.assertEqual(normalize_address_country("China"), "CHN")
        self.assertEqual(normalize_address_country("Hong Kong"), "CHN")
        self.assertEqual(normalize_address_country("HKG"), "CHN")
        self.assertEqual(normalize_address_country("Macao"), "CHN")
        self.assertEqual(normalize_address_country("台湾"), "CHN")
        self.assertEqual(normalize_address_country("TWN"), "CHN")

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

    def test_hk_keywords(self):
        self.assertTrue(english_address_is_hk(HK_EN))
        self.assertTrue(english_address_is_hk(NT_EN))
        self.assertTrue(english_address_is_hk("TIN SHUI WAI NT"))
        self.assertTrue(english_address_is_hk("1 Foo Road, N.T."))
        self.assertFalse(english_address_is_hk(SZ_EN))
        self.assertFalse(english_address_is_hk(UZB_EN))
        hk = weak_fallback_address(HK_EN)
        self.assertEqual(hk["address_is_hk"], "1")
        self.assertEqual(hk["address_country"], "CHN")
        nt = weak_fallback_address(NT_EN)
        self.assertEqual(nt["address_is_hk"], "1")
        self.assertEqual(nt["address_country"], "CHN")

    def test_taiwan_and_mainland_not_hk(self):
        tw = weak_fallback_address(TW_EN)
        self.assertEqual(tw["address_is_hk"], "0")
        self.assertEqual(tw["address_country"], "CHN")
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
        self.assertEqual(out["address_country"], "CHN")

    def test_chinese_nt_residence_is_hk_without_english(self):
        out = classify_director_address("", "新界天水圍天湖路", llm=object())
        self.assertEqual(out["address_is_hk"], "1")
        self.assertEqual(out["address_country"], "CHN")


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
        self.assertEqual(result.get("address_country"), "CHN")
        self.assertIn("TIN SHUI WAI", result.get("director_address_en") or "")

    def test_director_nt_office_mainland_still_hk(self):
        class Fake:
            def parse_quick_register_text(self, text: str) -> dict:
                return {
                    "director_address_en": NT_EN,
                    "registered_office_cn": "广东省深圳市南山区西丽南路8号",
                }

        result = parse_quick_register_text("x", llm=Fake())
        self.assertEqual(result.get("address_is_hk"), "1")
        self.assertEqual(result.get("address_country"), "CHN")

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
        self.assertEqual(result.get("address_country"), "CHN")


class TestHkDistrictPick(unittest.TestCase):
    def test_tin_shui_wai_candidates_yuen_long(self):
        cands = hk_district_select_candidates(NT_EN)
        self.assertIn("元朗", cands)
        self.assertIn("天水圍", cands)
        self.assertNotIn("香港仔", cands)

    def test_keyword_beats_llm_aberdeen(self):
        llm = MagicMock()
        llm.pick_hk_district.return_value = {"district": "香港仔"}
        opts = ["香港仔", "元朗", "天水圍", "屯門"]
        picked = pick_hk_district_from_options(NT_EN, opts, llm=llm)
        self.assertIn(picked, ("天水圍", "元朗"))
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


if __name__ == "__main__":
    unittest.main()
