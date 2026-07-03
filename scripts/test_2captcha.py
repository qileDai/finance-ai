"""测试 2Captcha v2 API"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.INFO)

from config.settings import settings
from src.browser.captcha_api import get_balance, prepare_image_variants, solve_2captcha


def main():
    if not settings.twocaptcha_api_key:
        print("ERROR: 未配置 TWOCAPTCHA_API_KEY")
        return 1

    try:
        bal = get_balance(settings.twocaptcha_api_key)
        print(f"账户余额: ${bal}")
    except Exception as e:
        print(f"余额查询失败: {e}")
        return 1

    gif = ROOT / "output" / "captcha_latest.gif"
    if not gif.exists():
        print("请先运行 debug_captcha.py 生成 output/captcha_latest.gif")
        return 0

    raw = gif.read_bytes()
    variants = prepare_image_variants(raw)
    print(f"生成 {len(variants)} 种图片变体")
    for label, png in variants:
        out = ROOT / "output" / f"captcha_2cap_{label}.png"
        out.write_bytes(png)
        print(f"  {label}: {len(png)} bytes -> {out.name}")

    code = solve_2captcha(raw, settings.twocaptcha_api_key, min_len=5, max_len=5)
    print(f"识别结果: {code!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
