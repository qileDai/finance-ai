"""测试 ICRIS 用户名/密码派生"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.browser.icris_registration import derive_icris_credentials, ensure_icris_password
from src.materials.packager import load_mock_data


def main():
    data = load_mock_data()
    user, pwd = derive_icris_credentials(data)
    assert user.startswith("chantaimanhk26"), user
    assert len(user) == len("chantaimanhk26") + 2, user
    assert pwd == "Chan2026Pass", pwd
    assert len(pwd) >= 10
    assert pwd[0].isupper()
    assert any(c.isdigit() for c in pwd)
    assert any(c.isalpha() for c in pwd)

    assert ensure_icris_password("short") == "Short2026Pass"
    assert ensure_icris_password("ValidPass99") == "ValidPass99"
    print("OK", user, pwd)


if __name__ == "__main__":
    main()
