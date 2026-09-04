"""证件类型：LLM 分类契约、NNC1-3.1 双栏映射、aggregator 创办成员字段。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from src.materials.aggregator import aggregate_company_data
from src.materials.id_type_classify import (
    CLASSIFY_ID_SYSTEM,
    classify_id_from_text,
    classify_id_user_prompt,
    nnc1_identity_fill_plan,
    normalize_stored_id_type,
    refine_id_type,
    split_hkid_number,
    weak_fallback_id_type,
)


PASTE_PRC = "董事+股东：姚曉佳\n身份证号码：44051420000318492X"
PASTE_HKID = "董事+股东：陳大文\n香港身份证号码：F570235（2）"
PASTE_PASSPORT = "董事+股东：WANG LEI\n护照号码：FA0266712"


class FakeLLM:
    def __init__(self, mapping: dict[str, dict[str, str]] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.mapping = mapping or {}

    def classify_id_document_text(self, text: str, id_number: str = "") -> dict:
        self.calls.append((text, id_number))
        blob = text or ""
        if "香港身份证" in blob:
            return {"id_type": "HKID", "id_number": "F570235（2）"}
        if "护照号码" in blob:
            return {"id_type": "PASSPORT", "id_number": "FA0266712"}
        if "身份证号码" in blob:
            return {"id_type": "PRC_ID", "id_number": "44051420000318492X"}
        return {"id_type": "", "id_number": id_number}


class TestClassifyPromptContract(unittest.TestCase):
    def test_system_prompt_covers_three_paste_labels(self):
        self.assertIn("香港身份证号码", CLASSIFY_ID_SYSTEM)
        self.assertIn("身份证号码", CLASSIFY_ID_SYSTEM)
        self.assertIn("护照号码", CLASSIFY_ID_SYSTEM)
        self.assertIn("護照号码", CLASSIFY_ID_SYSTEM)
        self.assertIn("HKID", CLASSIFY_ID_SYSTEM)
        self.assertIn("PRC_ID", CLASSIFY_ID_SYSTEM)
        self.assertIn("PASSPORT", CLASSIFY_ID_SYSTEM)

    def test_user_prompt_includes_paste_and_number(self):
        user = classify_id_user_prompt(PASTE_HKID, "F570235（2）")
        self.assertIn("香港身份证号码", user)
        self.assertIn("F570235（2）", user)


class TestClassifyIdFromText(unittest.TestCase):
    def test_mock_llm_three_paste_samples(self):
        llm = FakeLLM()
        prc = classify_id_from_text(PASTE_PRC, "44051420000318492X", llm=llm)
        self.assertEqual(prc["id_type"], "PRC_ID")
        hkid = classify_id_from_text(PASTE_HKID, "F570235（2）", llm=llm)
        self.assertEqual(hkid["id_type"], "HKID")
        ppt = classify_id_from_text(PASTE_PASSPORT, "FA0266712", llm=llm)
        self.assertEqual(ppt["id_type"], "PASSPORT")
        self.assertEqual(len(llm.calls), 3)

    def test_llm_failure_uses_weak_fallback_not_number_as_primary(self):
        class Boom:
            def classify_id_document_text(self, text: str, id_number: str = "") -> dict:
                raise RuntimeError("no llm")

        result = classify_id_from_text(PASTE_HKID, "F570235（2）", llm=Boom())
        self.assertEqual(result["id_type"], "HKID")
        prc = classify_id_from_text(PASTE_PRC, "44051420000318492X", llm=Boom())
        self.assertEqual(prc["id_type"], "PRC_ID")
        ppt = classify_id_from_text(PASTE_PASSPORT, "FA0266712", llm=Boom())
        self.assertEqual(ppt["id_type"], "PASSPORT")

    def test_invalid_llm_json_falls_back(self):
        llm = MagicMock()
        llm.classify_id_document_text.return_value = {"id_type": "UNKNOWN"}
        result = classify_id_from_text(PASTE_PRC, "44051420000318492X", llm=llm)
        self.assertEqual(result["id_type"], "PRC_ID")

    def test_mixed_passport_label_overrides_llm_prc(self):
        llm = MagicMock()
        llm.classify_id_document_text.return_value = {
            "id_type": "PRC_ID",
            "id_number": "FA0266712",
        }
        result = classify_id_from_text("護照号码：FA0266712", "FA0266712", llm=llm)
        self.assertEqual(result["id_type"], "PASSPORT")

    def test_refine_passport_simplified_traditional_mixed(self):
        self.assertEqual(refine_id_type("护照号码：FA0266712"), "PASSPORT")
        self.assertEqual(refine_id_type("護照號碼：FA0266712"), "PASSPORT")
        self.assertEqual(refine_id_type("護照号码：FA0266712"), "PASSPORT")
        self.assertEqual(refine_id_type("身分證號碼：44051420000318492X"), "PRC_ID")
        self.assertEqual(refine_id_type("香港身分證號碼：F570235（2）"), "HKID")


class TestNormalizeAndFillPlan(unittest.TestCase):
    def test_stored_type_respected(self):
        self.assertEqual(normalize_stored_id_type("HKID", "44051420000318492X"), "HKID")
        self.assertEqual(normalize_stored_id_type("PRC_ID", "FA0266712"), "PRC_ID")
        self.assertEqual(normalize_stored_id_type("PASSPORT", "F570235(2)"), "PASSPORT")

    def test_prc_ending_x_not_passport(self):
        self.assertEqual(
            normalize_stored_id_type("", "44051420000318492X"),
            "PRC_ID",
        )

    def test_nnc1_hkid_fills_hkid_and_passport_none(self):
        plan = nnc1_identity_fill_plan("HKID", "F570235（2）")
        self.assertEqual(plan["hkid"], "F570235（2）")
        self.assertEqual(plan["passport"], "無")
        self.assertEqual(plan["passport_country"], "")

    def test_nnc1_prc_fills_none_and_passport_number(self):
        plan = nnc1_identity_fill_plan("PRC_ID", "44051420000318492X")
        self.assertEqual(plan["hkid"], "無")
        self.assertEqual(plan["passport"], "44051420000318492X")
        self.assertEqual(plan["passport_country"], "中國")

    def test_nnc1_passport_same_as_prc_slot(self):
        plan = nnc1_identity_fill_plan("PASSPORT", "FA0266712")
        self.assertEqual(plan["hkid"], "無")
        self.assertEqual(plan["passport"], "FA0266712")
        self.assertEqual(plan["passport_country"], "中國")

    def test_nnc1_taiwan_passport_country(self):
        plan = nnc1_identity_fill_plan("PASSPORT", "FA0266712", "TWN")
        self.assertEqual(plan["hkid"], "無")
        self.assertEqual(plan["passport"], "FA0266712")
        self.assertEqual(plan["passport_country"], "台灣")

    def test_nnc1_macao_passport_country(self):
        plan = nnc1_identity_fill_plan("PASSPORT", "MA123456", "MAC")
        self.assertEqual(plan["hkid"], "無")
        self.assertEqual(plan["passport_country"], "澳門")

    def test_split_hkid_fullwidth_parens(self):
        main, check = split_hkid_number("F570235（2）")
        self.assertEqual(main, "F570235")
        self.assertEqual(check, "2")
        main2, check2 = split_hkid_number("F570235(2)")
        self.assertEqual((main2, check2), ("F570235", "2"))

    def test_weak_fallback_uses_label(self):
        self.assertEqual(weak_fallback_id_type(PASTE_HKID)["id_type"], "HKID")
        self.assertEqual(weak_fallback_id_type(PASTE_PRC)["id_type"], "PRC_ID")
        self.assertEqual(weak_fallback_id_type(PASTE_PASSPORT)["id_type"], "PASSPORT")


class TestAggregatorFounderIdFields(unittest.TestCase):
    def test_founder_and_director_get_id_type_number(self):
        materials = {
            "company_name_en": {"field_value": "Humsienk Global Limited"},
            "director_name": {"field_value": "姚曉佳"},
            "id_type": {"field_value": "HKID"},
            "id_number": {"field_value": "F570235（2）"},
        }
        data = aggregate_company_data(materials)
        founder = (data.get("founder_members") or [{}])[0]
        director = (data.get("directors") or [{}])[0]
        self.assertEqual(founder.get("id_type"), "HKID")
        self.assertEqual(founder.get("id_number"), "F570235（2）")
        self.assertEqual(director.get("id_type"), "HKID")
        self.assertEqual(director.get("id_number"), "F570235（2）")
        self.assertEqual((data.get("applicant") or {}).get("id_type"), "HKID")
        self.assertEqual((data.get("identity_proof") or {}).get("id_type"), "HKID")


if __name__ == "__main__":
    unittest.main()
