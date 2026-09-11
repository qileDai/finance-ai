import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.wework.icris_form_notify import (
    fields_from_payload,
    format_prelim_message,
    send_prelim_notify,
    strip_prelim_markdown,
)


def _payload() -> dict:
    return {
        "company_name_cn": "撼世全球有限公司",
        "company_name_en": "Humsienk Global Limited",
        "directors": [
            {
                "name_cn": "姚曉佳",
                "name_en": "YAU SIU KA",
                "id_type": "PRC_ID",
                "id_number": "362531199002111537",
            }
        ],
        "applicant": {
            "name_cn": "姚曉佳",
            "id_type": "PRC_ID",
            "id_number": "362531199002111537",
        },
        "icris_account": {"username": "yx84925ytg11u"},
    }


class TestPrelimNotifyMessage(unittest.TestCase):
    def test_fields_from_payload_prefers_director(self):
        fields = fields_from_payload(_payload())
        self.assertEqual(fields["company_name_cn"], "撼世全球有限公司")
        self.assertEqual(fields["company_name_en"], "Humsienk Global Limited")
        self.assertEqual(fields["shareholder_name"], "姚曉佳")
        self.assertEqual(fields["id_type"], "PRC_ID")
        self.assertEqual(fields["id_number"], "362531199002111537")
        self.assertEqual(fields["icris_username"], "yx84925ytg11u")

    def test_pass_message_has_no_reject_reason(self):
        msg = format_prelim_message(
            128, fields_from_payload(_payload()), passed=True, reasons="应被忽略"
        )
        self.assertIn("【NNC1 初步检查通过】", msg)
        self.assertIn('<font color="info">【NNC1 初步检查通过】</font>', msg)
        self.assertIn("任务 #128", msg)
        self.assertIn("撼世全球有限公司", msg)
        self.assertIn("Humsienk Global Limited", msg)
        self.assertIn("姚曉佳", msg)
        self.assertIn("PRC_ID", msg)
        self.assertIn("362531199002111537", msg)
        self.assertIn("yx84925ytg11u", msg)
        self.assertIn('初步检查结果: <font color="info">通过</font>', msg)
        self.assertNotIn("拒絕原因", msg)
        self.assertNotIn("应被忽略", msg)
        self.assertNotIn("填表耗时", msg)
        plain = strip_prelim_markdown(msg)
        self.assertNotIn("<font", plain)
        self.assertIn("初步检查结果: 通过", plain)

    def test_includes_nnc1_duration(self):
        msg = format_prelim_message(
            128,
            fields_from_payload(_payload()),
            passed=True,
            nnc1_duration="3分12秒",
        )
        self.assertIn("填表耗时：3分12秒", msg)
        self.assertLess(msg.find("填表耗时："), msg.find("公司中文名:"))

    def test_empty_nnc1_duration_omitted(self):
        msg = format_prelim_message(
            128, fields_from_payload(_payload()), passed=True, nnc1_duration="  "
        )
        self.assertNotIn("填表耗时", msg)

    def test_reject_message_includes_reasons(self):
        reasons = "1. 公司名称与身份证明不符\n2. 地址不完整"
        msg = format_prelim_message(
            128,
            fields_from_payload(_payload()),
            passed=False,
            reasons=reasons,
        )
        self.assertIn("【NNC1 初步检查拒絕】", msg)
        self.assertIn('<font color="warning">【NNC1 初步检查拒絕】</font>', msg)
        self.assertIn('初步检查结果: <font color="warning">拒絕</font>', msg)
        self.assertIn("拒絕原因:", msg)
        self.assertIn("公司名称与身份证明不符", msg)
        self.assertIn("地址不完整", msg)
        self.assertIn("撼世全球有限公司", msg)
        self.assertIn("362531199002111537", msg)
        self.assertIn("姚曉佳", msg)


class TestSendPrelimNotify(unittest.TestCase):
    def test_skips_when_no_channel(self):
        client = MagicMock()
        with patch("config.settings.settings") as settings:
            settings.icris_review_webhook_url = ""
            settings.icris_review_notify_chat_id = ""
            ok = send_prelim_notify(
                1, _payload(), passed=True, client=client
            )
        self.assertFalse(ok)
        client.send_webhook_markdown.assert_not_called()
        client.send_webhook_image.assert_not_called()
        client.send_group_text.assert_not_called()

    def test_webhook_markdown_then_image(self):
        client = MagicMock()
        shot = Path(__file__).resolve()
        with patch("config.settings.settings") as settings:
            settings.icris_review_webhook_url = "https://qyapi.weixin.qq.com/hook"
            settings.icris_review_notify_chat_id = "chat"
            ok = send_prelim_notify(
                128,
                _payload(),
                passed=True,
                screenshot_path=str(shot),
                nnc1_duration="3分12秒",
                client=client,
            )
        self.assertTrue(ok)
        client.send_webhook_markdown.assert_called_once()
        content = client.send_webhook_markdown.call_args[0][1]
        self.assertIn("初步检查通过", content)
        self.assertIn('<font color="info">', content)
        self.assertIn("填表耗时：3分12秒", content)
        client.send_webhook_image.assert_called_once_with(
            "https://qyapi.weixin.qq.com/hook", str(shot)
        )
        client.send_group_text.assert_not_called()

    def test_text_fallback_strips_font_tags(self):
        client = MagicMock()
        with patch("config.settings.settings") as settings:
            settings.icris_review_webhook_url = ""
            settings.icris_review_notify_chat_id = "chat"
            ok = send_prelim_notify(
                128, _payload(), passed=False, reasons="名称相同", client=client
            )
        self.assertTrue(ok)
        client.send_webhook_markdown.assert_not_called()
        client.send_group_text.assert_called_once()
        text = client.send_group_text.call_args[0][1]
        self.assertNotIn("<font", text)
        self.assertIn("【NNC1 初步检查拒絕】", text)
        self.assertIn("初步检查结果: 拒絕", text)
        self.assertIn("名称相同", text)


if __name__ == "__main__":
    unittest.main()
