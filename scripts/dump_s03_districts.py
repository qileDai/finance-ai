"""走到 s03 本地地址，采全量「郵遞區號」下拉 label+value。"""
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
logger = logging.getLogger("dump_s03_districts")

OUT_PATH = ROOT / "data" / "icris_s03_districts.json"

DISTRICT_KWS = ["郵遞區號", "邮递区号", "區/市", "区/市"]

DUMP_ROW_JS = """async (keywords) => {
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
  const norm = s => (s || '').replace(/[\\s/／:*：－-]/g, '');
  const kws = (keywords || []).map(k => norm(k)).filter(Boolean);
  const titles = [...document.querySelectorAll(
    '.rowTitle, th, label, .ant-form-item-label, .control-label'
  )];
  let native = null;
  let ant = null;
  for (const title of titles) {
    const tt = norm((title.innerText || '').trim());
    if (!kws.some(k => tt.includes(k))) continue;
    const row = title.closest(
      'tr, .ant-form-item, .form-group, fieldset, .content, div'
    );
    if (!row) continue;
    native = row.querySelector('select');
    ant = row.querySelector('.ant-select, [role=combobox]');
    if (native || ant) break;
  }
  if (native && native.options && native.options.length > 1) {
    for (const o of native.options) add(o.textContent, o.value);
    return [...seen.values()];
  }
  if (ant && ant.tagName !== 'SELECT') {
    const trigger = ant.querySelector(
      '.ant-select-selector, .ant-select-arrow'
    ) || ant;
    trigger.scrollIntoView({ block: 'center' });
    trigger.dispatchEvent(new MouseEvent('mousedown', {
      bubbles: true, cancelable: true
    }));
    trigger.click();
    await new Promise(r => setTimeout(r, 400));
  }
  const collect = () => {
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


def _looks_like_district_payload(text: str) -> bool:
    if not text or len(text) < 20:
        return False
    has_island = "香港島" in text or "香港岛" in text
    has_kln = "九龍" in text or "九龙" in text
    has_nt = "新界" in text
    return has_island and (has_kln or has_nt)


def _parse_district_list(payload: object) -> list[dict[str, str]]:
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
                        labels.append(
                            {"label": str(label).strip(), "value": str(value).strip()}
                        )
                if len(labels) >= 20 and any(
                    "-" in (r["label"] or "") and re.search(r"香港島|香港岛|九龍|九龙|新界", r["label"])
                    for r in labels
                ):
                    rows.extend(labels)
                    return
            for item in node:
                walk(item, depth + 1)
            return
        if isinstance(node, dict):
            for key in (
                "districts",
                "districtList",
                "postCode",
                "postcode",
                "list",
                "cn",
                "zh",
                "zhHK",
            ):
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


def _load_old_aliases(path: Path) -> dict[str, list[str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = raw.get("options") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return {}
    out: dict[str, list[str]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        aliases = [
            str(a).strip()
            for a in (item.get("aliases") or [])
            if str(a or "").strip()
        ]
        if label:
            out[label] = aliases
            short = label.split("-", 1)[-1].strip()
            if short and short not in out:
                out[short] = aliases
    return out


def _attach_aliases(
    options: list[dict[str, str]], old_aliases: dict[str, list[str]]
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for row in options:
        label = str(row.get("label") or "").strip()
        value = str(row.get("value") or label).strip() or label
        short = label.split("-", 1)[-1].strip() if "-" in label else label
        aliases: list[str] = []
        for key in (label, short):
            for a in old_aliases.get(key) or []:
                if a and a not in aliases:
                    aliases.append(a)
        if short and short not in aliases:
            aliases.insert(0, short)
        if value and value not in aliases:
            aliases.append(value)
        out.append({"label": label, "value": value, "aliases": aliases})
    return out


async def dump_from_district_row(page) -> list[dict[str, str]]:
    raw = await page.evaluate(DUMP_ROW_JS, DISTRICT_KWS)
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
    old_aliases = _load_old_aliases(OUT_PATH)

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
                if not _looks_like_district_payload(text):
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
                        "parsed_n": len(_parse_district_list(parsed)) if parsed else 0,
                    }
                )
                if parsed:
                    opts = _parse_district_list(parsed)
                    if len(opts) >= 20:
                        logger.info("XHR 郵遞區號字典 %s n=%s", url[:80], len(opts))
                        xhr_hits[-1]["options"] = opts
            except Exception:
                return

        page.on("response", on_response)

        opened = None
        for nav_try in range(1, 4):
            try:
                opened = await bot._navigate_to_registration(page)
            except Exception as exc:
                logger.warning("进入注册门户失败 (%s/3): %s", nav_try, exc)
                opened = None
            if opened:
                page = opened
                try:
                    page.on("response", on_response)
                except Exception:
                    pass
                break
            await asyncio.sleep(2)
        if not opened:
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

        radio_ok = await bot._select_address_type_radio(page, is_hk=True)
        logger.info("已点本地地址 ok=%s", radio_ok)
        try:
            await page.wait_for_function(
                """() => {
                    const norm = s => (s || '').replace(/[\\s/／:*：－-]/g, '');
                    const kws = ['郵遞區號', '邮递区号', '區/市', '区/市', '區市省'];
                    const titles = [...document.querySelectorAll(
                        '.rowTitle, th, label, .ant-form-item-label, .control-label'
                    )];
                    for (const title of titles) {
                        const tt = norm((title.innerText || '').trim());
                        if (!kws.some(k => tt.includes(norm(k)))) continue;
                        const row = title.closest(
                            'tr, .ant-form-item, .content, .row, .form-group'
                        ) || title.parentElement;
                        if (!row) continue;
                        if (row.querySelector(
                            '.ant-select:not(.ant-select-disabled), [role=combobox], select'
                        )) return true;
                    }
                    return false;
                }""",
                timeout=20000,
            )
        except Exception:
            logger.warning("等待郵遞區號下拉超时")
        await page.wait_for_timeout(800)

        options = await dump_from_district_row(page)
        logger.info(
            "页面郵遞區號 n=%s sample=%s",
            len(options),
            [o.get("label") for o in options[:8]],
        )
        xhr_opts: list[dict[str, str]] = []
        for hit in xhr_hits:
            extra = hit.get("options") or []
            if len(extra) > len(xhr_opts):
                xhr_opts = extra
        if len(xhr_opts) > len(options):
            logger.info("采用 XHR 字典 n=%s（页面滚动 n=%s）", len(xhr_opts), len(options))
            options = xhr_opts

        labels = [str(o.get("label") or "") for o in options]
        valid = len(options) >= 20 and any(
            re.search(r"香港島|香港岛|九龍|九龙|新界|香港仔|天水圍|觀塘|观塘", lab)
            for lab in labels
        )
        if not valid:
            logger.error(
                "郵遞區號选项异常 count=%s sample=%s，不覆盖现有文件",
                len(options),
                [o.get("label") for o in options[:8]],
            )
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await close_browser_session(browser, external_cdp=via_cdp)
            return 1

        merged = _attach_aliases(options, old_aliases)
        payload = {
            "source": "icris_s03_hk_district",
            "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "url": "https://www.e-services.cr.gov.hk/ICRIS3EF/system/registration/s03.do",
            "count": len(merged),
            "options": merged,
        }
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("已写入 %s count=%s", OUT_PATH, len(merged))
        logger.info("样例: %s", [o.get("label") for o in merged[:8]])
        logger.info("value 样例: %s", [str(o.get("value") or "") for o in merged[:8]])
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await close_browser_session(browser, external_cdp=via_cdp)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
