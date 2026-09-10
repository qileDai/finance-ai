"""公司中英文名：两个都填、或只填一个都可以。"""

from __future__ import annotations

import unittest

from src.materials.checklist import MATERIAL_FIELDS, _is_field_required, progress_summary
from src.materials.packager import collect_materials_from_dict
from src.web.admin_runner import _contact_email_only, _validate


def _file() -> dict:
    return {"id_card_front": {"data_url": "data:image/png;base64,xx"}}


def _base_fields(**extra: str) -> dict[str, str]:
    fields = {
        "director_name": "张三",
        "id_number": "440101199001011234",
        "contact_email": "a@b.com",
        "director_address_cn": "深圳市南山区",
    }
    fields.update(extra)
    return fields


def _mat(**vals: str) -> dict:
    return {k: {"field_value": v, "status": "received"} for k, v in vals.items()}


def _field(key: str):
    return next(f for f in MATERIAL_FIELDS if f.key == key)


class TestAdminValidateCompanyName(unittest.TestCase):
    def test_cn_only_ok(self):
        self.assertEqual(
            _validate(_base_fields(company_name_cn="撼世全球有限公司"), _file()),
            [],
        )

    def test_en_only_ok(self):
        self.assertEqual(
            _validate(_base_fields(company_name_en="Humsienk Global Limited"), _file()),
            [],
        )

    def test_both_ok(self):
        self.assertEqual(
            _validate(
                _base_fields(
                    company_name_cn="撼世全球有限公司",
                    company_name_en="Humsienk Global Limited",
                ),
                _file(),
            ),
            [],
        )

    def test_both_empty_fails(self):
        errs = _validate(_base_fields(), _file())
        self.assertTrue(any("中文名或英文名" in e for e in errs))

    def test_contact_email_with_remark_still_valid(self):
        self.assertEqual(
            _validate(
                _base_fields(
                    company_name_cn="撼世全球有限公司",
                    contact_email="a@b.com  主号",
                ),
                _file(),
            ),
            [],
        )


class TestContactEmailOnly(unittest.TestCase):
    def test_strips_remark(self):
        self.assertEqual(_contact_email_only("a@b.com  主号"), "a@b.com")

    def test_plain_email(self):
        self.assertEqual(_contact_email_only("a@b.com"), "a@b.com")

    def test_empty(self):
        self.assertEqual(_contact_email_only(""), "")


class TestChecklistCompanyName(unittest.TestCase):
    def test_cn_only_en_not_required(self):
        mats = _mat(company_name_cn="撼世全球有限公司")
        self.assertFalse(_is_field_required(_field("company_name_en"), mats))
        self.assertFalse(_is_field_required(_field("company_name_cn"), mats))
        missing = progress_summary(mats)["missing_labels"]
        self.assertNotIn("公司英文名", missing)
        self.assertNotIn("公司中文名", missing)

    def test_en_only_cn_not_required(self):
        mats = _mat(company_name_en="Foo Ltd")
        self.assertFalse(_is_field_required(_field("company_name_cn"), mats))
        missing = progress_summary(mats)["missing_labels"]
        self.assertNotIn("公司中文名", missing)
        self.assertNotIn("公司英文名", missing)

    def test_both_empty_required(self):
        mats: dict = {}
        self.assertTrue(_is_field_required(_field("company_name_cn"), mats))
        self.assertTrue(_is_field_required(_field("company_name_en"), mats))
        missing = progress_summary(mats)["missing_labels"]
        self.assertIn("公司中文名", missing)
        self.assertIn("公司英文名", missing)


class TestPackagerCompanyName(unittest.TestCase):
    def test_cn_only_complete_names(self):
        r = collect_materials_from_dict(
            {
                "company_name_cn": "撼世全球有限公司",
                "directors": [{}],
                "founder_members": [{}],
            }
        )
        self.assertNotIn("company_name", r["missing"])

    def test_en_only_complete_names(self):
        r = collect_materials_from_dict(
            {
                "company_name_en": "Foo Ltd",
                "directors": [{}],
                "founder_members": [{}],
            }
        )
        self.assertNotIn("company_name", r["missing"])

    def test_both_ok(self):
        r = collect_materials_from_dict(
            {
                "company_name_cn": "撼世",
                "company_name_en": "Foo Ltd",
                "directors": [{}],
                "founder_members": [{}],
            }
        )
        self.assertNotIn("company_name", r["missing"])

    def test_neither_missing(self):
        r = collect_materials_from_dict(
            {"directors": [{}], "founder_members": [{}]}
        )
        self.assertIn("company_name", r["missing"])
        self.assertFalse(r["complete"])


if __name__ == "__main__":
    unittest.main()
