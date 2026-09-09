import unittest

from src.browser.icris_nnc1_form import (
    _PRELIM_REASON_COLLECT_JS,
    _PRELIM_RESULT_COLLECT_JS,
    first_real_signatory_index,
    format_prelim_reject_error,
    is_signatory_placeholder,
    normalize_prelim_reject_reasons,
    pick_best_prelim_reasons,
    pick_best_prelim_result,
    prelim_check_passed,
    prelim_result_preference_score,
    resolve_nnc1_step3_names,
)
from src.wework.icris_form_notify import format_prelim_message


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


class TestNnc1SignatoryAndPrelim(unittest.TestCase):
    def test_signatory_placeholder(self):
        self.assertTrue(is_signatory_placeholder("请选择"))
        self.assertTrue(is_signatory_placeholder("請選擇"))
        self.assertTrue(is_signatory_placeholder(""))
        self.assertFalse(is_signatory_placeholder("甄欣荣"))

    def test_first_real_signatory_skips_placeholder(self):
        self.assertEqual(
            first_real_signatory_index(["请选择", "甄欣荣", "另一人"]),
            1,
        )

    def test_prelim_check_passed(self):
        self.assertTrue(
            prelim_check_passed("通过。请按“继续”按钮以完成提交过程。")
        )
        self.assertTrue(prelim_check_passed("通過，請按繼續"))
        self.assertFalse(prelim_check_passed("不通过：名称已存在"))
        self.assertFalse(prelim_check_passed(""))
        self.assertFalse(prelim_check_passed("拒絕"))
        self.assertFalse(prelim_check_passed("拒绝"))
        self.assertFalse(prelim_check_passed("Rejection"))
        self.assertFalse(
            prelim_check_passed("拒絕。通过。请按“继续”按钮以完成提交过程。")
        )
        self.assertGreater(
            prelim_result_preference_score("拒絕"),
            prelim_result_preference_score("通过。请按“继续”按钮以完成提交过程。"),
        )

    def test_pick_best_prefers_reject_over_continue_hint(self):
        picked = pick_best_prelim_result(
            [
                "通过。请按“继续”按钮以完成提交过程。",
                "拒絕",
            ]
        )
        self.assertEqual(picked, "拒絕")
        self.assertFalse(prelim_check_passed(picked))
        self.assertTrue(
            prelim_check_passed("通过。请按“继续”按钮以完成提交过程。")
        )

    def test_reasons_override_pass_hint(self):
        reasons = (
            "1. 建議採用的公司名稱 [Humsienk Global Limited] "
            "與另一間已註冊的公司名稱相同。\n"
            "2. 建議採用的公司名稱 [撼世全球有限公司] "
            "與另一間已註冊的公司名稱相同。"
        )
        self.assertFalse(
            prelim_check_passed(
                "通过。请按“继续”按钮以完成提交过程。",
                reasons,
            )
        )
        msg = format_prelim_message(
            128,
            {
                "company_name_cn": "撼世全球有限公司",
                "company_name_en": "Humsienk Global Limited",
                "shareholder_name": "戴啟樂",
                "id_type": "PRC_ID",
                "id_number": "362531199002111537",
                "icris_username": "Dq11535yt9mcf",
            },
            passed=False,
            reasons=reasons,
        )
        self.assertIn("【NNC1 初步检查拒絕】", msg)
        self.assertIn("撼世全球有限公司", msg)
        self.assertIn("Humsienk Global Limited", msg)
        self.assertIn("拒絕原因:", msg)
        self.assertIn("與另一間已註冊的公司名稱相同", msg)

    def test_collect_js_scans_all_cells_and_non_acceptance(self):
        self.assertIn("cells.length - 1", _PRELIM_RESULT_COLLECT_JS)
        self.assertIn("ant-descriptions-item", _PRELIM_RESULT_COLLECT_JS)
        self.assertIn("不予接納原因", _PRELIM_REASON_COLLECT_JS)
        self.assertIn("拒絕原因", _PRELIM_REASON_COLLECT_JS)
        self.assertEqual(
            pick_best_prelim_reasons(
                ["短", "1. 名稱相同\n2. 中文名稱相同"]
            ),
            "1. 名稱相同\n2. 中文名稱相同",
        )

    def test_format_prelim_reject_error_includes_reasons(self):
        reasons = (
            "1. 建議採用的公司名稱 [Humsienk Global Limited] "
            "與另一間已註冊的公司名稱相同。\n"
            "2. 建議採用的公司名稱 [撼世全球有限公司] "
            "與另一間已註冊的公司名稱相同。"
        )
        msg = format_prelim_reject_error("拒絕", reasons)
        self.assertIn("初步检查拒绝", msg)
        self.assertIn("Humsienk Global Limited", msg)
        self.assertIn("撼世全球有限公司", msg)
        self.assertEqual(
            format_prelim_reject_error("拒絕", ""),
            "初步检查未通过: 拒絕",
        )
        self.assertEqual(
            normalize_prelim_reject_reasons("  1. a  \n\n  2. b  "),
            "1. a\n2. b",
        )


if __name__ == "__main__":
    unittest.main()
