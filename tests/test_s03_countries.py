"""s03 非香港國家／地區：入库用 ICRIS 下拉原文。"""

from __future__ import annotations

import unittest

from src.materials.countries import (
    icris_country_select_candidates,
    resolve_s03_address_country,
)

FIXTURE = [
    {"label": "中國", "value": "CHN"},
    {"label": "澳門", "value": "MAC"},
    {"label": "台灣", "value": "TWN"},
    {"label": "烏茲別克", "value": "UZB"},
    {"label": "美國", "value": "USA"},
]


class TestResolveS03AddressCountry(unittest.TestCase):
    def test_iso3_uzbekistan_to_icris_label(self):
        self.assertEqual(
            resolve_s03_address_country("UZB", options=FIXTURE),
            "烏茲別克",
        )

    def test_simplified_maps_to_traditional(self):
        self.assertEqual(
            resolve_s03_address_country("乌兹别克斯坦", options=FIXTURE),
            "烏茲別克",
        )

    def test_already_icris_label(self):
        self.assertEqual(
            resolve_s03_address_country("中國", options=FIXTURE),
            "中國",
        )

    def test_chn_taiwan_macao_separate(self):
        self.assertEqual(
            resolve_s03_address_country("CHN", options=FIXTURE),
            "中國",
        )
        self.assertEqual(
            resolve_s03_address_country("TWN", options=FIXTURE),
            "台灣",
        )
        self.assertEqual(
            resolve_s03_address_country("MAC", options=FIXTURE),
            "澳門",
        )
        self.assertNotEqual(
            resolve_s03_address_country("HKG", options=FIXTURE),
            "中國",
        )

    def test_not_iso3(self):
        self.assertNotEqual(
            resolve_s03_address_country("UZB", options=FIXTURE),
            "UZB",
        )

    def test_s04_candidates_taiwan_macao(self):
        self.assertEqual(
            icris_country_select_candidates("TWN", options=FIXTURE)[0],
            "台灣",
        )
        self.assertEqual(
            icris_country_select_candidates("MAC", options=FIXTURE)[0],
            "澳門",
        )
        cands = icris_country_select_candidates("UZB", options=FIXTURE)
        self.assertEqual(cands[0], "烏茲別克")
        self.assertIn("UZB", cands)
        self.assertNotEqual(cands[0], "烏茲別克斯坦")

    def test_live_dump_if_present(self):
        from src.materials.countries import S03_COUNTRIES_PATH, load_s03_country_options

        if not S03_COUNTRIES_PATH.is_file():
            self.skipTest("no dump")
        opts = load_s03_country_options(reload=True)
        self.assertGreaterEqual(len(opts), 50)
        self.assertEqual(resolve_s03_address_country("CHN", options=opts), "中國")
        self.assertEqual(resolve_s03_address_country("TWN", options=opts), "台灣")
        self.assertEqual(resolve_s03_address_country("MAC", options=opts), "澳門")
        self.assertEqual(resolve_s03_address_country("UZB", options=opts), "烏茲別克")
        cands = icris_country_select_candidates("UZB", options=opts)
        self.assertEqual(cands[0], "烏茲別克")
        self.assertTrue(all(r.get("label") and r.get("value") for r in opts))


if __name__ == "__main__":
    unittest.main()
