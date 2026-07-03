"""测试验证码融合与置信度"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.browser.captcha_fusion import assess_confidence, pick_best_captcha

CASES = [
    (
        [("2captcha:merged_min", "O57GV"), ("llm", "O57GV"), ("audio", "O57GV")],
        "O57GV",
        "high",
    ),
    (
        [("2captcha:merged_min", "BEKDW"), ("2captcha:frame0", "BEKDW")],
        "BEKDW",
        "high",
    ),
    (
        [("2captcha:merged_min", "BEKDW"), ("audio", "BEK3W")],
        "BEKDW",
        "medium",
    ),
    (
        [("2captcha:merged_min", "ABCDE"), ("llm", "FGHIJ")],
        "ABCDE",
        "low",
    ),
]

failed = 0
for candidates, result, expected_conf in CASES:
    got = pick_best_captcha(candidates, 5)
    conf = assess_confidence(candidates, got or "", 5)
    ok = got == result and conf == expected_conf
    if not ok:
        failed += 1
        print(f"FAIL got={got!r} conf={conf} expect={result!r}/{expected_conf}")
    else:
        print(f"OK {result!r} conf={conf}")

print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
sys.exit(1 if failed else 0)
