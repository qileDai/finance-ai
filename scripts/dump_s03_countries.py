"""走到 s03 非香港地址，采全量「國家／地區」下拉 label+value。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("dump_s03_countries")

OUT_PATH = ROOT / "data" / "icris_s03_countries.json"

COUNTRY_KWS = [
    "国家",
    "國家",
    "国家/地区",
    "國家/地區",
    "国家／地区",
    "國家／地區",
]

DUMP_ALL_JS = """async () => {
  const skip = /請選擇|请选择|^Select$|^--+/i;
  const seen = new Map();
  const add = (label, value) => {
    const l = String(label || '').trim();
    if (!l || skip.test(l)) return;
    let v = String(value == null || value === '' ? l : value).trim();
    if (!v) v = l;
    const k = l + '\\0' + v;
    if (!seen.has(k)) seen.set(k, {label: l, value: v});
  };
  const reactVal = (el) => {
    const key = Object.keys(el).find(k =>
      k.startsWith('__reactProps$') || k.startsWith('__reactFiber$')
    );
    if (!key) return '';
    let n = el[key];
    for (let i = 0; i < 8 && n; i++) {
      const p = n.memoizedProps || n.pendingProps || n;
      if (p && p.value != null && String(p.value).trim()) return String(p.value);
      n = n.return || n._owner;
    }
    return '';
  };
  const collect = () => {
    for (const sel of document.querySelectorAll('select')) {
      for (const o of sel.options) {
        add(o.textContent, o.value);
      }
    }
    const dd = document.querySelector(
      '.ant-select-dropdown:not(.ant-select-dropdown-hidden)'
    );
    if (!dd) return;
    for (const el of dd.querySelectorAll('.ant-select-item-option')) {
      const label = el.getAttribute('title') || el.innerText || '';
      const value = el.getAttribute('data-value')
        || reactVal(el)
        || el.getAttribute('title')
        || label;
      add(label, value);
    }
  };
  const holder = document.querySelector(
    '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .rc-virtual-list-holder'
  );
  collect();
  if (holder) {
    holder.scrollTop = 0;
    await new Promise(r => setTimeout(r, 60));
    let lastTop = -1, same = 0;
    for (let i = 0; i < 800; i++) {
      collect();
      const step = Math.max(holder.clientHeight || 80, 80);
      const next = Math.min(holder.scrollTop + step, holder.scrollHeight);
      holder.scrollTop = next;
      if (holder.scrollTop === lastTop) {
        same += 1;
        if (same > 10) break;
      } else {
        same = 0;
      }
      lastTop = holder.scrollTop;
      await new Promise(r => setTimeout(r, 35));
    }
  }
  collect();
  return [...seen.values()];
}"""


def _looks_like_country_payload(text: str) -> bool:
    if not text or len(text) < 40:
        return False
    hits = 0
    for token in ("中國", "中国", "Afghanistan", "阿富汗", "Uzbekistan", "阿爾及利亞"):
        if token in text:
            hits += 1
    return hits >= 2


def _parse_country_list(payload: object) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    def walk(node: object, depth: int = 0) -> None:
        if depth > 8 or node is None:
            return
        if isinstance(node, list):
            if node and isinstance(node[0], dict):
                labels = []
                for item in node:
                    if not isinstance(item, dict):
                        continue
                    label = (
                        item.get("label")
                        or item.get("text")
                        or item.get("name")
                        or item.get("cn")
                        or item.get("zh")
                    )
                    value = item.get("value") or item.get("code") or item.get("id") or label
                    if label:
                        labels.append({"label": str(label).strip(), "value": str(value).strip()})
                if len(labels) >= 30:
                    rows.extend(labels)
                    return
            for item in node:
                walk(item, depth + 1)
            return
        if isinstance(node, dict):
            for key in ("cn", "zh", "zhHK", "zh-hk", "countries", "countryList", "list"):
                if key in node:
                    walk(node[key], depth + 1)
            for v in node.values():
                walk(v, depth + 1)

    walk(payload)
    seen: dict[str, dict[str, str]] = {}
    skip = re.compile(r"請選擇|请选择|^Select$|^--+", re.I)
    for row in rows:
        lab = (row.get("label") or "").strip()
        if not lab or skip.search(lab):
            continue
        val = (row.get("value") or lab).strip() or lab
        seen[lab + "\0" + val] = {"label": lab, "value": val}
    return list(seen.values())


async def dump_from_open_dropdown(page) -> list[dict[str, str]]:
    raw = await page.evaluate(DUMP_ALL_JS)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        lab = str(item.get("label") or "").strip()
        val = str(item.get("value") or lab).strip() or lab
        if not lab or re.search(r"請選擇|请选择|^Select$", lab, re.I):
            continue
        key = lab + "\0" + val
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": lab, "value": val})
    return out


async def run() -> int:
    from playwright.async_api import async_playwright

    from config.settings import settings
    from src.browser.icris_registration import IcrisRegistrationBot
    from src.browser.launcher import (
        close_browser_session,
        create_browser_context,
        launch_browser,
    )
    from src.materials.packager import load_mock_data

    bot = IcrisRegistrationBot()
    data = load_mock_data()
    xhr_hits: list[dict] = []

    async with async_playwright() as p:
        browser = await launch_browser(p)
        via_cdp = bool(settings.chrome_use_existing and browser.contexts)
        context = await create_browser_context(browser)
        page = await context.new_page()

        async def on_response(resp) -> None:
            try:
                url = resp.url or ""
                if resp.status != 200:
                    return
                ctype = (resp.headers.get("content-type") or "").lower()
                if "json" not in ctype and "javascript" not in ctype and "text" not in ctype:
                    return
                text = await resp.text()
                if not _looks_like_country_payload(text):
                    return
                parsed = None
                try:
                    parsed = json.loads(text)
                except Exception:
                    parsed = None
                xhr_hits.append(
                    {
                        "url": url[:240],
                        "len": len(text),
                        "parsed_n": len(_parse_country_list(parsed)) if parsed else 0,
                    }
                )
                if parsed:
                    opts = _parse_country_list(parsed)
                    if len(opts) >= 50:
                        logger.info("XHR 国家字典 %s n=%s", url[:80], len(opts))
                        xhr_hits[-1]["options"] = opts
            except Exception:
                return

        page.on("response", on_response)

        page = await bot._navigate_to_registration(page)
        if not page:
            logger.error("未能进入注册门户")
            await close_browser_session(browser, external_cdp=via_cdp)
            return 1
        await bot._ensure_simplified_chinese(page)
        for _ in range(4):
            if await bot._fill_captcha(page) and await bot._accept_terms(page):
                break
            from src.browser.icris_captcha import _reload_captcha

            await _reload_captcha(page)
        await bot._fill_user_profile_step(page, data)
        ok = await bot._wait_for_user_info_form(page, timeout_ms=120000)
        if not ok:
            logger.error("未等到 s03 用户资料表 url=%s", page.url)
            await close_browser_session(browser, external_cdp=via_cdp)
            return 1
        await bot._ensure_traditional_chinese(page)
        await page.wait_for_timeout(1500)

        radio_ok = await bot._select_address_type_radio(page, is_hk=False)
        logger.info("已点非香港地址 ok=%s", radio_ok)
        await bot._wait_country_select_visible(page, timeout_ms=20000)
        await page.wait_for_timeout(800)

        # 打开國家／地區下拉（不选具体项）
        opened = await page.evaluate(
            """(keywords) => {
                const norm = s => (s || '').replace(/[\\s/／:*：－-]/g, '');
                const kws = keywords.map(k => norm(k)).filter(Boolean);
                const titles = [...document.querySelectorAll(
                    '.rowTitle, th, label, .ant-form-item-label, .control-label'
                )];
                const openAnt = (selectEl) => {
                    const trigger = selectEl.querySelector(
                        '.ant-select-selector, .ant-select-arrow'
                    ) || selectEl;
                    trigger.scrollIntoView({ block: 'center' });
                    trigger.dispatchEvent(new MouseEvent('mousedown', {
                        bubbles: true, cancelable: true
                    }));
                    trigger.click();
                };
                for (const title of titles) {
                    const tt = norm((title.innerText || '').trim());
                    if (!kws.some(k => tt.includes(k))) continue;
                    const row = title.closest(
                        'tr, .ant-form-item, .form-group, fieldset, .content, div'
                    );
                    const native = row && row.querySelector('select');
                    if (native && native.options && native.options.length > 1) return 'native';
                    const sel = row && row.querySelector('.ant-select, [role=combobox]');
                    if (!sel || sel.tagName === 'SELECT') continue;
                    openAnt(sel);
                    return 'ant';
                }
                return '';
            }""",
            COUNTRY_KWS,
        )
        logger.info("打开国家下拉: %s", opened)
        await page.wait_for_timeout(600)

        options = await dump_from_open_dropdown(page)
        xhr_opts: list[dict[str, str]] = []
        for hit in xhr_hits:
            extra = hit.get("options") or []
            if len(extra) > len(xhr_opts):
                xhr_opts = extra
        if len(xhr_opts) > len(options):
            logger.info("采用 XHR 字典 n=%s（页面滚动 n=%s）", len(xhr_opts), len(options))
            options = xhr_opts

        payload = {
            "source": "icris_s03_non_hk_country",
            "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "url": "https://www.e-services.cr.gov.hk/ICRIS3EF/system/registration/s03.do",
            "count": len(options),
            "options": options,
        }
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("已写入 %s count=%s", OUT_PATH, len(options))
        sample = [o.get("label") for o in options[:8]]
        logger.info("样例: %s", sample)
        values = [str(o.get("value") or "") for o in options[:12]]
        logger.info("value 样例: %s", values)
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await close_browser_session(browser, external_cdp=via_cdp)

    if len(options) < 50:
        logger.error("国家选项过少 count=%s，采集可能失败", len(options))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
