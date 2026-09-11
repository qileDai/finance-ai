"""快速注册跑注册：表单最终值入库，不被 paste / 二次分类覆盖。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src.materials.aggregator import aggregate_company_data
from src.web.admin_runner import (
    _apply_submitted_form_to_company_data,
    _build_materials,
    _prepare_submit_fields,
    _strip_unparsed_director_english,
)


class TestPrepareSubmitFields(unittest.TestCase):
    def test_keeps_form_id_type_when_paste_says_passport(self):
        fields = _prepare_submit_fields(
            {
                "id_type": "HKID",
                "id_number": "A123456(7)",
                "paste_text": "护照号码：E12345678\n董事：張三",
                "id_type_user_edited": "0",
            }
        )
        self.assertEqual(fields["id_type"], "HKID")
        self.assertEqual(fields["id_number"], "A123456(7)")
        self.assertNotIn("paste_text", fields)

    def test_keeps_user_address_street_and_non_hk(self):
        fake_addr = {
            "director_address_street": "SHOULD_NOT_USE",
            "director_address_region": "Guangdong",
            "address_is_hk": "1",
            "address_country": "HKG",
        }
        with patch(
            "src.materials.address_classify.classify_director_address",
            return_value=fake_addr,
        ):
            fields = _prepare_submit_fields(
                {
                    "director_address_en": "Some Road, Shenzhen, China",
                    "director_address_street": "Kejiyuan South Road",
                    "address_is_hk": "0",
                    "id_type": "PRC_ID",
                    "id_number": "44051420000318492X",
                }
            )
        self.assertEqual(fields["director_address_street"], "Kejiyuan South Road")
        self.assertEqual(fields["address_is_hk"], "0")
        self.assertEqual(fields["director_address_region"], "Guangdong")

    def test_keeps_cjk_name_with_user_english(self):
        fields = {
            "director_name": "姚曉佳",
            "director_name_cn": "姚曉佳",
            "director_surname_en": "YAU",
            "director_given_en": "SIU KA",
        }
        _strip_unparsed_director_english(fields)
        self.assertEqual(fields["director_surname_en"], "YAU")
        self.assertEqual(fields["director_given_en"], "SIU KA")
        prepared = _prepare_submit_fields(
            {
                **fields,
                "id_type": "PRC_ID",
                "id_number": "44051420000318492X",
            }
        )
        mats = _build_materials(prepared, {})
        data = aggregate_company_data(mats)
        _apply_submitted_form_to_company_data(data, prepared)
        director = (data.get("directors") or [{}])[0]
        applicant = data.get("applicant") or {}
        self.assertEqual(director.get("surname_en"), "YAU")
        self.assertEqual(director.get("given_en"), "SIU KA")
        self.assertEqual(applicant.get("surname_en"), "YAU")
        self.assertEqual(applicant.get("given_en"), "SIU KA")
        self.assertIn("YAU", str(director.get("name_en") or ""))


if __name__ == "__main__":
    unittest.main()
