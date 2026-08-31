"""测试 ICRIS s02 用户名/密码派生（yingtai + s02 规范化）"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.browser.icris_registration import (
    _s02_password_meets_rules,
    derive_icris_credentials,
    finalize_s02_icris_credentials,
)
from src.materials.packager import load_mock_data


def test_yingtai_from_mock():
    data = load_mock_data()
    user, pwd = derive_icris_credentials(data)
    assert user[0].isupper(), f"username first letter should be upper: {user!r}"
    assert user[1].islower(), f"username second letter should be lower: {user!r}"
    assert user == user[0].upper() + user[1:], f"only first letter upper: {user!r}"
    assert user.endswith("yt") is False, "should have random suffix after yt"
    assert "yt" in user
    assert _s02_password_meets_rules(pwd), f"password not compliant: {pwd!r}"
    assert pwd.endswith("@"), pwd
    assert pwd.startswith(user[0]), "password should match username capitalization"
    print("yingtai mock OK", user, pwd)


def test_uppercase_initials_regen():
    data = load_mock_data()
    data["icris_account"] = {
        "username": "yx8492ytabcd",
        "password": "yx8492ytabcd@",
    }
    data.pop("_icris_session", None)
    user, pwd = derive_icris_credentials(data)
    assert user[0].isupper(), user
    assert user == user[0].upper() + user[1:], user
    assert _s02_password_meets_rules(pwd), pwd
    print("uppercase regen OK", user, pwd)


def test_password_fix_no_uppercase_in_id():
    data = {
        "applicant": {"name_en": "YAO Xiaojia", "id_number": "440514200003184927"},
        "identity_proof": {"id_number": "440514200003184927"},
        "icris_account": {"username": "yx184927ytwxyz", "password": "yx184927ytwxyz@"},
    }
    user, pwd = finalize_s02_icris_credentials(
        data, "yx184927ytwxyz", "yx184927ytwxyz@"
    )
    assert user == "Yx184927ytwxyz"
    assert _s02_password_meets_rules(pwd), f"password needs uppercase fix: {pwd!r}"
    assert pwd == f"{user}@", pwd
    print("password fix OK", user, pwd)


def test_mock_nnc1_account_untouched():
    data = load_mock_data()
    mock = data.get("nnc1_mock_account") or {}
    assert mock.get("username") == "KYAUk13579"
    assert mock.get("password") == "KYAUk13579@"
    print("nnc1_mock_account unchanged OK")


def main():
    test_yingtai_from_mock()
    test_uppercase_initials_regen()
    test_password_fix_no_uppercase_in_id()
    test_mock_nnc1_account_untouched()
    print("ALL OK")


if __name__ == "__main__":
    main()
