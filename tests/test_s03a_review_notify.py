import hashlib
import tempfile
import unittest
from pathlib import Path

from src.browser.icris_registration import format_s03a_review_message
from src.wework.client import build_webhook_image_payload


class TestS03aReviewMessage(unittest.TestCase):
    def _data(self) -> dict:
        return {
            "company_name_cn": "撼世全球有限公司",
            "company_name_en": "Humsienk Global Limited",
            "applicant": {
                "name_cn": "姚曉佳",
                "name_en": "",
                "id_type": "PRC_ID",
                "id_number": "44051420000318492X",
                "email": "",
            },
            "contact": {"email": "13828784214@163.com"},
            "icris_account": {
                "username": "yx84925ytg11u",
                "password": "yx84925ytg11u@",
            },
        }

    def test_message_includes_details(self):
        msg = format_s03a_review_message(
            83, self._data(), admin_public_url=""
        )
        self.assertIn("任务 #83", msg)
        self.assertIn("撼世全球有限公司", msg)
        self.assertIn("Humsienk Global Limited", msg)
        self.assertIn("姚曉佳", msg)
        self.assertIn("PRC_ID", msg)
        self.assertIn("44051420000318492X", msg)
        self.assertIn("yx84925ytg11u", msg)
        self.assertIn("yx84925ytg11u@", msg)
        self.assertIn("13828784214@163.com", msg)
        self.assertNotIn("127.0.0.1", msg)
        self.assertNotIn("localhost", msg)
        self.assertNotIn("后台：", msg)
        self.assertNotIn("打开任务", msg)
        self.assertNotIn("耗时：", msg)

    def test_skips_localhost_admin_url(self):
        msg = format_s03a_review_message(
            83,
            self._data(),
            admin_public_url="http://127.0.0.1:8082/admin",
        )
        self.assertNotIn("127.0.0.1", msg)
        self.assertNotIn("/jobs/83", msg)
        self.assertNotIn("后台：", msg)

    def test_omits_public_admin_link(self):
        msg = format_s03a_review_message(
            83,
            self._data(),
            admin_public_url="https://www.szyingtai.cn/admin",
        )
        self.assertNotIn("https://www.szyingtai.cn/admin/jobs/83", msg)
        self.assertNotIn("szyingtai.cn", msg)
        self.assertNotIn("打开任务", msg)

    def test_includes_s03a_duration(self):
        msg = format_s03a_review_message(
            83, self._data(), s03a_duration="5分23秒"
        )
        self.assertIn("耗时：5分23秒", msg)
        self.assertLess(msg.find("耗时："), msg.find("公司："))

    def test_empty_fields_omitted(self):
        msg = format_s03a_review_message(1, {}, admin_public_url="")
        self.assertIn("任务 #1", msg)
        self.assertNotIn("公司：", msg)
        self.assertNotIn("申请人：", msg)
        self.assertNotIn("账号：", msg)
        self.assertNotIn("耗时：", msg)
        self.assertNotIn("后台：", msg)


class TestWebhookImagePayload(unittest.TestCase):
    def test_small_png_payload(self):
        from PIL import Image

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "shot.png"
        Image.new("RGB", (20, 20), color=(10, 20, 30)).save(path)
        raw = path.read_bytes()
        payload = build_webhook_image_payload(str(path))
        self.assertEqual(payload["msgtype"], "image")
        self.assertEqual(
            payload["image"]["md5"], hashlib.md5(raw).hexdigest()
        )
        self.assertTrue(payload["image"]["base64"])


if __name__ == "__main__":
    unittest.main()
