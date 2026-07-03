"""测试语音验证码数字/字母解析"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.browser.captcha_audio import parse_spoken_captcha

CASES = [
    ("B.E.K.D.W.", "BEKDW"),
    ("B 3 E K 7", "B3EK7"),
    ("B, three, E, K, seven", "B3EK7"),
    ("B \u4e09 E K \u4e03", "B3EK7"),  # 三 七
    ("A P N B K", "APNBK"),
    ("X, S, J, L, E", "XSJLE"),
    ("zero five A B nine", "05AB9"),
    ("0 5 A B 9", "05AB9"),
    ("\u96f6 \u4e94 A B \u4e5d", "05AB9"),  # 零 五 九
    ("O57GV", "O57GV"),
    ("oh five seven G V", "057GV"),  # oh→0 是语音读法，与字母O不同
]

failed = 0
for raw, expected in CASES:
    got = parse_spoken_captcha(raw, 5)
    ok = got == expected
    if not ok:
        failed += 1
    print(f"{'OK' if ok else 'FAIL'} {raw!r} -> {got!r} (expect {expected!r})")

print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
sys.exit(1 if failed else 0)
