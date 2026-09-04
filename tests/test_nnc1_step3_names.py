import unittest

from src.browser.icris_nnc1_form import resolve_nnc1_step3_names


class TestResolveNnc1Step3Names(unittest.TestCase):
    def test_chinese_only_skips_english(self):
        cn, surname, given = resolve_nnc1_step3_names(
            {
                "name_cn": "姚曉佳",
                "name_en": "Yao Xiaojia",
                "director_name": "姚曉佳",
            }
        )
        self.assertEqual(cn, "姚曉佳")
        self.assertEqual(surname, "")
        self.assertEqual(given, "")

    def test_chinese_and_english_fills_three_fields(self):
        cn, surname, given = resolve_nnc1_step3_names(
            {
                "name_cn": "張慧斌",
                "name_en": "ZHANG Huibin",
                "surname_en": "ZHANG",
                "given_en": "Huibin",
                "director_name": "張慧斌【ZHANG，Huibin】",
            }
        )
        self.assertEqual(cn, "張慧斌")
        self.assertEqual(surname, "ZHANG")
        self.assertEqual(given, "Huibin")

    def test_english_only_splits_surname_given(self):
        cn, surname, given = resolve_nnc1_step3_names(
            {
                "name_cn": "",
                "name_en": "KHALILOV AKHTAM",
                "surname_en": "KHALILOV",
                "given_en": "AKHTAM",
            }
        )
        self.assertEqual(cn, "")
        self.assertEqual(surname, "KHALILOV")
        self.assertEqual(given, "AKHTAM")

    def test_english_only_from_name_en(self):
        cn, surname, given = resolve_nnc1_step3_names(
            {"name_cn": "", "name_en": "CHAN Tai Man"}
        )
        self.assertEqual(cn, "")
        self.assertEqual(surname, "CHAN")
        self.assertEqual(given, "Tai Man")

    def test_applicant_fallback_surname_given(self):
        cn, surname, given = resolve_nnc1_step3_names(
            {"name_cn": "張慧斌"},
            {
                "applicant": {
                    "surname_en": "ZHANG",
                    "given_en": "Huibin",
                    "director_name": "張慧斌【ZHANG，Huibin】",
                }
            },
        )
        self.assertEqual(cn, "張慧斌")
        self.assertEqual(surname, "ZHANG")
        self.assertEqual(given, "Huibin")

    def test_hudandong_skips_parsed_pinyin(self):
        cn, surname, given = resolve_nnc1_step3_names(
            {
                "name_cn": "胡丹东",
                "name_en": "HU Dandong",
                "surname_en": "HU",
                "given_en": "Dandong",
                "director_name": "胡丹东",
            }
        )
        self.assertEqual(cn, "胡丹东")
        self.assertEqual(surname, "")
        self.assertEqual(given, "")


if __name__ == "__main__":
    unittest.main()
