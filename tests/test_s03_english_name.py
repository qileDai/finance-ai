import unittest

from src.browser.icris_registration import (
    latin_english_given_surname,
    s03_skip_english_name,
)
from src.materials.aggregator import aggregate_company_data


class TestLatinEnglishGivenSurname(unittest.TestCase):
    def test_chinese_only_skips_english_fields(self):
        self.assertEqual(latin_english_given_surname("张三"), ("", ""))
        self.assertEqual(latin_english_given_surname("張三"), ("", ""))
        self.assertEqual(latin_english_given_surname(""), ("", ""))

    def test_latin_name_splits(self):
        given, surname = latin_english_given_surname("CHAN Tai Man")
        self.assertEqual((given, surname), ("Tai Man", "CHAN"))

    def test_mixed_cjk_without_latin_skips(self):
        self.assertEqual(latin_english_given_surname("李小明·"), ("", ""))

    def test_chinese_name_cn_skips_pinyin_en(self):
        self.assertEqual(latin_english_given_surname("Zhang San", "张三"), ("", ""))
        self.assertEqual(latin_english_given_surname("Yao Xiaojia", "姚曉佳"), ("", ""))

    def test_skip_english_uses_raw_director_name(self):
        self.assertTrue(s03_skip_english_name("姚曉佳", name_en="Yao Xiaojia"))
        self.assertFalse(
            s03_skip_english_name("張慧斌【ZHANG，Huibin】", name_cn="張慧斌")
        )
        self.assertTrue(s03_skip_english_name("", name_cn="姚曉佳", name_en=""))
        self.assertFalse(
            s03_skip_english_name("", name_cn="張慧斌", name_en="ZHANG Huibin")
        )

    def test_english_only_still_splits_when_name_cn_empty(self):
        given, surname = latin_english_given_surname("Yau Siu Ka", "")
        self.assertEqual((given, surname), ("Siu Ka", "Yau"))


class TestAggregatorChineseNameEn(unittest.TestCase):
    def _materials(self, director_name: str) -> dict:
        return {
            "company_name_cn": {"field_value": "撼世全球有限公司"},
            "company_name_en": {"field_value": "Humsienk Global Limited"},
            "director_name": {"field_value": director_name},
            "id_type": {"field_value": "PRC_ID"},
            "id_number": {"field_value": "44051420000318492X"},
        }

    def test_chinese_director_name_leaves_name_en_empty(self):
        data = aggregate_company_data(self._materials("姚曉佳"))
        applicant = data.get("applicant") or {}
        self.assertEqual(applicant.get("name_cn"), "姚曉佳")
        self.assertEqual(applicant.get("name_en"), "")
        self.assertEqual(applicant.get("surname_en") or "", "")
        directors = data.get("directors") or []
        self.assertTrue(directors)
        self.assertEqual(directors[0].get("name_cn"), "姚曉佳")
        self.assertEqual(directors[0].get("name_en"), "")
        username = str((data.get("icris_account") or {}).get("username") or "")
        self.assertTrue(username)

    def test_bracket_english_fills_s03_three_fields(self):
        materials = self._materials("張慧斌【ZHANG，Huibin】")
        materials["director_name_cn"] = {"field_value": "張慧斌"}
        materials["director_surname_en"] = {"field_value": "ZHANG"}
        materials["director_given_en"] = {"field_value": "Huibin"}
        data = aggregate_company_data(materials)
        applicant = data.get("applicant") or {}
        self.assertEqual(applicant.get("name_cn"), "張慧斌")
        self.assertEqual(applicant.get("surname_en"), "ZHANG")
        self.assertEqual(applicant.get("given_en"), "Huibin")
        self.assertEqual(applicant.get("name_en"), "ZHANG Huibin")

    def test_english_director_name_fills_name_en(self):
        data = aggregate_company_data(self._materials("Yau Siu Ka"))
        applicant = data.get("applicant") or {}
        self.assertEqual(applicant.get("name_en"), "Yau Siu Ka")
        self.assertEqual(applicant.get("name_cn"), "")

    def test_khalilov_comma_not_in_chinese_name(self):
        data = aggregate_company_data(self._materials("KHALILOV，AKHTAM"))
        applicant = data.get("applicant") or {}
        self.assertEqual(applicant.get("name_cn") or "", "")
        self.assertNotIn("，", applicant.get("name_cn") or "")
        self.assertEqual(applicant.get("surname_en"), "KHALILOV")
        self.assertEqual(applicant.get("given_en"), "AKHTAM")


if __name__ == "__main__":
    unittest.main()
