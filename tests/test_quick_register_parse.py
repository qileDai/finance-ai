"""快速注册粘贴解析：简繁、住址国籍、台湾护照→CHN。"""

from __future__ import annotations

import unittest

from src.materials.countries import normalize_issuing_iso, passport_country_option_names
from src.materials.id_type_classify import nnc1_identity_fill_plan
from src.materials.quick_register_parse import (
    PARSE_QUICK_REGISTER_SYSTEM,
    coerce_parse_result,
    parse_quick_register_text,
    parse_registration_text_regex,
)


class TestParsePromptContract(unittest.TestCase):
    def test_prompt_covers_traditional_and_address_script(self):
        self.assertIn("註冊地址", PARSE_QUICK_REGISTER_SYSTEM)
        self.assertIn("住址(Uzbekistan)", PARSE_QUICK_REGISTER_SYSTEM)
        self.assertIn("director_address_en", PARSE_QUICK_REGISTER_SYSTEM)
        self.assertIn("issuing_country", PARSE_QUICK_REGISTER_SYSTEM)
        self.assertIn("CHN", PARSE_QUICK_REGISTER_SYSTEM)
        self.assertIn("不要根据注册地址或住址里的「香港」判断", PARSE_QUICK_REGISTER_SYSTEM)
        self.assertIn("必须保留括号内英文", PARSE_QUICK_REGISTER_SYSTEM)
        self.assertIn("办事处地址", PARSE_QUICK_REGISTER_SYSTEM)


class TestParseRegexFallback(unittest.TestCase):
    def test_traditional_registered_office(self):
        text = "註冊地址：香港新界葵涌葵喜街1-11号达利国际中心9楼909O"
        parsed = parse_registration_text_regex(text)
        self.assertIn("香港新界", parsed.get("registered_office_cn") or "")
        self.assertFalse(parsed.get("registered_office_en"))

    def test_uzbekistan_english_address_not_cn(self):
        text = (
            "住址(Uzbekistan) 39-uy, Zevarsoy ko'chasi, Xamkorobod MFY, "
            "Yunusabad district, Tashkent city, Uzbekistan"
        )
        parsed = parse_registration_text_regex(text)
        self.assertIn("Zevarsoy", parsed.get("director_address_en") or "")
        self.assertFalse(parsed.get("director_address_cn"))

    def test_taiwan_passport_maps_issuing_to_chn(self):
        text = "护照号码：FA0266712\n护照签发地：台湾"
        parsed = parse_registration_text_regex(text)
        self.assertTrue(parsed.get("taiwan_passport"))
        self.assertEqual(parsed.get("issuing_country"), "CHN")

    def test_hk_office_with_prc_id_number_is_prc_id(self):
        text = (
            "身份证号码：44051420000318492X\n"
            "注册地址：香港新界葵涌葵喜街1-11号达利国际中心9楼909O"
        )
        parsed = parse_registration_text_regex(text)
        self.assertEqual(parsed.get("id_type"), "PRC_ID")
        self.assertEqual(parsed.get("id_number"), "44051420000318492X")
        self.assertIn("香港新界", parsed.get("registered_office_cn") or "")

    def test_address_label_english_is_director_not_office(self):
        text = (
            "地址：RM D, 11/F, BLK 5, LOCWOOD COURT, 1 TIN WU ROAD, TIN SHUI WAI NT\n"
            "注册地址：香港新界葵涌葵喜街1-11号达利国际中心9楼909O"
        )
        parsed = parse_registration_text_regex(text)
        self.assertIn("TIN SHUI WAI NT", parsed.get("director_address_en") or "")
        self.assertFalse(parsed.get("director_address_cn"))
        self.assertIn("香港新界", parsed.get("registered_office_cn") or "")
        self.assertNotIn("TIN SHUI WAI", parsed.get("registered_office_en") or "")

    def test_address_en_label_extracts_director(self):
        text = (
            "地址英文：RM D, 11/F, BLK 5, LOCWOOD COURT, "
            "1 TIN WU ROAD, TIN SHUI WAI NT"
        )
        parsed = parse_registration_text_regex(text)
        self.assertIn("TIN SHUI WAI NT", parsed.get("director_address_en") or "")

    def test_office_next_line_not_stolen_when_address_labeled(self):
        text = (
            "注册地址：香港新界葵涌葵喜街1-11号达利国际中心9楼909O\n"
            "地址：RM D, 11/F, BLK 5, LOCWOOD COURT, 1 TIN WU ROAD, TIN SHUI WAI NT"
        )
        parsed = parse_registration_text_regex(text)
        self.assertIn("香港新界", parsed.get("registered_office_cn") or "")
        self.assertIn("TIN SHUI WAI NT", parsed.get("director_address_en") or "")
        self.assertNotEqual(
            parsed.get("registered_office_en"), parsed.get("director_address_en")
        )


class TestParseLlmPrimary(unittest.TestCase):
    def test_mock_llm_used_when_valid(self):
        class Fake:
            def parse_quick_register_text(self, text: str) -> dict:
                return {
                    "registered_office_cn": "香港中環",
                    "id_type": "PASSPORT",
                    "id_number": "E12345678",
                    "issuing_country": "TWN",
                    "taiwan_passport": True,
                }

        result = parse_quick_register_text("任意粘贴", llm=Fake())
        self.assertEqual(result.get("source"), "llm")
        self.assertEqual(result.get("issuing_country"), "CHN")
        self.assertTrue(result.get("taiwan_passport"))
        self.assertEqual(result.get("id_type"), "PASSPORT")

    def test_llm_failure_falls_back_to_regex(self):
        class Boom:
            def parse_quick_register_text(self, text: str) -> dict:
                raise RuntimeError("no llm")

        text = "註冊地址：香港九龍旺角"
        result = parse_quick_register_text(text, llm=Boom())
        self.assertEqual(result.get("source"), "regex")
        self.assertIn("香港九龍", result.get("registered_office_cn") or "")


class TestIssuingCountry(unittest.TestCase):
    def test_taiwan_normalize(self):
        self.assertEqual(normalize_issuing_iso("TWN"), "CHN")
        self.assertEqual(normalize_issuing_iso("台湾"), "CHN")
        self.assertEqual(normalize_issuing_iso("Uzbekistan"), "UZB")

    def test_nnc1_uzbekistan_passport_country(self):
        plan = nnc1_identity_fill_plan("PASSPORT", "FA0266712", "UZB")
        self.assertEqual(plan["hkid"], "無")
        self.assertEqual(plan["passport"], "FA0266712")
        self.assertEqual(plan["passport_country"], "烏茲別克斯坦")
        names = passport_country_option_names("UZB")
        self.assertTrue(any("烏茲別克" in n for n in names))
        names_tw = passport_country_option_names("TWN")
        self.assertIn("中國", names_tw)

    def test_aggregator_copies_issuing_country(self):
        from src.materials.aggregator import aggregate_company_data

        data = aggregate_company_data(
            {
                "company_name_en": {"field_value": "Foo Ltd"},
                "director_name": {"field_value": "WANG LEI"},
                "id_type": {"field_value": "PASSPORT"},
                "id_number": {"field_value": "FA0266712"},
                "issuing_country": {"field_value": "UZB"},
            }
        )
        self.assertEqual((data.get("identity_proof") or {}).get("issuing_country"), "UZB")
        self.assertEqual((data.get("founder_members") or [{}])[0].get("issuing_country"), "UZB")

    def test_coerce_taiwan_flag(self):
        out = coerce_parse_result(
            {"issuing_country": "TWN", "id_type": "PASSPORT", "id_number": "A1"}
        )
        self.assertEqual(out["issuing_country"], "CHN")
        self.assertTrue(out.get("taiwan_passport"))

    def test_hk_office_plus_prc_id_overrides_llm_hkid(self):
        paste = (
            "身份证号码：44051420000318492X\n"
            "注册地址：香港新界葵涌葵喜街1-11号达利国际中心9楼909O"
        )
        class Fake:
            def parse_quick_register_text(self, text: str) -> dict:
                return {
                    "id_type": "HKID",
                    "id_number": "44051420000318492X",
                    "registered_office_cn": "香港新界葵涌葵喜街1-11号达利国际中心9楼909O",
                }

        result = parse_quick_register_text(paste, llm=Fake())
        self.assertEqual(result.get("id_type"), "PRC_ID")
        self.assertIn("香港新界", result.get("registered_office_cn") or "")

    def test_coerce_labels_override_llm(self):
        prc = coerce_parse_result(
            {"id_type": "HKID", "id_number": "44051420000318492X"},
            source_text="身份证号码：44051420000318492X\n注册地址：香港新界",
        )
        self.assertEqual(prc["id_type"], "PRC_ID")
        hkid = coerce_parse_result(
            {"id_type": "PRC_ID", "id_number": "F570235（2）"},
            source_text="香港身份证号码：F570235（2）",
        )
        self.assertEqual(hkid["id_type"], "HKID")
        ppt = coerce_parse_result(
            {"id_type": "HKID", "id_number": "FA0266712"},
            source_text="护照号码：FA0266712",
        )
        self.assertEqual(ppt["id_type"], "PASSPORT")


if __name__ == "__main__":
    unittest.main()
