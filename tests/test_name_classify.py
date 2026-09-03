"""董事姓名拆分：原文保留，括号种类不写死。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from src.materials.aggregator import aggregate_company_data
from src.materials.name_classify import (
    classify_director_name,
    weak_fallback_name,
)
from src.materials.quick_register_parse import parse_quick_register_text


class TestNameFallback(unittest.TestCase):
    def test_corner_brackets(self):
        out = weak_fallback_name("張慧斌【ZHANG，Huibin】")
        self.assertEqual(out["director_name_cn"], "張慧斌")
        self.assertEqual(out["director_surname_en"], "ZHANG")
        self.assertEqual(out["director_given_en"], "Huibin")

    def test_square_brackets(self):
        out = weak_fallback_name("張慧斌[ZHANG, Huibin]")
        self.assertEqual(out["director_name_cn"], "張慧斌")
        self.assertEqual(out["director_surname_en"], "ZHANG")
        self.assertEqual(out["director_given_en"], "Huibin")

    def test_pure_chinese(self):
        out = weak_fallback_name("姚曉佳")
        self.assertEqual(out["director_name_cn"], "姚曉佳")
        self.assertEqual(out["director_surname_en"], "")
        self.assertEqual(out["director_given_en"], "")

    def test_llm_wrong_keeps_cjk_only_in_cn(self):
        llm = MagicMock()
        llm.classify_director_name.return_value = {
            "name_cn": "張慧斌【ZHANG，Huibin】",
            "surname_en": "ZHANG",
            "given_en": "Huibin",
        }
        out = classify_director_name("張慧斌【ZHANG，Huibin】", llm=llm)
        self.assertEqual(out["director_name_cn"], "張慧斌")
        self.assertEqual(out["director_surname_en"], "ZHANG")

    def test_llm_chinese_only_fills_wrapped_english(self):
        llm = MagicMock()
        llm.classify_director_name.return_value = {"name_cn": "張慧斌"}
        out = classify_director_name("張慧斌【ZHANG，Huibin】", llm=llm)
        self.assertEqual(out["director_name_cn"], "張慧斌")
        self.assertEqual(out["director_surname_en"], "ZHANG")
        self.assertEqual(out["director_given_en"], "Huibin")

    def test_pure_chinese_llm_pinyin_dropped(self):
        llm = MagicMock()
        llm.classify_director_name.return_value = {
            "name_cn": "姚曉佳",
            "surname_en": "Yao",
            "given_en": "Xiaojia",
        }
        out = classify_director_name("姚曉佳", llm=llm)
        self.assertEqual(out["director_name_cn"], "姚曉佳")
        self.assertEqual(out["director_surname_en"], "")
        self.assertEqual(out["director_given_en"], "")

    def test_comma_english_name_not_chinese(self):
        out = weak_fallback_name("KHALILOV，AKHTAM")
        self.assertEqual(out["director_name_cn"], "")
        self.assertEqual(out["director_surname_en"], "KHALILOV")
        self.assertEqual(out["director_given_en"], "AKHTAM")


class TestParseAndAggregateName(unittest.TestCase):
    def test_parse_keeps_raw_and_splits(self):
        class Fake:
            def parse_quick_register_text(self, text: str) -> dict:
                return {"director_name": "張慧斌【ZHANG，Huibin】"}

        result = parse_quick_register_text("x", llm=Fake())
        self.assertEqual(result.get("director_name"), "張慧斌【ZHANG，Huibin】")
        self.assertEqual(result.get("director_name_cn"), "張慧斌")
        self.assertEqual(result.get("director_surname_en"), "ZHANG")
        self.assertEqual(result.get("director_given_en"), "Huibin")

    def test_aggregator_s03_fields(self):
        data = aggregate_company_data(
            {
                "company_name_en": {"field_value": "Foo Ltd"},
                "director_name": {"field_value": "張慧斌【ZHANG，Huibin】"},
                "director_name_cn": {"field_value": "張慧斌"},
                "director_surname_en": {"field_value": "ZHANG"},
                "director_given_en": {"field_value": "Huibin"},
                "id_type": {"field_value": "PRC_ID"},
                "id_number": {"field_value": "44051420000318492X"},
            }
        )
        applicant = data.get("applicant") or {}
        self.assertEqual(applicant.get("name_cn"), "張慧斌")
        self.assertEqual(applicant.get("surname_en"), "ZHANG")
        self.assertEqual(applicant.get("given_en"), "Huibin")
        self.assertNotIn("【", applicant.get("name_cn") or "")

    def test_aggregator_comma_english_name(self):
        data = aggregate_company_data(
            {
                "company_name_en": {"field_value": "Foo Ltd"},
                "director_name": {"field_value": "KHALILOV，AKHTAM"},
                "id_type": {"field_value": "PASSPORT"},
                "id_number": {"field_value": "FA0266712"},
            }
        )
        applicant = data.get("applicant") or {}
        self.assertEqual(applicant.get("name_cn") or "", "")
        self.assertNotIn("，", applicant.get("name_cn") or "")
        self.assertNotIn(",", applicant.get("name_cn") or "")
        self.assertEqual(applicant.get("surname_en"), "KHALILOV")
        self.assertEqual(applicant.get("given_en"), "AKHTAM")


if __name__ == "__main__":
    unittest.main()
