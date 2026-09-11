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
        self.assertTrue(
            s03_skip_english_name("", name_cn="張慧斌", name_en="ZHANG Huibin")
        )
        self.assertTrue(s03_skip_english_name("胡丹东", name_en="HU Dandong"))

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

    def test_hudandong_pinyin_not_in_payload_username_kept(self):
        materials = self._materials("胡丹东")
        materials["director_name_cn"] = {"field_value": "胡丹东"}
        materials["director_surname_en"] = {"field_value": "HU"}
        materials["director_given_en"] = {"field_value": "Dandong"}
        data = aggregate_company_data(materials)
        applicant = data.get("applicant") or {}
        self.assertEqual(applicant.get("name_cn"), "胡丹东")
        self.assertEqual(applicant.get("name_en") or "", "")
        self.assertEqual(applicant.get("surname_en") or "", "")
        self.assertEqual(applicant.get("given_en") or "", "")
        directors = data.get("directors") or []
        self.assertTrue(directors)
        self.assertEqual(directors[0].get("surname_en") or "", "")
        username = str((data.get("icris_account") or {}).get("username") or "")
        self.assertTrue(username)
        self.assertRegex(username.lower(), r"^hd")
        self.assertIn("yt", username.lower())

    def test_hudandong_username_from_pinyin_without_en_fields(self):
        data = aggregate_company_data(self._materials("胡丹东"))
        applicant = data.get("applicant") or {}
        self.assertEqual(applicant.get("name_cn"), "胡丹东")
        self.assertEqual(applicant.get("name_en") or "", "")
        self.assertEqual(applicant.get("surname_en") or "", "")
        self.assertEqual(applicant.get("given_en") or "", "")
        username = str((data.get("icris_account") or {}).get("username") or "")
        self.assertRegex(username.lower(), r"^hd")
        self.assertIn("yt", username.lower())

    def test_build_materials_keeps_user_english(self):
        from src.web.admin_runner import (
            _build_materials,
            _strip_unparsed_director_english,
        )

        fields = {
            "company_name_en": "Foo Ltd",
            "director_name": "胡丹东",
            "director_name_cn": "胡丹东",
            "director_surname_en": "HU",
            "director_given_en": "Dandong",
            "director_name_en": "HU Dandong",
            "id_number": "340421198611163844",
        }
        _strip_unparsed_director_english(fields)
        self.assertEqual(fields["director_surname_en"], "HU")
        self.assertEqual(fields["director_given_en"], "Dandong")
        self.assertEqual(fields["director_name_en"], "HU Dandong")
        mats = _build_materials(fields, {})
        self.assertEqual(mats["director_surname_en"]["field_value"], "HU")
        self.assertEqual(mats["director_given_en"]["field_value"], "Dandong")
        self.assertEqual(mats["director_name"]["field_value"], "胡丹东")

    def test_strip_keeps_bracket_english(self):
        from src.web.admin_runner import _strip_unparsed_director_english

        fields = {
            "director_name": "張慧斌【ZHANG，Huibin】",
            "director_surname_en": "ZHANG",
            "director_given_en": "Huibin",
        }
        _strip_unparsed_director_english(fields)
        self.assertEqual(fields["director_surname_en"], "ZHANG")
        self.assertEqual(fields["director_given_en"], "Huibin")


class TestS02UsernamePinyin(unittest.TestCase):
    def test_person_en_helper_pinyin_vs_latin(self):
        from src.materials.aggregator import person_en_for_icris_username

        self.assertEqual(person_en_for_icris_username("", "胡丹东"), "Hu Dandong")
        self.assertEqual(
            person_en_for_icris_username("CHAN Tai Man", "陳大文"),
            "CHAN Tai Man",
        )
        self.assertEqual(
            person_en_for_icris_username("ZHANG Huibin", "張慧斌"),
            "ZHANG Huibin",
        )

    def test_derive_hudandong_username_has_pinyin_not_written_back(self):
        from src.browser.icris_registration import derive_icris_credentials

        data = {
            "applicant": {
                "name_cn": "胡丹东",
                "name_en": "",
                "id_number": "340421198611163846",
                "director_name": "胡丹东",
            },
            "identity_proof": {"id_number": "340421198611163846"},
            "icris_account": {},
        }
        user, _pwd = derive_icris_credentials(data)
        self.assertRegex(user, r"^[Hh]d63846yt")
        self.assertEqual(data["applicant"].get("name_en") or "", "")
        self.assertEqual(data["applicant"].get("surname_en") or "", "")

    def test_derive_uses_english_initials_not_pinyin(self):
        from src.browser.icris_registration import derive_icris_credentials

        data = {
            "applicant": {
                "name_cn": "陳大文",
                "name_en": "CHAN Tai Man",
                "surname_en": "CHAN",
                "given_en": "Tai Man",
                "id_number": "A1234567",
            },
            "identity_proof": {"id_number": "A1234567"},
            "icris_account": {},
        }
        user, _pwd = derive_icris_credentials(data)
        self.assertRegex(user.lower(), r"^ctm")
        self.assertEqual(data["applicant"]["name_en"], "CHAN Tai Man")

    def test_retry_path_hudandong_has_pinyin(self):
        from src.browser.icris_registration import _person_en_and_id_from_data
        from src.materials.aggregator import _generate_icris_credentials

        data = {
            "applicant": {"name_cn": "胡丹东", "name_en": ""},
            "identity_proof": {"id_number": "340421198611163846"},
        }
        person_en, id_number = _person_en_and_id_from_data(data)
        user, _pwd = _generate_icris_credentials(person_en, id_number, retry=True)
        self.assertRegex(user.lower(), r"^hd63846yt")
        self.assertEqual(data["applicant"].get("name_en") or "", "")

    def test_bracket_english_username_zhang_huibin(self):
        materials = {
            "company_name_cn": {"field_value": "撼世全球有限公司"},
            "company_name_en": {"field_value": "Humsienk Global Limited"},
            "director_name": {"field_value": "張慧斌【ZHANG，Huibin】"},
            "director_name_cn": {"field_value": "張慧斌"},
            "director_surname_en": {"field_value": "ZHANG"},
            "director_given_en": {"field_value": "Huibin"},
            "id_type": {"field_value": "PRC_ID"},
            "id_number": {"field_value": "44051420000318492X"},
        }
        data = aggregate_company_data(materials)
        applicant = data.get("applicant") or {}
        self.assertEqual(applicant.get("surname_en"), "ZHANG")
        self.assertEqual(applicant.get("given_en"), "Huibin")
        username = str((data.get("icris_account") or {}).get("username") or "")
        self.assertRegex(username.lower(), r"^zh")
        self.assertIn("yt", username.lower())


if __name__ == "__main__":
    unittest.main()
