#!/usr/bin/env python3
"""LinkedIn daily connection-request automation.

Commands:
  python linkedin_bot.py login                 # sign in by hand once; session is saved
  python linkedin_bot.py run [--dry-run] [--limit N] [--force] [--scheduled]
  python linkedin_bot.py stats

All behaviour is controlled from config.yaml. See README.md.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode

import yaml
from playwright.sync_api import Error as PWError
from playwright.sync_api import ElementHandle, Page
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
# Everything the bot writes lives in DATA_DIR (a mounted volume in Docker).
DATA = Path(os.environ.get("DATA_DIR") or ROOT).resolve()
PROFILE_DIR = DATA / "browser_profile"
DB_PATH = DATA / "history.db"
STATE_PATH = DATA / "state.json"
SETTINGS_PATH = DATA / "settings.json"  # written by the web UI; overrides config.yaml
LOG_DIR = DATA / "logs"
STOP_FILE = DATA / "STOP"  # create this file to make a running bot stop at the next check
BASE = "https://www.linkedin.com"

# ---------------------------------------------------------------------------
# Everything that depends on LinkedIn's page markup lives here. When LinkedIn
# changes its pages, this is the place to fix. (Markup checked Nov 2026: the
# Connect control moved from <a href="/preload/custom-invite/..."> to
# <button aria-label="Invite NAME to connect">, so both are matched.)
# ---------------------------------------------------------------------------
SELECTORS = {
    # Search results use <a href="/preload/search-custom-invite/?vanityName=..">,
    # My Network suggestions use <button aria-label="Invite NAME to connect">.
    "connect": (
        'a[href*="custom-invite"], '
        'a[aria-label*="Invite"][aria-label*="to connect"], '
        'button[aria-label*="Invite"][aria-label*="to connect"], '
        'div[role="button"][aria-label*="Invite"][aria-label*="to connect"]'
    ),
    "more_actions": 'button[aria-label="More actions"]',
    "menu_connect": (
        '[role="menu"] [aria-label*="Invite"][aria-label*="to connect"], '
        '.artdeco-dropdown__content [aria-label*="Invite"][aria-label*="to connect"]'
    ),
    "overlays": '[role="dialog"], [role="alertdialog"], [role="alert"], .artdeco-toast-item',
    "note_textarea": 'textarea[name="message"], textarea#custom-message, [role="dialog"] textarea',
    "dismiss": 'button[aria-label="Dismiss"]',
    # Rows on the Sent page: <a data-view-name="sent-invitations-withdraw-single">.
    "withdraw": '[data-view-name="sent-invitations-withdraw-single"], a[aria-label^="Withdraw invitation"]',
    # The confirm pop-up is a native <dialog> with buttons "Cancel" and "Withdraw"
    # (the Withdraw button's aria-label is "Withdraw invitation sent to NAME").
    "modal_buttons": 'dialog[open] button, [role="dialog"] button, [role="alertdialog"] button',
    # Received invitations: <button aria-label="Accept ... invitation ...">Accept</button>
    # next to "Ignore an invitation to connect from NAME". Matched by text too.
    "invite_actions": 'main button, main [role="button"]',
    "captcha": 'iframe[src*="captcha"], #captcha-internal, iframe[title*="challenge" i]',
}
BUTTON_NAMES = {
    "send_without_note": ["Send without a note", "Send now", "Send"],
    "add_note": ["Add a note"],
    "send_with_note": ["Send invitation", "Send"],
}
TEXT_PATTERNS = {
    "weekly_limit": re.compile(r"weekly invitation limit|reached the weekly|invitation limit", re.I),
    "security": re.compile(r"security verification|quick security check|unusual activity|account (has been |is )?restricted", re.I),
    "email_required": re.compile(r"enter (their|the member'?s) email|verify this member knows you", re.I),
}
BLOCKED_URL_PARTS = ("/checkpoint/", "/authwall", "/login", "/uas/", "/signup")

# Walks up from a button to the largest ancestor that still contains only this
# one action button: that's the person's card. Returns its text + profile link.
CARD_JS = """
(el, sel) => {
  let best = el, node = el;
  for (let i = 0; i < 12 && node.parentElement && node.parentElement !== document.body; i++) {
    const p = node.parentElement;
    if (p.querySelectorAll(sel).length > 1) break;
    best = p; node = p;
  }
  const a = best.querySelector('a[href*="/in/"]');
  return {text: (best.innerText || '').trim().slice(0, 800), href: a ? a.href : ''};
}
"""
ACCEPT_CARD_JS = """
e => {
  let c = e;
  for (let i = 0; i < 10 && c.parentElement; i++) {
    c = c.parentElement;
    if (c.querySelector('a[href*="/in/"]') && (c.innerText || '').split('\\n').filter(Boolean).length >= 3) break;
  }
  const a = c.querySelector('a[href*="/in/"]');
  return {text: (c.innerText || '').trim().slice(0, 600), href: a ? a.href : ''};
}
"""
HANDLE_KEY_JS = "e => (e.getAttribute('aria-label') || '') + '|' + (e.getAttribute('href') || '')"
LOOKS_SENT_JS = """
e => !e.isConnected
  || /pending/i.test(e.innerText || '')
  || (!/to connect/i.test(e.getAttribute('aria-label') || '')
      && !(e.getAttribute('href') || '').includes('custom-invite'))
"""

log = logging.getLogger("linkedin_bot")


class StopRun(Exception):
    """Raised to end the run immediately. pause=True also pauses future runs."""

    def __init__(self, reason: str, pause: bool = False):
        super().__init__(reason)
        self.pause = pause


# ---------------------------------------------------------------------------
# Config, state, storage
# ---------------------------------------------------------------------------
def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: Path) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    if SETTINGS_PATH.exists():
        cfg = _deep_merge(cfg, json.loads(SETTINGS_PATH.read_text() or "{}"))
    if cfg.get("daily_limit_min", 0) > cfg.get("daily_limit_max", 0):
        raise SystemExit("config: daily_limit_min must be <= daily_limit_max")
    if cfg.get("delay_seconds_min", 0) > cfg.get("delay_seconds_max", 0):
        raise SystemExit("config: delay_seconds_min must be <= delay_seconds_max")
    unknown = set(cfg.get("sources", [])) - {"suggested", "search", "profile_urls"}
    if unknown:
        raise SystemExit(f"config: unknown sources {sorted(unknown)}")
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class History:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS invites (
                id INTEGER PRIMARY KEY,
                profile_key TEXT, name TEXT, headline TEXT, url TEXT,
                source TEXT, status TEXT, note_used INTEGER DEFAULT 0,
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_invites_key ON invites(profile_key);
            CREATE INDEX IF NOT EXISTS idx_invites_created ON invites(created_at);
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY, name TEXT, age_days INTEGER, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS accepted (
                id INTEGER PRIMARY KEY, name TEXT, headline TEXT, url TEXT, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, started_at TEXT, ended_at TEXT, trigger TEXT,
                dry_run INTEGER, status TEXT, reason TEXT,
                sent INTEGER DEFAULT 0, withdrawn INTEGER DEFAULT 0, exit_code INTEGER
            );
            """
        )

        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(runs)")]
        if "accepted" not in cols:
            self.conn.execute("ALTER TABLE runs ADD COLUMN accepted INTEGER DEFAULT 0")
            self.conn.commit()

    def record_accept(self, name: str, headline: str, url: str) -> None:
        self.conn.execute(
            "INSERT INTO accepted(name,headline,url,created_at) VALUES (?,?,?,?)",
            (name, headline, url, now_iso()),
        )
        self.conn.commit()

    def start_run(self, trigger: str, dry_run: bool) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(started_at,trigger,dry_run,status) VALUES (?,?,?,'running')",
            (now_iso(), trigger, int(dry_run)),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, reason: str, sent: int, withdrawn: int, code: int,
                   accepted: int = 0) -> None:
        self.conn.execute(
            "UPDATE runs SET ended_at=?, status=?, reason=?, sent=?, withdrawn=?, exit_code=?, accepted=? WHERE id=?",
            (now_iso(), status, reason, sent, withdrawn, code, accepted, run_id),
        )
        self.conn.commit()

    def already_handled(self, key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM invites WHERE profile_key=? AND status IN ('sent','unavailable') LIMIT 1",
            (key,),
        ).fetchone()
        return row is not None

    def record(self, key, name, headline, url, source, status, note_used=False) -> None:
        self.conn.execute(
            "INSERT INTO invites(profile_key,name,headline,url,source,status,note_used,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (key, name, headline, url, source, status, int(note_used), now_iso()),
        )
        self.conn.commit()

    def record_withdrawal(self, name: str, age_days: int) -> None:
        self.conn.execute(
            "INSERT INTO withdrawals(name,age_days,created_at) VALUES (?,?,?)",
            (name, age_days, now_iso()),
        )
        self.conn.commit()

    def sent_since(self, since: datetime) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM invites WHERE status='sent' AND created_at >= ?",
            (since.isoformat(timespec="seconds"),),
        ).fetchone()[0]

    def sent_today(self) -> int:
        return self.sent_since(datetime.combine(date.today(), datetime.min.time()))

    def sent_last_7_days(self) -> int:
        return self.sent_since(datetime.now() - timedelta(days=7))

    def notes_this_month(self) -> int:
        start = date.today().replace(day=1)
        return self.conn.execute(
            "SELECT COUNT(*) FROM invites WHERE status='sent' AND note_used=1 AND created_at >= ?",
            (start.isoformat(),),
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def pause(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def profile_key(href: str, name: str) -> str:
    m = re.search(r"/in/([^/?#]+)", href or "")
    if m:
        return "in/" + m.group(1).lower()
    return "name:" + (name or "").strip().lower()


def name_from_label(label: str) -> str:
    m = re.search(r"Invite (.+?) to connect", label or "")
    return m.group(1).strip() if m else ""


def compact(text: str, limit: int = 240) -> str:
    return " | ".join(line.strip() for line in (text or "").splitlines() if line.strip())[:limit]


def parse_sent_age_days(text: str) -> Optional[int]:
    m = re.search(r"Sent (today|yesterday|(\d+) (minute|hour|day|week|month|year)s? ago)", text or "", re.I)
    if not m:
        return None
    if m.group(1).lower() == "today":
        return 0
    if m.group(1).lower() == "yesterday":
        return 1
    n, unit = int(m.group(2)), m.group(3).lower()
    return {"minute": 0, "hour": 0, "day": n, "week": 7 * n, "month": 30 * n, "year": 365 * n}[unit]


def targets_path(cfg: dict) -> Path:
    name = cfg.get("profile_urls_file") or "targets.txt"
    p = Path(name)
    if p.is_absolute():
        return p
    return DATA / name if (DATA / name).exists() else ROOT / name


def read_targets(path: Path) -> list:
    if not path.exists():
        return []
    urls = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "linkedin.com/in/" in line:
            urls.append(line)
    return urls


def launch_context(p, headless: bool):
    PROFILE_DIR.mkdir(exist_ok=True)
    return p.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
    )


def is_logged_in(page: Page) -> bool:
    return not any(part in page.url for part in BLOCKED_URL_PARTS)


# ---------------------------------------------------------------------------
# The bot
# ---------------------------------------------------------------------------
class Bot:
    def __init__(self, cfg: dict, page: Page, db: History, remaining: int, dry_run: bool):
        self.cfg = cfg
        self.page = page
        self.db = db
        self.remaining = remaining
        self.dry_run = dry_run
        self.failures = 0
        self.blocked_status: Optional[int] = None
        self.sent = 0
        self.withdrawn = 0
        self.accepted = 0
        note = (cfg.get("note") or "").strip()
        self.note = note[:200]
        self.notes_left = max(0, int(cfg.get("note_monthly_cap", 0)) - db.notes_this_month()) if note else 0
        f = cfg.get("filters") or {}
        self.include = [k.lower() for k in f.get("include_keywords") or []]
        self.exclude = [k.lower() for k in f.get("exclude_keywords") or []]
        self.require_mutual = bool(f.get("require_mutual_connections"))
        page.on("response", self._on_response)

    # ---- safety ----------------------------------------------------------
    def _on_response(self, resp) -> None:
        try:
            if resp.status in (429, 999) and "linkedin.com" in resp.url:
                if resp.request.resource_type == "document" or "/voyager/" in resp.url:
                    self.blocked_status = resp.status
        except PWError:
            pass

    def check_guards(self) -> None:
        if STOP_FILE.exists():
            raise StopRun("STOP file found")
        if self.blocked_status:
            raise StopRun(f"LinkedIn answered HTTP {self.blocked_status} (rate limited / blocked)", pause=True)
        url = self.page.url
        if "/checkpoint/" in url:
            raise StopRun("LinkedIn security checkpoint (verify in the browser view)", pause=True)
        if any(part in url for part in BLOCKED_URL_PARTS):
            # Signed out is not a LinkedIn warning: flag it for the UI, don't pause runs.
            state = load_state()
            state["logged_in"] = {"ok": False, "checked_at": now_iso()}
            save_state(state)
            raise StopRun("signed out of LinkedIn; use 'Sign in to LinkedIn' again")
        if self.page.query_selector(SELECTORS["captcha"]):
            raise StopRun("CAPTCHA / challenge shown", pause=True)
        for el in self.page.query_selector_all(SELECTORS["overlays"]):
            try:
                text = el.inner_text()
            except PWError:
                continue
            if TEXT_PATTERNS["weekly_limit"].search(text):
                raise StopRun("LinkedIn says the weekly invitation limit is reached", pause=True)
            if TEXT_PATTERNS["security"].search(text):
                raise StopRun("LinkedIn showed a security / restriction notice", pause=True)

    def goto(self, url: str) -> None:
        for attempt in (1, 2):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
                break
            except PWTimeout:
                self.check_guards()
                if attempt == 2:
                    raise StopRun(f"page did not load twice in a row: {url}")
                log.warning("  page load timed out, retrying in a moment")
                pause(15, 30)
        pause(2.5, 4.5)
        self.check_guards()

    def wait_for_connect_buttons(self) -> bool:
        """LinkedIn renders names first and action buttons much later (~15s on
        search results), so wait for at least one Connect control to exist."""
        timeout = float(self.cfg.get("page_load_timeout_seconds", 40))
        try:
            self.page.wait_for_selector(SELECTORS["connect"], state="attached", timeout=timeout * 1000)
        except PWTimeout:
            log.info("  no Connect buttons appeared within %.0fs", timeout)
            return False
        pause(1.5, 3.0)  # let the rest of the list finish rendering
        self.check_guards()
        return True

    # ---- UI helpers ------------------------------------------------------
    def find_button(self, names: list):
        dialogs = self.page.get_by_role("dialog")
        scopes = [dialogs.last, self.page] if dialogs.count() else [self.page]
        for scope in scopes:
            for n in names:
                loc = scope.get_by_role("button", name=n, exact=True)
                try:
                    if loc.count() and loc.first.is_visible():
                        return loc.first
                except PWError:
                    continue
        return None

    def dialog_text(self) -> str:
        texts = []
        for el in self.page.query_selector_all('[role="dialog"]'):
            try:
                texts.append(el.inner_text())
            except PWError:
                pass
        return "\n".join(texts)

    def dismiss(self) -> None:
        btn = self.page.query_selector(SELECTORS["dismiss"])
        try:
            if btn and btn.is_visible():
                btn.click()
            else:
                self.page.keyboard.press("Escape")
        except PWError:
            self.page.keyboard.press("Escape")
        pause(0.8, 1.5)

    @staticmethod
    def _has_word(text: str, kw: str) -> bool:
        # Whole-word/phrase match, so "sre" doesn't match inside other words.
        return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text) is not None

    def passes_filters(self, text: str):
        t = (text or "").lower()
        for kw in self.exclude:
            if self._has_word(t, kw):
                return False, f"excluded keyword '{kw}'"
        if self.include and not any(self._has_word(t, kw) for kw in self.include):
            return False, "no include keyword"
        if self.require_mutual and "mutual" not in t:
            return False, "no mutual connections"
        return True, ""

    # ---- sending one invite ---------------------------------------------
    def _open_invite(self, handle: ElementHandle) -> None:
        handle.scroll_into_view_if_needed(timeout=5000)
        pause(0.8, 2.0)
        handle.click(timeout=5000)
        pause(1.5, 3.0)
        self.check_guards()

    def _try_send_with_note(self, first_name: str) -> bool:
        btn = self.find_button(BUTTON_NAMES["add_note"])
        if not btn:
            return False
        btn.click()
        box = self.page.locator(SELECTORS["note_textarea"]).first
        try:
            box.wait_for(state="visible", timeout=5000)
        except PWTimeout:
            log.info("  note box did not appear (note limit / Premium prompt?). Notes off for this run.")
            self.notes_left = 0
            return False
        msg = self.note.replace("{first_name}", first_name or "there")[:200]
        box.fill("")
        box.press_sequentially(msg, delay=random.randint(30, 80))
        pause(0.8, 1.6)
        send = self.find_button(BUTTON_NAMES["send_with_note"])
        if not send:
            return False
        send.click()
        return True

    def invite(self, handle: ElementHandle, name: str, key: str, info: dict, source: str) -> None:
        first = name.split()[0] if name else ""
        headline = compact(info.get("text", ""))
        url = info.get("href", "")
        if self.dry_run:
            log.info("[dry-run] would invite %s  (%s)", name or "?", headline[:120])
            self.remaining -= 1
            return

        self._open_invite(handle)
        if TEXT_PATTERNS["email_required"].search(self.dialog_text()):
            log.info("  %s requires their email address. Skipping.", name)
            self.dismiss()
            self.db.record(key, name, headline, url, source, "unavailable")
            return

        sent, note_used = False, False
        if self.note and self.notes_left > 0:
            note_used = sent = self._try_send_with_note(first)
            if not sent:
                # The note path failed; reset the dialog and fall back to no note.
                self.dismiss()
                if handle.evaluate("e => e.isConnected"):
                    self._open_invite(handle)
        if not sent:
            btn = self.find_button(BUTTON_NAMES["send_without_note"])
            if btn:
                btn.click()
                sent = True
            else:
                # Some pages (e.g. suggestions) send immediately with no dialog.
                try:
                    sent = bool(handle.evaluate(LOOKS_SENT_JS))
                except PWError:
                    sent = True
        pause(1.5, 3.0)
        self.check_guards()

        if sent:
            if note_used:
                self.notes_left -= 1
            self.db.record(key, name, headline, url, source, "sent", note_used)
            self.remaining -= 1
            self.sent += 1
            self.failures = 0
            log.info("Sent #%d to %s%s  (%s)", self.sent, name or "?", " +note" if note_used else "", headline[:120])
        else:
            self.dismiss()
            self.db.record(key, name, headline, url, source, "failed")
            self.failures += 1
            log.warning("  could not send to %s (failure %d)", name or "?", self.failures)
            if self.failures >= int(self.cfg.get("max_consecutive_failures", 3)):
                raise StopRun("too many consecutive failures; selectors may be out of date")
        pause(float(self.cfg.get("delay_seconds_min", 25)), float(self.cfg.get("delay_seconds_max", 70)))

    # ---- list-style sources (search / suggested) -------------------------
    def harvest(self, source: str, load_more: Callable[[], bool]) -> None:
        seen = set()
        list_url = self.page.url
        while self.remaining > 0:
            self.check_guards()
            candidate = None
            for h in self.page.query_selector_all(SELECTORS["connect"]):
                try:
                    if not h.is_visible():
                        continue
                    k = h.evaluate(HANDLE_KEY_JS)
                except PWError:
                    continue
                if k in seen:
                    continue
                seen.add(k)
                candidate = h
                break
            if candidate is None:
                if not load_more():
                    return
                continue
            load_more.idle = 0  # type: ignore[attr-defined]
            try:
                info = candidate.evaluate(CARD_JS, SELECTORS["connect"])
                label = candidate.get_attribute("aria-label") or ""
            except PWError:
                continue
            name = name_from_label(label) or (info["text"].splitlines() or [""])[0].strip()
            key = profile_key(info.get("href", ""), name)
            if self.db.already_handled(key):
                log.debug("skip %s: already invited", name)
                continue
            ok, why = self.passes_filters(info.get("text", ""))
            if not ok:
                log.info("skip %s: %s", name or "?", why)
                continue
            try:
                self.invite(candidate, name, key, info, source)
            except StopRun:
                raise
            except PWError as e:
                self.failures += 1
                log.warning("  error inviting %s: %s", name, str(e).splitlines()[0])
                self.dismiss()
                if self.failures >= int(self.cfg.get("max_consecutive_failures", 3)):
                    raise StopRun("too many consecutive failures; selectors may be out of date")
            # Search-result Connect links can navigate to an invite page; come back.
            if self.page.url.split("#")[0] != list_url.split("#")[0] and "custom-invite" in self.page.url:
                log.debug("returning to the list page")
                self.goto(list_url)
                self.wait_for_connect_buttons()

    def run_search(self) -> None:
        s = self.cfg.get("search") or {}
        raw = s.get("keywords") or []
        queries = [q.strip() for q in ([raw] if isinstance(raw, str) else raw) if q and q.strip()]
        if not queries:
            log.info("search: no keywords configured, skipping")
            return
        random.shuffle(queries)  # spread invites across all the queries over time
        for q in queries:
            if self.remaining <= 0:
                return
            self._search_one(q, s)

    def _search_one(self, keywords: str, s: dict) -> None:
        max_pages = int(s.get("max_pages", 5))
        state = {"page": 1}

        def url_for(n: int) -> str:
            params = [("keywords", keywords), ("origin", "FACETED_SEARCH")]
            if s.get("network"):
                params.append(("network", json.dumps(list(s["network"]))))
            if s.get("geo_urn"):
                params.append(("geoUrn", json.dumps([str(g) for g in s["geo_urn"]])))
            if n > 1:
                params.append(("page", str(n)))
            return f"{BASE}/search/results/people/?{urlencode(params)}"

        def load_more() -> bool:
            state["page"] += 1
            if state["page"] > max_pages:
                return False
            log.info("search '%s': page %d", keywords, state["page"])
            self.goto(url_for(state["page"]))
            self.wait_for_connect_buttons()
            return True

        log.info("search: '%s' (up to %d pages)", keywords, max_pages)
        self.goto(url_for(1))
        self.wait_for_connect_buttons()
        self.harvest("search", load_more)

    def run_suggested(self) -> None:
        # "People you may know" sits below invitations, puzzles, etc. and only
        # loads as you scroll, so keep scrolling (patiently) until cards appear.
        max_idle = int(self.cfg.get("suggested_max_idle_scrolls", 10))

        def load_more() -> bool:
            load_more.idle += 1  # type: ignore[attr-defined]
            if load_more.idle > max_idle:  # type: ignore[attr-defined]
                log.info("suggested: no more new suggestions after %d scrolls", max_idle)
                return False
            self.page.mouse.move(800, 500)
            self.page.mouse.wheel(0, random.randint(1000, 1600))
            pause(2.5, 4.0)
            self.check_guards()
            return True

        load_more.idle = 0  # type: ignore[attr-defined]
        log.info("suggested: opening My Network")
        self.goto(f"{BASE}/mynetwork/grow/")
        pause(3, 5)
        self.harvest("suggested", load_more)

    # ---- specific profiles -------------------------------------------------
    def _profile_connect(self, name: str) -> Optional[ElementHandle]:
        def matches(h: ElementHandle) -> bool:
            try:
                if not h.is_visible():
                    return False
                label = (h.get_attribute("aria-label") or "").lower()
            except PWError:
                return False
            return (name.lower() in label) if name else True

        main = self.page.query_selector("main") or self.page
        for h in main.query_selector_all(SELECTORS["connect"]):
            if matches(h):
                return h
        more = next((b for b in main.query_selector_all(SELECTORS["more_actions"]) if b.is_visible()), None)
        if more:
            more.click()
            pause(1, 2)
            for h in self.page.query_selector_all(SELECTORS["menu_connect"]):
                if matches(h):
                    return h
            self.page.keyboard.press("Escape")
        return None

    def run_profile_urls(self) -> None:
        path = targets_path(self.cfg)
        urls = read_targets(path)
        log.info("profile_urls: %d profiles in %s", len(urls), path.name)
        for url in urls:
            if self.remaining <= 0:
                return
            key = profile_key(url, "")
            if self.db.already_handled(key):
                continue
            self.goto(url)
            try:
                name = self.page.locator("main h1").first.inner_text(timeout=8000).strip()
            except PWError:
                name = ""
            handle = self._profile_connect(name)
            if handle is None:
                log.info("skip %s: no Connect option (already connected, pending, or follow-only)", name or url)
                if not self.dry_run:
                    self.db.record(key, name, "", url, "profile_urls", "unavailable")
                continue
            try:
                self.invite(handle, name, key, {"text": name, "href": url}, "profile_urls")
            except StopRun:
                raise
            except PWError as e:
                log.warning("  error inviting %s: %s", name, str(e).splitlines()[0])
                self.dismiss()

    # ---- housekeeping --------------------------------------------------------
    def _modal_button(self, text: str) -> Optional[ElementHandle]:
        for b in self.page.query_selector_all(SELECTORS["modal_buttons"]):
            try:
                if b.is_visible() and (b.inner_text() or "").strip().lower() == text.lower():
                    return b
            except PWError:
                continue
        return None

    def _withdraw_labels(self) -> list:
        out = []
        for h in self.page.query_selector_all(SELECTORS["withdraw"]):
            try:
                out.append(h.get_attribute("aria-label") or "")
            except PWError:
                pass
        return out

    def _withdraw_one(self, handle: ElementHandle, label: str) -> bool:
        handle.scroll_into_view_if_needed(timeout=5000)
        pause(0.8, 1.5)
        handle.click(timeout=10000)
        confirm = None
        for _ in range(30):  # wait up to 15s for the confirmation pop-up (LinkedIn can be slow)
            confirm = self._modal_button("Withdraw")
            if confirm:
                break
            time.sleep(0.5)
        if not confirm:
            log.warning("  confirmation pop-up did not appear")
            self._close_modal()
            return False
        pause(0.6, 1.4)
        confirm.click(timeout=10000)
        for _ in range(16):  # wait for the pop-up to close
            if not self._modal_button("Withdraw"):
                break
            time.sleep(0.5)
        else:
            log.warning("  confirmation pop-up did not close")
            self._close_modal()
            return False
        pause(1.5, 2.5)
        self.check_guards()
        for _ in range(3):  # confirm the row is gone from the list
            if label not in self._withdraw_labels():
                return True
            time.sleep(1.5)
        log.warning("  invite still listed after confirming")
        return False

    def _close_modal(self) -> None:
        cancel = self._modal_button("Cancel")
        try:
            if cancel:
                cancel.click(timeout=5000)
            else:
                self.page.keyboard.press("Escape")
        except PWError:
            self.page.keyboard.press("Escape")
        pause(0.8, 1.5)

    # ---- incoming invitations ----------------------------------------------
    def _accept_buttons(self) -> list:
        out = []
        for b in self.page.query_selector_all(SELECTORS["invite_actions"]):
            try:
                if not b.is_visible():
                    continue
                text = (b.inner_text() or "").strip().lower()
                label = (b.get_attribute("aria-label") or "").lower()
            except PWError:
                continue
            if text == "accept" or (label.startswith("accept") and "invit" in label):
                out.append(b)
        return out

    def accept_all(self) -> None:
        limit = int(self.cfg.get("accept_max_per_run", 50))
        log.info("accept: opening received invitations")
        self.goto(f"{BASE}/mynetwork/invitation-manager/received/")
        timeout = float(self.cfg.get("page_load_timeout_seconds", 40))
        deadline = time.time() + min(timeout, 25)
        while time.time() < deadline and not self._accept_buttons():
            if "no new invitations" in (self.page.inner_text("main") or "").lower():
                break
            time.sleep(1.5)
        done, fails, idle, seen = 0, 0, 0, set()
        while done < limit:
            self.check_guards()
            btn = None
            for b in self._accept_buttons():
                try:
                    key = (b.get_attribute("aria-label") or "") + "|" + b.evaluate(ACCEPT_CARD_JS)["href"]
                except PWError:
                    continue
                if key not in seen:
                    btn, seen_key = b, key
                    break
            if btn is None:
                idle += 1
                if idle > 3:
                    break
                self.page.mouse.move(640, 500)
                self.page.mouse.wheel(0, 1500)
                pause(2, 3.5)
                continue
            idle = 0
            seen.add(seen_key)
            try:
                info = btn.evaluate(ACCEPT_CARD_JS)
                label = btn.get_attribute("aria-label") or ""
            except PWError:
                continue
            m = re.search(r"from (.+)$", label) or re.search(r"Accept (.+?)['’]s invitation", label)
            name = m.group(1).strip() if m else (info["text"].splitlines() or ["?"])[0].strip()
            headline = compact(info.get("text", ""))
            if self.dry_run:
                log.info("[dry-run] would accept %s  (%s)", name, headline[:120])
                done += 1
                continue
            try:
                btn.scroll_into_view_if_needed(timeout=5000)
                pause(0.8, 1.8)
                btn.click(timeout=10000)
                ok = False
                for _ in range(16):  # wait for the button to go away / change
                    time.sleep(0.5)
                    try:
                        if not btn.evaluate("e => e.isConnected && /^accept$/i.test((e.innerText||'').trim())"):
                            ok = True
                            break
                    except PWError:
                        ok = True
                        break
            except PWError as e:
                log.warning("  error accepting %s: %s", name, str(e).splitlines()[0])
                ok = False
            pause(1.0, 2.0)
            if self.page.query_selector('[role="dialog"]'):
                self.dismiss()  # e.g. a "say hello" prompt after accepting
            self.check_guards()
            if ok:
                self.db.record_accept(name, headline, info.get("href", ""))
                self.accepted += 1
                done += 1
                fails = 0
                log.info("Accepted %s  (%s)", name, headline[:120])
            else:
                fails += 1
                log.warning("  could not accept %s", name)
                if fails >= 2:
                    log.warning("accept: stopping after 2 failures in a row")
                    break
            pause(4, 10)
        log.info("accept: %d invitation(s) %s", done, "would be accepted" if self.dry_run else "accepted")

    def withdraw_old(self) -> None:
        days = int(self.cfg.get("withdraw_pending_after_days") or 0)
        if days <= 0:
            return
        limit = int(self.cfg.get("withdraw_max_per_run", 20))
        log.info("withdraw: checking sent invites older than %d days", days)
        self.goto(f"{BASE}/mynetwork/invitation-manager/sent/")
        for _ in range(8):  # load older invites at the bottom of the list
            self.page.mouse.move(640, 500)
            self.page.mouse.wheel(0, 3000)
            pause(1.0, 2.0)
        done, fails, tried = 0, 0, set()
        while done < limit:
            target = None
            # Re-query every time: the list re-renders after each withdrawal.
            for h in reversed(self.page.query_selector_all(SELECTORS["withdraw"])):  # oldest first
                try:
                    label = h.get_attribute("aria-label") or ""
                    if not label or label in tried:
                        continue
                    info = h.evaluate(CARD_JS, SELECTORS["withdraw"])
                except PWError:
                    continue
                tried.add(label)
                age = parse_sent_age_days(info.get("text", ""))
                if age is None or age < days:
                    continue
                target = (h, label, age)
                break
            if target is None:
                break
            h, label, age = target
            who = re.sub(r"^Withdraw (invitation sent to )?", "", label).strip()
            if self.dry_run:
                log.info("[dry-run] would withdraw invite to %s (%d days old)", who, age)
                done += 1
                continue
            if self._withdraw_one(h, label):
                self.db.record_withdrawal(who, age)
                self.withdrawn += 1
                done += 1
                fails = 0
                log.info("Withdrew invite to %s (%d days old)", who, age)
            else:
                fails += 1
                log.warning("  could not withdraw invite to %s", who)
                if fails >= 2:
                    log.warning("withdraw: stopping after 2 failures in a row")
                    break
            pause(8, 20)  # unhurried: rapid withdrawals made LinkedIn slow down
        log.info("withdraw: %d invite(s) %s", done, "would be withdrawn" if self.dry_run else "withdrawn")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.FileHandler(LOG_DIR / f"{date.today().isoformat()}.log")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)


def daily_budget(cfg: dict, db: History, state: dict) -> int:
    today = date.today().isoformat()
    target_state = state.get("target") or {}
    if target_state.get("date") == today:
        target = int(target_state["value"])
    else:
        target = random.randint(int(cfg.get("daily_limit_min", 15)), int(cfg.get("daily_limit_max", 25)))
        w = cfg.get("warmup") or {}
        if w.get("enabled"):
            start = w.get("start_date") or state.get("warmup_start")
            if not start:
                start = today
                state["warmup_start"] = start
            weeks = max(0, (date.today() - date.fromisoformat(str(start))).days // 7)
            warm = int(w.get("start_per_day", 5)) + weeks * int(w.get("increase_per_week", 5))
            if warm < target:
                log.info("warm-up week %d: capping today at %d", weeks + 1, warm)
                target = warm
        state["target"] = {"date": today, "value": target}
        save_state(state)
    sent_today, sent_week = db.sent_today(), db.sent_last_7_days()
    weekly_left = int(cfg.get("weekly_cap", 100)) - sent_week
    remaining = max(0, min(target - sent_today, weekly_left))
    log.info(
        "today's target %d, sent today %d, sent last 7 days %d/%d  ->  %d to send",
        target, sent_today, sent_week, int(cfg.get("weekly_cap", 100)), remaining,
    )
    return remaining


def cmd_login(cfg: dict, args) -> int:
    with sync_playwright() as p:
        ctx = launch_context(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/feed/", wait_until="domcontentloaded", timeout=60000)
        pause(2, 3)
        if is_logged_in(page):
            ok = True
        elif args.wait:
            # Non-interactive (used by the web UI): wait until the user has signed in.
            page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=60000)
            log.info("waiting up to %d s for you to sign in in the browser view", args.wait)
            deadline = time.time() + args.wait
            ok = False
            while time.time() < deadline and not ok:
                time.sleep(2)
                # Check every tab: Chromium may restore old tabs, or sign-in may open a new one.
                for tab in list(ctx.pages):
                    try:
                        url = tab.url
                    except PWError:
                        continue
                    if "linkedin.com/" in url and "/feed" in url:
                        pause(3, 5)  # let LinkedIn finish setting cookies
                        ok = True
                        break
        else:
            page.goto(f"{BASE}/login")
            print("\nSign in to LinkedIn in the browser window (including any 2-step verification).")
            input("When you can see your feed, come back here and press Enter... ")
            page.goto(f"{BASE}/feed/", wait_until="domcontentloaded")
            pause(2, 3)
            ok = is_logged_in(page)
        state = load_state()
        state["logged_in"] = {"ok": ok, "checked_at": now_iso()}
        save_state(state)
        ctx.close()
    print("Logged in. Session saved to browser_profile/." if ok else "Still not logged in. Try again.")
    return 0 if ok else 1


def cmd_export_session(cfg: dict, args) -> int:
    """Save this profile's LinkedIn cookies to a file (to move a session into Docker)."""
    with sync_playwright() as p:
        ctx = launch_context(p, headless=True)
        cookies = [c for c in ctx.cookies() if c.get("domain", "").lstrip(".").endswith("linkedin.com")]
        ctx.close()
    if not any(c["name"] == "li_at" for c in cookies):
        print("This profile is not signed in to LinkedIn (no session cookie found).")
        return 1
    out = Path(args.file)
    out.write_text(json.dumps(cookies))
    os.chmod(out, 0o600)
    print(f"Saved {len(cookies)} LinkedIn cookies to {out}. Treat this file like a password; delete it after importing.")
    return 0


def cmd_import_session(cfg: dict, args) -> int:
    """Load LinkedIn cookies saved by export-session into this profile, then check sign-in."""
    path = Path(args.file)
    cookies = json.loads(path.read_text())
    with sync_playwright() as p:
        ctx = launch_context(p, headless=False)
        ctx.add_cookies(cookies)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/feed/", wait_until="domcontentloaded", timeout=60000)
        pause(3, 5)
        ok = is_logged_in(page)
        state = load_state()
        state["logged_in"] = {"ok": ok, "checked_at": now_iso()}
        save_state(state)
        ctx.close()
    if ok and args.delete:
        path.unlink()
    print("Signed in. Session saved in the profile." if ok else "Imported, but LinkedIn did not accept the session.")
    return 0 if ok else 1


def cmd_run(cfg: dict, args) -> int:
    dry_run = bool(args.dry_run or cfg.get("dry_run"))
    db = History(DB_PATH)
    run_id = db.start_run(args.trigger, dry_run)
    result = {"status": "ok", "reason": "", "sent": 0, "withdrawn": 0, "accepted": 0, "code": 0}
    try:
        _run(cfg, args, db, dry_run, result)
    except BaseException as e:  # record crashes too, then re-raise
        first = (str(e).splitlines() or [""])[0]
        result.update(status="error", reason=f"{type(e).__name__}: {first}"[:200], code=result["code"] or 1)
        raise
    finally:
        db.finish_run(run_id, result["status"], result["reason"], result["sent"], result["withdrawn"],
                      result["code"], result["accepted"])
    return result["code"]


def _skip(result: dict, reason: str) -> None:
    log.info(reason)
    result.update(status="skipped", reason=reason)


def _run(cfg: dict, args, db: History, dry_run: bool, result: dict) -> None:
    state = load_state()
    if not args.force:
        paused = state.get("paused_until")
        if paused and date.today().isoformat() < paused:
            return _skip(result, f"paused until {paused} after a LinkedIn warning")
        if not args.ignore_hours:
            lo, hi = (cfg.get("active_hours") or [0, 24])[:2]
            if not (int(lo) <= datetime.now().hour < int(hi)):
                return _skip(result, f"outside active hours {lo}-{hi}")
    if STOP_FILE.exists():
        return _skip(result, "STOP file present; delete it to allow runs")
    if args.scheduled and cfg.get("start_jitter_minutes"):
        wait = random.uniform(0, float(cfg["start_jitter_minutes"]) * 60)
        log.info("scheduled run: waiting %.0f min before starting", wait / 60)
        time.sleep(wait)

    accept_only = bool(getattr(args, "accept_only", False))
    remaining = 0 if accept_only else daily_budget(cfg, db, state)
    if accept_only:
        pass
    elif args.limit is not None:
        remaining = args.limit if dry_run else min(remaining, args.limit)
    elif dry_run and remaining <= 0:
        remaining = 10  # let a dry run show matches even if today's quota is used
    wants_accept = accept_only or bool(cfg.get("accept_invites_on_run"))
    if remaining <= 0 and not dry_run and not wants_accept:
        return _skip(result, "today's quota already sent")
    if dry_run:
        log.info("DRY RUN: nothing will be clicked")

    with sync_playwright() as p:
        ctx = launch_context(p, headless=bool(cfg.get("headless", False)))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        bot = Bot(cfg, page, db, remaining, dry_run)
        try:
            page.goto(f"{BASE}/feed/", wait_until="domcontentloaded", timeout=60000)
            pause(2, 4)
            if not is_logged_in(page):
                log.error("Not logged in. Use 'Sign in to LinkedIn' in the UI (or: python linkedin_bot.py login)")
                state["logged_in"] = {"ok": False, "checked_at": now_iso()}
                save_state(state)
                result.update(status="error", reason="not logged in to LinkedIn", code=2)
                return
            state["logged_in"] = {"ok": True, "checked_at": now_iso()}
            save_state(state)
            if wants_accept:
                try:
                    bot.accept_all()
                except StopRun:
                    raise
                except PWError as e:
                    log.warning("accept step failed, continuing: %s", str(e).splitlines()[0])
            if accept_only:
                return
            try:
                bot.withdraw_old()
            except StopRun:
                raise
            except PWError as e:
                log.warning("withdraw step failed, continuing: %s", str(e).splitlines()[0])
            runners = {"search": bot.run_search, "suggested": bot.run_suggested, "profile_urls": bot.run_profile_urls}
            for src in cfg.get("sources") or ["suggested"]:
                if bot.remaining <= 0:
                    break
                runners[src]()
            if bot.remaining > 0 and not dry_run and not accept_only:
                result["reason"] = f"ran out of matching people ({bot.remaining} short of target)"
        except StopRun as e:
            log.warning("STOPPED: %s", e)
            result.update(status="stopped", reason=str(e), code=3)
            if e.pause and not dry_run:
                days = int(cfg.get("pause_days_after_warning", 3))
                state["paused_until"] = (date.today() + timedelta(days=days)).isoformat()
                save_state(state)
                log.warning("pausing runs until %s", state["paused_until"])
        except KeyboardInterrupt:
            log.warning("interrupted")
            result.update(status="stopped", reason="stopped by you", code=130)
        except PWError as e:
            log.error("browser error, stopping: %s", str(e).splitlines()[0])
            result.update(status="error", reason=str(e).splitlines()[0][:300], code=4)
        finally:
            would = remaining - bot.remaining
            result["sent"] = would if dry_run else bot.sent
            result["withdrawn"] = bot.withdrawn
            result["accepted"] = bot.accepted
            verb = "would send" if dry_run else "sent"
            log.info("done: %s %d invite(s), withdrew %d, accepted %d this run",
                     verb, result["sent"], bot.withdrawn, bot.accepted)
            try:
                ctx.close()
            except PWError:
                pass


def cmd_stats(cfg: dict, args) -> int:
    db = History(DB_PATH)
    print("Invites sent per day (last 14 days):")
    for i in range(13, -1, -1):
        d = date.today() - timedelta(days=i)
        n = db.conn.execute(
            "SELECT COUNT(*) FROM invites WHERE status='sent' AND created_at LIKE ?", (d.isoformat() + "%",)
        ).fetchone()[0]
        print(f"  {d.isoformat()}  {'#' * n} {n}")
    print(f"\nLast 7 days: {db.sent_last_7_days()} / weekly cap {cfg.get('weekly_cap', 100)}")
    print(f"Notes used this month: {db.notes_this_month()} / {cfg.get('note_monthly_cap', 0)}")
    total = db.conn.execute("SELECT COUNT(*) FROM invites WHERE status='sent'").fetchone()[0]
    wd = db.conn.execute("SELECT COUNT(*) FROM withdrawals").fetchone()[0]
    print(f"All-time sent: {total}   withdrawn: {wd}")
    rows = db.conn.execute(
        "SELECT source, COUNT(*) FROM invites WHERE status='sent' GROUP BY source"
    ).fetchall()
    if rows:
        print("By source: " + ", ".join(f"{s} {n}" for s, n in rows))
    state = load_state()
    if state.get("paused_until"):
        print(f"Paused until: {state['paused_until']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login", help="open a browser to sign in by hand")
    lg.add_argument("--wait", type=int, default=0, help="don't prompt; wait up to N seconds for sign-in (web UI)")
    r = sub.add_parser("run", help="send today's invites")
    r.add_argument("--dry-run", action="store_true", help="log who would be invited; click nothing")
    r.add_argument("--limit", type=int, help="send at most N this run")
    r.add_argument("--force", action="store_true", help="ignore active hours and pauses")
    r.add_argument("--ignore-hours", action="store_true", help="ignore active hours only (manual runs from the UI)")
    r.add_argument("--trigger", default="manual", help="recorded in run history: manual | scheduled")
    r.add_argument("--accept-only", action="store_true", help="only accept incoming invitations, send nothing")
    r.add_argument("--scheduled", action="store_true", help="used by launchd: adds the random start delay")
    sub.add_parser("stats", help="show history")
    ex = sub.add_parser("export-session", help="save LinkedIn cookies from this profile to a file")
    ex.add_argument("file")
    im = sub.add_parser("import-session", help="load LinkedIn cookies from a file into this profile")
    im.add_argument("file")
    im.add_argument("--delete", action="store_true", help="delete the file after a successful import")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    setup_logging(args.verbose)
    commands = {"login": cmd_login, "run": cmd_run, "stats": cmd_stats,
                "export-session": cmd_export_session, "import-session": cmd_import_session}
    return commands[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
