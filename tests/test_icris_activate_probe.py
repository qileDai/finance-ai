import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.email.icris_activate import (
    resolve_probe_password,
    run_icris_activation_probe,
)
from src.storage.db import ExternalGroupStore
from src.web.admin_api import handle_admin_api

_S06 = (
    "https://www.e-services.cr.gov.hk/ICRIS3EF/system/registration/s06.do?code=x"
)
_HOME = "https://www.e-services.cr.gov.hk/ICRIS3EF/system/home.do"


def _load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "activate_icris.py"
    spec = importlib.util.spec_from_file_location("activate_icris_cli", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestResolveProbePassword(unittest.TestCase):
    def test_manual_wins_over_job(self):
        store = MagicMock()
        store.find_icris_password_by_username.return_value = "from-job"
        pwd, src = resolve_probe_password(
            store, username="MAWADA123", password="typed"
        )
        self.assertEqual(pwd, "typed")
        self.assertEqual(src, "manual")
        store.find_icris_password_by_username.assert_not_called()

    def test_empty_uses_job(self):
        store = MagicMock()
        store.find_icris_password_by_username.return_value = "from-job"
        pwd, src = resolve_probe_password(
            store, username="MAWADA123", password=""
        )
        self.assertEqual(pwd, "from-job")
        self.assertEqual(src, "job")

    def test_none_when_no_job(self):
        store = MagicMock()
        store.find_icris_password_by_username.return_value = ""
        pwd, src = resolve_probe_password(
            store, username="MAWADA123", password="  "
        )
        self.assertEqual(pwd, "")
        self.assertEqual(src, "none")


class TestActivationProbe(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExternalGroupStore(
            db_path=Path(self._tmp.name) / "probe.db"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add_imap(self) -> None:
        self.store.upsert_email_account(
            "foo@163.com",
            "imap.163.com",
            993,
            "foo@163.com",
            "auth-code",
        )

    def test_missing_imap(self) -> None:
        out = run_icris_activation_probe(
            self.store,
            username="MAWADA123",
            email="missing@163.com",
            fetch_link=lambda *a, **k: "https://example.com/act",
            activate=lambda *a: (True, "ok"),
        )
        self.assertFalse(out["ok"])
        self.assertFalse(out["found"])
        self.assertIn("未配置 IMAP", out["detail"])

    def test_no_mail(self) -> None:
        self._add_imap()
        out = run_icris_activation_probe(
            self.store,
            username="MAWADA123",
            email="foo@163.com",
            password="secret",
            fetch_link=lambda *a, **k: None,
            activate=lambda *a: (True, "should-not-run"),
        )
        self.assertFalse(out["ok"])
        self.assertFalse(out["found"])
        self.assertEqual(out["password_source"], "manual")
        self.assertIn("暂无", out["detail"])

    def test_manual_password_and_activate(self) -> None:
        self._add_imap()
        seen: list[tuple[str, str, str]] = []

        def fetch(account, expected_username=""):
            self.assertEqual(expected_username, "MAWADA123")
            return _S06

        def activate(url, user, pwd):
            seen.append((url, user, pwd))
            return True, "shot.png"

        out = run_icris_activation_probe(
            self.store,
            username="MAWADA123",
            email="foo@163.com",
            password="typed-pass",
            fetch_link=fetch,
            activate=activate,
        )
        self.assertTrue(out["ok"])
        self.assertTrue(out["found"])
        self.assertEqual(out["password_source"], "manual")
        self.assertEqual(seen[0][2], "typed-pass")
        self.assertIn("s06.do", seen[0][0])
        self.assertEqual(out.get("url"), _S06)

    def test_home_do_is_not_opened(self) -> None:
        self._add_imap()
        called = []

        out = run_icris_activation_probe(
            self.store,
            username="MAWADA123",
            email="foo@163.com",
            password="typed-pass",
            fetch_link=lambda *a, **k: _HOME,
            activate=lambda *a: called.append(a) or (True, "no"),
        )
        self.assertFalse(out["ok"])
        self.assertTrue(out["found"])
        self.assertIn("不是启动帐户链接", out["detail"])
        self.assertEqual(called, [])

    def test_job_password_readonly(self) -> None:
        self._add_imap()
        self.store.enqueue_registration_job(
            roomid="probe-room-1",
            payload={
                "icris_account": {
                    "username": "MAWADA123",
                    "password": "job-pass",
                }
            },
        )
        seen: list[str] = []

        def activate(url, user, pwd):
            seen.append(pwd)
            return True, "ok"

        out = run_icris_activation_probe(
            self.store,
            username="MAWADA123",
            email="foo@163.com",
            password="",
            fetch_link=lambda *a, **k: _S06,
            activate=activate,
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["password_source"], "job")
        self.assertEqual(seen, ["job-pass"])
        job = self.store.list_registration_jobs(limit=5)[0]
        self.assertNotEqual(job.get("activation_status"), "activated")

    def test_probe_does_not_call_job_markers(self) -> None:
        store = MagicMock()
        store.get_email_account_by_address.return_value = {
            "imap_host": "imap.163.com",
            "imap_port": 993,
            "username": "foo@163.com",
            "password": "auth",
        }
        store.find_icris_password_by_username.return_value = ""
        run_icris_activation_probe(
            store,
            username="U1",
            email="foo@163.com",
            password="p",
            fetch_link=lambda *a, **k: _S06,
            activate=lambda *a: (True, "ok"),
        )
        store.mark_job_activated.assert_not_called()
        store.mark_job_activation_failed.assert_not_called()
        store.mark_job_activation_checked.assert_not_called()

    def test_api_missing_fields(self) -> None:
        data, code = handle_admin_api(
            method="POST",
            path="/admin/api/icris-activate-probe",
            store=self.store,
            body={},
        )
        self.assertEqual(code, 400)
        self.assertFalse(data.get("ok"))

    def test_api_no_imap(self) -> None:
        data, code = handle_admin_api(
            method="POST",
            path="/admin/api/icris-activate-probe",
            store=self.store,
            body={"username": "U1", "email": "none@163.com"},
        )
        self.assertEqual(code, 400)
        self.assertIn("未配置 IMAP", str(data.get("error") or ""))

    def test_cli_requires_username_email(self) -> None:
        cli = _load_cli()
        with self.assertRaises(SystemExit):
            cli.parse_args([])

    def test_cli_missing_imap_exits_nonzero(self) -> None:
        cli = _load_cli()
        store = MagicMock()
        store.get_email_account_by_address.return_value = None
        with patch.object(cli, "ExternalGroupStore", return_value=store):
            code = cli.main(["--username", "U1", "--email", "none@163.com"])
        self.assertEqual(code, 1)


class TestFindIcrisPassword(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ExternalGroupStore(
            db_path=Path(self._tmp.name) / "pwd.db"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_latest_matching_username(self) -> None:
        self.store.enqueue_registration_job(
            roomid="r1",
            payload={"icris_account": {"username": "AA", "password": "old"}},
        )
        self.store.enqueue_registration_job(
            roomid="r2",
            payload={"icris_account": {"username": "BB", "password": "bb"}},
        )
        self.assertEqual(self.store.find_icris_password_by_username("BB"), "bb")
        self.assertEqual(self.store.find_icris_password_by_username("AA"), "old")
        self.assertEqual(self.store.find_icris_password_by_username("NOPE"), "")


if __name__ == "__main__":
    unittest.main()
