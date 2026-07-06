"""诊断 2Captcha 是否真正可用"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from config.settings import settings
from src.browser.captcha_api import get_balance, prepare_image_variants, solve_2captcha_voted


def main() -> int:
    print("=" * 50)
    print("2Captcha 诊断")
    print("=" * 50)

    mode = settings.captcha_mode
    key = settings.twocaptcha_api_key
    print(f"CAPTCHA_MODE = {mode}")
    print(f"TWOCAPTCHA_API_KEY = {'已配置 (' + str(len(key)) + ' 字符)' if key else '未配置'}")

    try:
        import PIL  # noqa: F401
        print("Pillow = 已安装（可多帧增强）")
    except ImportError:
        print("Pillow = 未安装（将使用原始 GIF 提交 2Captcha）")

    if not key:
        print("\n[FAIL] 请在 .env 配置 TWOCAPTCHA_API_KEY")
        return 1

    print("\n--- 查询余额 ---")
    try:
        bal = get_balance(key)
        print(f"账户余额: ${bal}")
    except Exception as e:
        print(f"[FAIL] 余额查询失败: {e}")
        print("提示: 检查网络/代理，或确认 API Key 正确")
        return 1

    gif = ROOT / "output" / "captcha_latest.gif"
    if not gif.exists():
        print(f"\n[WARN] 无测试图片 {gif}")
        print("请先运行 python main.py --step register 生成验证码截图")
        print("余额正常说明 API Key 有效，2Captcha 可以连通")
        return 0

    raw = gif.read_bytes()
    print(f"\n--- 测试识别 ({gif.name}, {len(raw)} bytes) ---")
    variants = prepare_image_variants(raw)
    print(f"图片变体: {len(variants)} 个 → {[v[0] for v in variants]}")

    results = solve_2captcha_voted(raw, key, max_variants=min(2, settings.twocaptcha_max_variants))
    if results:
        print("\n[OK] 2Captcha 识别成功:")
        for src, code in results:
            print(f"  {src}: {code}")
        return 0

    print("\n[FAIL] 2Captcha 未返回有效结果，请查看上方日志")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
