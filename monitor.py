from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

# =========================
# User configuration
# =========================
EVENT_URL = "https://cenacolovinciano.vivaticket.it/en/event/cenacolo-vinciano/151991?idt=2547"
TARGET_DATES = ["2026-11-13", "2026-11-14", "2026-11-15"]
PARTY_SIZE = 3
MAX_CONFIRMED_SLOTS = 3          # stop after this many confirmed slots per run
MAX_TIME_SLOTS_TO_PROBE = 12     # avoid hammering the ticket site
REMIND_AFTER_HOURS = 6           # same slot may remind again after this many hours
STATE_PATH = Path(os.getenv("STATE_PATH", "state.json"))
DEBUG_DIR = Path(os.getenv("DEBUG_DIR", "debug"))

BOT_BLOCK_PATTERNS = [
    "access denied", "captcha", "verify you are human", "cloudflare",
    "temporarily blocked", "robot check", "unusual traffic"
]

TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)")


@dataclass(frozen=True)
class Slot:
    date: str
    time: str
    url: str
    capacity: int | None
    evidence: str

    @property
    def key(self) -> str:
        return f"{self.date}|{self.time}"


# ---------- state ----------
def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"last_notified": {}, "errors": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        data.setdefault("last_notified", {})
        data.setdefault("errors", {})
        return data
    except Exception:
        return {"last_notified": {}, "errors": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- Telegram ----------
def telegram_credentials() -> tuple[str, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    return token, chat_id


def send_telegram(message: str) -> None:
    token, chat_id = telegram_credentials()
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(
        endpoint,
        data={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        timeout=20,
    )
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API error: {body}")


def send_error_once(state: dict[str, Any], code: str, detail: str, cooldown_hours: int = 24) -> None:
    now = time.time()
    last = float(state.get("errors", {}).get(code, 0) or 0)
    if now - last < cooldown_hours * 3600:
        return
    msg = (
        "⚠️ <b>Last Supper monitor needs attention</b>\n"
        f"{html.escape(detail)}\n\n"
        "The monitor will keep trying on later runs."
    )
    try:
        send_telegram(msg)
        state.setdefault("errors", {})[code] = now
    finally:
        save_state(state)


# ---------- browser helpers ----------
def accept_cookies(page: Page) -> None:
    patterns = [r"accept all", r"accept", r"allow all", r"accetta tutto", r"accetta", r"consenti", r"ok"]
    for p in patterns:
        try:
            btn = page.get_by_role("button", name=re.compile(p, re.I)).first
            if btn.is_visible(timeout=400):
                btn.click(timeout=1200)
                page.wait_for_timeout(300)
                return
        except Exception:
            pass


def safe_body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


def detect_block(page: Page) -> str | None:
    blob = f"{page.title()}\n{safe_body_text(page)[:12000]}".lower()
    for needle in BOT_BLOCK_PATTERNS:
        if needle in blob:
            return needle
    return None


def save_debug(page: Page, stem: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{stem}.png"), full_page=True)
    except Exception:
        pass
    try:
        (DEBUG_DIR / f"{stem}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    try:
        (DEBUG_DIR / f"{stem}.txt").write_text(safe_body_text(page), encoding="utf-8")
    except Exception:
        pass


def candidate_nodes(page: Page) -> list[dict[str, Any]]:
    js = r"""
    () => {
      const sel = 'a,button,[role="button"],[data-date],[data-day],[data-value],[aria-label],[title]';
      const nodes = [...document.querySelectorAll(sel)];
      return nodes.map((el, idx) => {
        const st = getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        const visible = st.display !== 'none' && st.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        let parentText = '';
        let p = el;
        for (let i = 0; i < 3 && p; i++, p = p.parentElement) {
          parentText += ' ' + (p.innerText || '');
        }
        const attrs = {};
        for (const a of el.attributes || []) attrs[a.name] = a.value;
        return {
          idx,
          tag: el.tagName,
          text: (el.innerText || el.textContent || '').trim(),
          parentText: parentText.trim().slice(0, 1000),
          className: String(el.className || ''),
          href: el.href || '',
          disabled: !!el.disabled,
          ariaDisabled: el.getAttribute('aria-disabled') || '',
          attrs,
          visible,
        };
      });
    }
    """
    return page.evaluate(js)


def month_name(date_iso: str) -> str:
    dt = datetime.strptime(date_iso, "%Y-%m-%d")
    return dt.strftime("%B")


def date_patterns(date_iso: str) -> list[str]:
    dt = datetime.strptime(date_iso, "%Y-%m-%d")
    d = dt.day
    m = dt.month
    mon = dt.strftime("%B")
    mon3 = dt.strftime("%b")
    y = dt.year
    return [
        date_iso,
        f"{d:02d}/{m:02d}/{y}", f"{d}/{m}/{y}",
        f"{m:02d}/{d:02d}/{y}", f"{m}/{d}/{y}",
        f"{d:02d}-{m:02d}-{y}", f"{d}-{m}-{y}",
        f"{mon} {d}", f"{mon} {d}, {y}", f"{d} {mon}", f"{d} {mon} {y}",
        f"{mon3} {d}", f"{d} {mon3}",
    ]


def score_date_node(node: dict[str, Any], date_iso: str) -> int:
    if not node.get("visible"):
        return -999
    attrs = node.get("attrs") or {}
    hay = " ".join([
        str(node.get("text", "")), str(node.get("parentText", "")),
        str(node.get("className", "")), " ".join(f"{k}={v}" for k, v in attrs.items())
    ]).lower()
    cls = str(node.get("className", "")).lower()
    if node.get("disabled") or str(node.get("ariaDisabled", "")).lower() == "true":
        return -500
    if any(x in cls or x in hay for x in ["sold-out", "soldout", "unavailable", "disabled"]):
        return -450

    score = 0
    pats = [p.lower() for p in date_patterns(date_iso)]
    for p in pats:
        if p in hay:
            score = max(score, 100)
    dt = datetime.strptime(date_iso, "%Y-%m-%d")
    text = str(node.get("text", "")).strip()
    parent = str(node.get("parentText", "")).lower()
    if text == str(dt.day) and month_name(date_iso).lower() in parent:
        score = max(score, 55)
    if any(x in cls for x in ["available", "active", "enabled", "green", "selectable"]):
        score += 20
    if "green" in hay:
        score += 10
    return score


def find_date_node(page: Page, date_iso: str) -> dict[str, Any] | None:
    nodes = candidate_nodes(page)
    ranked = sorted(((score_date_node(n, date_iso), n) for n in nodes), key=lambda x: x[0], reverse=True)
    if ranked and ranked[0][0] >= 55:
        return ranked[0][1]
    return None


def click_candidate_by_index(page: Page, idx: int) -> None:
    sel = 'a,button,[role="button"],[data-date],[data-day],[data-value],[aria-label],[title]'
    loc = page.locator(sel).nth(idx)
    loc.scroll_into_view_if_needed(timeout=3000)
    loc.click(timeout=5000)


def click_next_month(page: Page) -> bool:
    selectors = [
        'button[aria-label*="next" i]', 'a[aria-label*="next" i]',
        'button[title*="next" i]', 'a[title*="next" i]',
        '.next', '.calendar-next', '.datepicker-next', '.ui-datepicker-next'
    ]
    for s in selectors:
        try:
            loc = page.locator(s).first
            if loc.is_visible(timeout=250):
                loc.click(timeout=2000)
                page.wait_for_timeout(600)
                return True
        except Exception:
            pass
    # textual fallback
    for pat in [r"next", r"successivo", r"mese successivo", r"›", r"»"]:
        try:
            loc = page.get_by_role("button", name=re.compile(pat, re.I)).first
            if loc.is_visible(timeout=250):
                loc.click(timeout=2000)
                page.wait_for_timeout(600)
                return True
        except Exception:
            pass
    return False


def ensure_target_month_visible(page: Page, target_date: str) -> None:
    # Usually the target calendar is already in the quarterly range. If not, advance conservatively.
    month = month_name(target_date)
    body = safe_body_text(page)
    if month.lower() in body.lower():
        return
    for _ in range(4):
        if not click_next_month(page):
            return
        if month.lower() in safe_body_text(page).lower():
            return


def time_candidates(page: Page) -> list[dict[str, Any]]:
    js = r"""
    () => {
      const sel = 'a,button,[role="button"]';
      const nodes = [...document.querySelectorAll(sel)];
      return nodes.map((el, idx) => {
        const st = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return {
          idx,
          text: (el.innerText || el.textContent || '').trim(),
          className: String(el.className || ''),
          href: el.href || '',
          title: el.getAttribute('title') || '',
          aria: el.getAttribute('aria-label') || '',
          disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
          visible: st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0
        };
      });
    }
    """
    nodes = page.evaluate(js)
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for n in nodes:
        if not n.get("visible") or n.get("disabled"):
            continue
        blob = " ".join([str(n.get("text", "")), str(n.get("title", "")), str(n.get("aria", ""))])
        m = TIME_RE.search(blob)
        if not m:
            continue
        cls = str(n.get("className", "")).lower()
        if any(x in cls for x in ["sold", "unavailable", "disabled"]):
            continue
        hhmm = f"{int(m.group(1)):02d}:{m.group(2)}"
        href = str(n.get("href", ""))
        sig = (hhmm, href or str(n.get("idx")))
        if sig in seen:
            continue
        seen.add(sig)
        n["time"] = hhmm
        out.append(n)
    out.sort(key=lambda x: x["time"])
    return out


def capacity_from_page(page: Page) -> tuple[int | None, str]:
    """Confirm whether a ticket quantity control permits at least PARTY_SIZE.

    We deliberately avoid clicking 'Add to cart' or 'Continue'. Merely inspecting
    quantity selectors/limits does not place an order or intentionally hold tickets.
    """
    # Select dropdowns are the most reliable signal on ticketing pages.
    data = page.evaluate(r"""
    () => {
      const result = {selects: [], numbers: [], maxAttrs: [], body: document.body ? document.body.innerText : ''};
      for (const s of document.querySelectorAll('select')) {
        const opts = [...s.options].map(o => ({value:o.value, text:(o.textContent||'').trim(), disabled:o.disabled}));
        const parent = s.parentElement ? (s.parentElement.innerText || '') : '';
        result.selects.push({name:s.name||'', id:s.id||'', className:String(s.className||''), parent:parent.slice(0,500), opts});
      }
      for (const i of document.querySelectorAll('input[type="number"]')) {
        const parent = i.parentElement ? (i.parentElement.innerText || '') : '';
        result.numbers.push({name:i.name||'', id:i.id||'', min:i.min||'', max:i.max||'', value:i.value||'', parent:parent.slice(0,500)});
      }
      for (const el of document.querySelectorAll('[data-max],[data-availability],[data-available],[data-quantity],[aria-valuemax]')) {
        result.maxAttrs.push({
          tag:el.tagName,
          text:(el.innerText||el.textContent||'').trim().slice(0,300),
          dataMax:el.getAttribute('data-max'),
          dataAvailability:el.getAttribute('data-availability'),
          dataAvailable:el.getAttribute('data-available'),
          dataQuantity:el.getAttribute('data-quantity'),
          ariaValueMax:el.getAttribute('aria-valuemax')
        });
      }
      return result;
    }
    """)

    candidates: list[tuple[int, str]] = []
    ticket_words = re.compile(r"ticket|bigliett|quantity|quantit|qty|intero|full|reduced|ridotto|free|gratuito|visitor|adult", re.I)

    for s in data.get("selects", []):
        nums: list[int] = []
        for o in s.get("opts", []):
            if o.get("disabled"):
                continue
            for raw in (str(o.get("value", "")), str(o.get("text", ""))):
                m = re.fullmatch(r"\s*(\d+)\s*", raw)
                if m:
                    n = int(m.group(1))
                    if 0 <= n <= 10:
                        nums.append(n)
        if nums:
            mx = max(nums)
            context = " ".join([str(s.get("name", "")), str(s.get("id", "")), str(s.get("className", "")), str(s.get("parent", ""))])
            # Quantity selectors on Vivaticket should normally top out at <= 5.
            if 1 <= mx <= 5 and (ticket_words.search(context) or len(set(nums)) >= 3):
                candidates.append((mx, f"quantity dropdown allows 0–{mx}"))

    for i in data.get("numbers", []):
        try:
            mx = int(float(i.get("max") or ""))
        except Exception:
            continue
        context = " ".join([str(i.get("name", "")), str(i.get("id", "")), str(i.get("parent", ""))])
        if 1 <= mx <= 5 and ticket_words.search(context):
            candidates.append((mx, f"quantity input max={mx}"))

    for a in data.get("maxAttrs", []):
        for field in ["dataMax", "dataAvailability", "dataAvailable", "dataQuantity", "ariaValueMax"]:
            raw = a.get(field)
            if raw is None:
                continue
            m = re.fullmatch(r"\s*(\d+)\s*", str(raw))
            if m:
                n = int(m.group(1))
                if 1 <= n <= 5:
                    candidates.append((n, f"{field}={n}"))

    # Structured/textual fallbacks, deliberately avoiding generic "maximum 5 tickets" wording.
    body = str(data.get("body", ""))
    for pat in [
        r"(?i)(\d+)\s+(?:tickets?|places?|seats?)\s+(?:available|remaining|left)",
        r"(?i)(?:available|remaining|left)\s*[:\-]?\s*(\d+)\s+(?:tickets?|places?|seats?)",
        r"(?i)(?:disponibili|rimasti)\s*[:\-]?\s*(\d+)\s+(?:biglietti|posti)",
        r"(?i)(\d+)\s+(?:biglietti|posti)\s+(?:disponibili|rimasti)",
    ]:
        for m in re.finditer(pat, body):
            n = int(m.group(1))
            if 0 <= n <= 40:
                candidates.append((n, "availability text on ticket page"))

    if not candidates:
        # Search embedded HTML/JSON for availability-like fields.
        source = page.content()
        field_re = re.compile(r'(?i)["\'](?:availability|availableQuantity|remaining|residualAvailability|available)["\']\s*[:=]\s*["\']?(\d+)["\']?')
        nums = [int(m.group(1)) for m in field_re.finditer(source)]
        nums = [n for n in nums if 0 <= n <= 40]
        if nums:
            n = max(nums)
            candidates.append((n, "embedded availability field"))

    if not candidates:
        return None, "no reliable quantity control found"

    # A valid ticket-type selector permitting 3 is enough to confirm 3 people can be selected.
    best = max(candidates, key=lambda x: x[0])
    return best


def inspect_slot_url(browser: Browser, base_url: str, href: str, debug: bool, label: str) -> tuple[int | None, str, str]:
    page = browser.new_page(locale="en-US")
    url = urljoin(base_url, href)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1200)
        accept_cookies(page)
        block = detect_block(page)
        if block:
            return None, f"blocked: {block}", page.url
        if debug:
            save_debug(page, label)
        cap, evidence = capacity_from_page(page)
        return cap, evidence, page.url
    finally:
        page.close()


def inspect_slot_by_click(page: Page, idx: int, debug: bool, label: str) -> tuple[int | None, str, str]:
    before = page.url
    sel = 'a,button,[role="button"]'
    loc = page.locator(sel).nth(idx)
    loc.scroll_into_view_if_needed(timeout=3000)
    loc.click(timeout=5000)
    page.wait_for_timeout(1200)
    if debug:
        save_debug(page, label)
    cap, evidence = capacity_from_page(page)
    url = page.url
    # Return to event page when navigation occurred; otherwise caller will reload.
    if page.url != before:
        try:
            page.go_back(wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
    return cap, evidence, url


def scan(browser: Browser, debug: bool = False) -> tuple[list[Slot], list[str]]:
    page = browser.new_page(locale="en-US", viewport={"width": 1440, "height": 1100})
    findings: list[Slot] = []
    diagnostics: list[str] = []
    try:
        page.goto(EVENT_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1800)
        accept_cookies(page)
        block = detect_block(page)
        if block:
            raise RuntimeError(f"Vivaticket returned a bot/access block ({block})")
        if debug:
            save_debug(page, "00_event_page")

        for date_iso in TARGET_DATES:
            if len(findings) >= MAX_CONFIRMED_SLOTS:
                break

            # Reset to a clean calendar for every date.
            if page.url != EVENT_URL:
                page.goto(EVENT_URL, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1200)
                accept_cookies(page)

            ensure_target_month_visible(page, date_iso)
            node = find_date_node(page, date_iso)
            if not node:
                diagnostics.append(f"{date_iso}: no selectable date node found (likely sold out/grey)")
                continue

            try:
                click_candidate_by_index(page, int(node["idx"]))
                page.wait_for_timeout(1000)
            except Exception as e:
                diagnostics.append(f"{date_iso}: found date but click failed: {type(e).__name__}")
                continue

            if debug:
                save_debug(page, f"date_{date_iso}")

            times = time_candidates(page)
            if not times:
                diagnostics.append(f"{date_iso}: date looked selectable but no time-slot links were found")
                continue

            diagnostics.append(f"{date_iso}: found {len(times)} candidate time slots")
            for n, t in enumerate(times[:MAX_TIME_SLOTS_TO_PROBE], start=1):
                if len(findings) >= MAX_CONFIRMED_SLOTS:
                    break
                label = f"slot_{date_iso}_{t['time'].replace(':','')}_{n}"
                cap: int | None
                evidence: str
                slot_url: str
                try:
                    if t.get("href"):
                        cap, evidence, slot_url = inspect_slot_url(browser, page.url, str(t["href"]), debug, label)
                    else:
                        cap, evidence, slot_url = inspect_slot_by_click(page, int(t["idx"]), debug, label)
                        # Dynamic/modal pages are easiest to reset cleanly.
                        page.goto(EVENT_URL, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(900)
                        accept_cookies(page)
                        ensure_target_month_visible(page, date_iso)
                        again = find_date_node(page, date_iso)
                        if again:
                            click_candidate_by_index(page, int(again["idx"]))
                            page.wait_for_timeout(700)
                except Exception as e:
                    diagnostics.append(f"{date_iso} {t['time']}: probe failed: {type(e).__name__}")
                    continue

                diagnostics.append(f"{date_iso} {t['time']}: capacity={cap}; {evidence}")
                if cap is not None and cap >= PARTY_SIZE:
                    findings.append(Slot(date=date_iso, time=t["time"], url=slot_url or EVENT_URL, capacity=cap, evidence=evidence))

        return findings, diagnostics
    finally:
        page.close()


# ---------- notifications ----------
def format_alert(slots: list[Slot]) -> str:
    lines = [
        "🚨 <b>Last Supper tickets found</b>",
        f"Need: <b>{PARTY_SIZE} people in the same time slot</b>",
        "",
    ]
    for s in slots:
        dt = datetime.strptime(s.date, "%Y-%m-%d")
        date_label = dt.strftime("%a %d %b %Y")
        qty = f"≥ {PARTY_SIZE}" if s.capacity is None else f"up to at least {s.capacity} selectable"
        lines.append(f"✅ <b>{html.escape(date_label)} · {html.escape(s.time)}</b>")
        lines.append(f"   Availability check: {html.escape(qty)}")
    lines += ["", f'<a href="{html.escape(EVENT_URL)}">Open official Vivaticket now</a>', "⚠️ Availability can disappear quickly."]
    return "\n".join(lines)


def notify_new_slots(state: dict[str, Any], slots: list[Slot]) -> list[Slot]:
    now = time.time()
    fresh: list[Slot] = []
    last_notified = state.setdefault("last_notified", {})
    for s in slots:
        last = float(last_notified.get(s.key, 0) or 0)
        if now - last >= REMIND_AFTER_HOURS * 3600:
            fresh.append(s)
    if fresh:
        send_telegram(format_alert(fresh))
        for s in fresh:
            last_notified[s.key] = now
        save_state(state)
    return fresh


def telegram_test() -> None:
    send_telegram(
        "✅ <b>Last Supper monitor Telegram test</b>\n"
        "Notifications are configured correctly."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor Cenacolo Vinciano Vivaticket availability")
    parser.add_argument("--debug", action="store_true", help="save screenshots/HTML/text for selector diagnostics")
    parser.add_argument("--telegram-test", action="store_true", help="send a Telegram test message and exit")
    parser.add_argument("--no-telegram", action="store_true", help="scan only; never send Telegram")
    args = parser.parse_args()

    if args.telegram_test:
        telegram_test()
        print("Telegram test sent.")
        return 0

    state = load_state()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                slots, diagnostics = scan(browser, debug=args.debug)
            finally:
                browser.close()
    except PlaywrightTimeoutError as e:
        detail = f"Timed out while opening Vivaticket: {e}"
        print(detail, file=sys.stderr)
        if not args.no_telegram:
            send_error_once(state, "timeout", detail)
        return 2
    except Exception as e:
        detail = f"Monitor error: {type(e).__name__}: {e}"
        print(detail, file=sys.stderr)
        if not args.no_telegram:
            try:
                send_error_once(state, "monitor_error", detail)
            except Exception as te:
                print(f"Could not send Telegram error notice: {te}", file=sys.stderr)
        return 2

    print("\n".join(diagnostics))
    if slots:
        print("Confirmed slots:")
        for s in slots:
            print(f"  {s.date} {s.time} capacity={s.capacity} evidence={s.evidence}")
        if not args.no_telegram:
            fresh = notify_new_slots(state, slots)
            print(f"Telegram notifications sent for {len(fresh)} slot(s).")
    else:
        print("No slot was confirmed to allow 3 tickets in the same time slot.")

    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
