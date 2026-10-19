#!/usr/bin/env python3
"""
Avengers: Doomsday ticket bot — Palaxi Cinemas, HiLITE Mall, Thrissur
--------------------------------------------------------------------
Every 10 minutes it:
  1. Opens Palaxi HiLITE Thrissur's BookMyShow page for 17 Dec onwards.
  2. Looks for "Avengers: Doomsday" in that cinema's own show listings
     (not in the page footer, which lists upcoming movies for the whole city).
  3. Also checks whether booking has opened anywhere in Thrissur.
  4. Sends you a Telegram message (with a screenshot) when something changes.

Usage:
    python bot.py                   # run forever
    python bot.py --once            # one check, then exit
    python bot.py --test-telegram   # send a test Telegram message
    python bot.py --reset           # forget past alerts / saved links
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"
SHOTS_DIR = BASE / "screenshots"
PROFILE_DIR = BASE / ".browser-profile"
LOG_PATH = BASE / "bot.log"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
BLOCK_MARKERS = ["verify you are human", "just a moment", "access denied",
                 "are you a robot", "unusual traffic", "captcha"]
# Where BookMyShow's site-wide footer starts (it lists movies for the whole
# city, so text after this must never count as "showing at Palaxi")
FOOTER_MARKERS = ["HomeCinemas in", "Home\nCinemas in", "List your Show",
                  "MOVIES NOW SHOWING", "UPCOMING MOVIES IN",
                  "TOP CINEMAS IN", "24/7 CUSTOMER CARE"]
DATE_RE = re.compile(r"/(20\d{6})(?=/?$|[?#])")

log = logging.getLogger("ticketbot")
PAGES_OK = 0  # pages loaded successfully during the current check

DEFAULTS = {
    "movie_name": "Avengers: Doomsday",
    "movie_keyword": "doomsday",
    "theatre_keyword": "palaxi",
    "exclude_words": ["calicut", "kozhikode"],
    "city": "thrissur",
    "first_date": "20261217",
    "days_to_check": 5,
    "extra_dates": [],
    "bms_base": "https://in.bookmyshow.com",
    "palaxi_venue_url": "https://in.bookmyshow.com/cinemas/thrissur/"
                        "palaxi-cinemas-hilite-mall-thrissur/PCAT",
    "movie_url": "",
    "web_search_fallback": True,
    "interval_minutes": 10,
    "jitter_seconds": 60,
    "headless": True,
    "page_wait_seconds": 6,
    "repeat_alert_minutes": 0,
    "alert_after_consecutive_failures": 3,
    "daily_status_time": "09:00",
    "desktop_notifications": True,
    "telegram": {},
    "whatsapp": {},
}


# --------------------------------------------------------------------------- #
# Config & state
# --------------------------------------------------------------------------- #
def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    cfg["bms_base"] = cfg["bms_base"].rstrip("/")
    if not cfg.get("palaxi_venue_url"):
        cfg["palaxi_venue_url"] = ""

    # Secrets can come from environment variables (GitHub Actions secrets)
    tg = cfg["telegram"] = dict(cfg.get("telegram") or {})
    for env, key in (("TELEGRAM_BOT_TOKEN", "bot_token"),
                     ("TELEGRAM_CHAT_ID", "chat_id")):
        if os.environ.get(env, "").strip():
            tg[key] = os.environ[env].strip()
    wa = cfg["whatsapp"] = dict(cfg.get("whatsapp") or {})
    for env, key in (("WHATSAPP_PHONE", "phone"),
                     ("CALLMEBOT_APIKEY", "callmebot_apikey"),
                     ("WHATSAPP_PROVIDER", "provider"),
                     ("TWILIO_ACCOUNT_SID", "twilio_account_sid"),
                     ("TWILIO_AUTH_TOKEN", "twilio_auth_token"),
                     ("TWILIO_FROM", "twilio_from")):
        if os.environ.get(env, "").strip():
            wa[key] = os.environ[env].strip()

    if os.environ.get("CI"):          # running on GitHub Actions
        cfg["headless"] = True
        cfg["desktop_notifications"] = False
    return cfg


def load_state():
    st = {}
    if STATE_PATH.exists():
        try:
            st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    for k, v in (("alerted_dates", {}), ("thrissur_open_alerted", False),
                 ("failures", 0), ("failure_alert_sent", False),
                 ("venue_url", ""), ("movie_url", ""), ("last_daily", ""),
                 ("last_summary", "No check yet"), ("started_sent", False),
                 ("telegram_chat_id", "")):
        st.setdefault(k, v)
    return st


def save_state(st):
    STATE_PATH.write_text(json.dumps(st, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Notifications: Telegram (main), WhatsApp (optional), desktop
# --------------------------------------------------------------------------- #
def _real(v):
    return bool(v) and "PASTE" not in str(v)


def telegram_chat_id(cfg, st):
    """Use the configured chat id, or find it from the bot's recent messages."""
    tg = cfg["telegram"]
    if _real(tg.get("chat_id")):
        return str(tg["chat_id"])
    if st and st.get("telegram_chat_id"):
        return st["telegram_chat_id"]
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{tg['bot_token']}/getUpdates",
            timeout=30)
        for upd in reversed(r.json().get("result", [])):
            msg = upd.get("message") or upd.get("my_chat_member") or {}
            chat = msg.get("chat", {})
            if chat.get("type") == "private" and chat.get("id"):
                cid = str(chat["id"])
                log.info("Found your Telegram chat id: %s "
                         "(add it as TELEGRAM_CHAT_ID to make it permanent)", cid)
                if st is not None:
                    st["telegram_chat_id"] = cid
                return cid
    except (requests.RequestException, ValueError) as e:
        log.warning("Couldn't look up Telegram chat: %s", e)
    log.warning("No Telegram chat found — open your bot in Telegram, press "
                "Start (or send it any message), then try again.")
    return ""


def send_telegram(cfg, text, st=None, photo=None):
    tg = cfg["telegram"]
    if not _real(tg.get("bot_token")):
        return False
    chat_id = telegram_chat_id(cfg, st)
    if not chat_id:
        return False
    api = f"https://api.telegram.org/bot{tg['bot_token']}"
    try:
        if photo and Path(photo).exists():
            with open(photo, "rb") as f:
                r = requests.post(f"{api}/sendPhoto",
                                  data={"chat_id": chat_id,
                                        "caption": text[:1024]},
                                  files={"photo": f}, timeout=60)
            if r.ok:
                log.info("Telegram sent (with screenshot).")
                return True
        r = requests.post(f"{api}/sendMessage",
                          data={"chat_id": chat_id, "text": text[:4000],
                                "disable_web_page_preview": "true"},
                          timeout=30)
    except requests.RequestException as e:
        log.warning("Telegram send failed: %s", e)
        return False
    if not r.ok:
        log.warning("Telegram error %s: %s", r.status_code, r.text[:200])
        return False
    log.info("Telegram sent.")
    return True


def send_whatsapp(cfg, text):
    wa = cfg["whatsapp"]
    phone = str(wa.get("phone", "")).replace(" ", "")
    if not _real(phone):
        return False
    provider = wa.get("provider", "callmebot").lower()
    try:
        if provider == "twilio":
            sid, token = wa.get("twilio_account_sid"), wa.get("twilio_auth_token")
            if not (_real(sid) and _real(token)):
                return False
            r = requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                auth=(sid, token),
                data={"From": f"whatsapp:{wa.get('twilio_from', '+14155238886')}",
                      "To": f"whatsapp:{phone}", "Body": text}, timeout=40)
        else:
            key = wa.get("callmebot_apikey")
            if not _real(key):
                return False
            r = requests.get("https://api.callmebot.com/whatsapp.php",
                             params={"phone": phone, "text": text,
                                     "apikey": key}, timeout=40)
    except requests.RequestException as e:
        log.warning("WhatsApp send failed: %s", e)
        return False
    if not r.ok:
        log.warning("WhatsApp error %s", r.status_code)
        return False
    log.info("WhatsApp sent.")
    return True


def notify(cfg, text, st=None, photo=None):
    log.info("ALERT: %s", text.replace("\n", " | "))
    sent = send_telegram(cfg, text, st, photo)
    sent = send_whatsapp(cfg, text) or sent
    if not sent:
        log.warning("No alert channel worked — check the Telegram settings.")
    if cfg.get("desktop_notifications", True):
        try:
            from plyer import notification
            notification.notify(title="Ticket bot", message=text[:250],
                                timeout=20)
        except Exception as e:
            log.debug("Desktop notification unavailable: %s", e)
        print("\a", end="", flush=True)
    return sent


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def main_section(text):
    """Page text above BookMyShow's site-wide footer."""
    cut = len(text)
    for m in FOOTER_MARKERS:
        i = text.find(m)
        if i != -1:
            cut = min(cut, i)
    return text[:cut]


def open_page(page, url, cfg):
    """Load a URL; return (status, text). status: ok | blocked | error"""
    global PAGES_OK
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(cfg["page_wait_seconds"] * 1000)
        text = page.inner_text("body")
    except PWTimeout:
        log.warning("Timed out: %s", url)
        return "error", ""
    except Exception as e:
        log.warning("Failed to load %s: %s", url, str(e).splitlines()[0])
        return "error", ""
    low = text.lower()
    if len(low) < 1500 and any(m in low for m in BLOCK_MARKERS):
        return "blocked", text
    if resp is not None and resp.status in (403, 429, 503) and len(low) < 1500:
        return "blocked", text
    if resp is not None and resp.status == 404:
        return "error", text
    PAGES_OK += 1
    return "ok", text


def page_links(page):
    try:
        return page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => [e.href, (e.innerText || e.title || '')])")
    except Exception:
        return []


def web_search(query):
    """Fallback: DuckDuckGo HTML search, returns result URLs."""
    try:
        r = requests.get("https://html.duckduckgo.com/html/",
                         params={"q": query}, headers={"User-Agent": UA},
                         timeout=30)
    except requests.RequestException as e:
        log.warning("Web search failed: %s", e)
        return []
    urls = [unquote(u) for u in re.findall(r"uddg=([^&\"']+)", r.text)]
    urls += re.findall(r'href="(https?://[^"]+)"', r.text)
    return list(dict.fromkeys(urls))


def with_date(url, date):
    if DATE_RE.search(url):
        return DATE_RE.sub("/" + date, url, count=1)
    return url.split("?")[0].split("#")[0].rstrip("/") + "/" + date


def date_of(url):
    m = DATE_RE.search(url.split("#")[0])
    return m.group(1) if m else ""


def pretty_date(d):
    try:
        return datetime.strptime(d, "%Y%m%d").strftime("%a %d %b")
    except ValueError:
        return d or "today"


# --------------------------------------------------------------------------- #
# Finding the pages
# --------------------------------------------------------------------------- #
def is_palaxi_venue(href, text, cfg):
    h, t = href.lower(), text.lower()
    return ("/cinemas/" in h and cfg["city"] in h
            and cfg["theatre_keyword"] in h + " " + t
            and not any(w in h + " " + t for w in cfg["exclude_words"]))


def palaxi_dated_url(page, cfg, st):
    """
    Return Palaxi Thrissur's dated page address, e.g.
    .../palaxi-cinemas-hilite-mall-thrissur/buytickets/PCAT/20261019
    (BookMyShow redirects the cinema link to this form).
    """
    candidates = [u for u in (st["venue_url"], cfg["palaxi_venue_url"]) if u]
    for url in candidates:
        status, _ = open_page(page, url, cfg)
        if status == "ok" and cfg["theatre_keyword"] in page.url.lower():
            if page.url != st["venue_url"]:
                st["venue_url"] = page.url
            return page.url
        if status == "blocked":
            return ""

    # Fall back to finding it in BookMyShow's list of Thrissur cinemas
    base, city = cfg["bms_base"], cfg["city"]
    for url in (f"{base}/{city}/cinemas", f"{base}/explore/cinemas-{city}"):
        status, _ = open_page(page, url, cfg)
        if status != "ok":
            continue
        for href, text in page_links(page):
            if is_palaxi_venue(href, text, cfg):
                log.info("Found Palaxi Thrissur page: %s", href)
                status, _ = open_page(page, href, cfg)
                if status == "ok":
                    st["venue_url"] = page.url
                    return page.url
    if cfg["web_search_fallback"]:
        for href in web_search(f"bookmyshow {cfg['theatre_keyword']} cinemas "
                               f"hilite mall {city}"):
            if "bookmyshow.com" in href and is_palaxi_venue(href, "", cfg):
                status, _ = open_page(page, href, cfg)
                if status == "ok":
                    st["venue_url"] = page.url
                    return page.url
    return ""


def find_movie(page, cfg, st):
    if cfg["movie_url"]:
        return cfg["movie_url"]
    if st["movie_url"]:
        return st["movie_url"]

    base, city = cfg["bms_base"], cfg["city"]
    slug, kw = slugify(cfg["movie_name"]), cfg["movie_keyword"]

    for url in (f"{base}/explore/movies-{city}",
                f"{base}/explore/upcoming-movies-{city}"):
        status, _ = open_page(page, url, cfg)
        if status != "ok":
            continue
        for href, text in page_links(page):
            h = href.lower()
            if (re.search(r"/ET\d{5,}", href) and "/buytickets/" not in h
                    and (slug in h or kw in text.lower())):
                log.info("Found movie page: %s", href)
                st["movie_url"] = href
                return href

    if cfg["web_search_fallback"]:
        for href in web_search(f"bookmyshow {cfg['movie_name']} {city}"):
            m = re.search(r"/(ET\d{5,})", href)
            if "bookmyshow.com" in href and m and slug in href.lower():
                for cand in (f"{base}/movies/{city}/{slug}/{m.group(1)}",
                             f"{base}/{city}/movies/{slug}/{m.group(1)}"):
                    status, text = open_page(page, cand, cfg)
                    if status == "ok" and kw in text.lower():
                        log.info("Found movie page via search: %s", cand)
                        st["movie_url"] = cand
                        return cand
    return ""


def dates_to_check(cfg):
    start = datetime.strptime(cfg["first_date"], "%Y%m%d")
    dates = {(start + timedelta(days=i)).strftime("%Y%m%d")
             for i in range(max(1, cfg["days_to_check"]))}
    dates.update(cfg.get("extra_dates", []))
    return sorted(dates)[:12]


def showtimes_snippet(text, kw):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if kw in line.lower():
            short = [l for l in lines[i:i + 12] if len(l) <= 60]
            return "\n".join(short[:8])[:450]
    return ""


def should_alert(st, key, cfg):
    last = st["alerted_dates"].get(key)
    if not last:
        return True
    repeat = cfg.get("repeat_alert_minutes") or 0
    if repeat <= 0:
        return False
    try:
        return datetime.now() - datetime.fromisoformat(last) >= \
            timedelta(minutes=repeat) - timedelta(seconds=90)
    except ValueError:
        return True


# --------------------------------------------------------------------------- #
# One full check
# --------------------------------------------------------------------------- #
def check_once(cfg, st):
    global PAGES_OK
    PAGES_OK = 0
    kw = cfg["movie_keyword"].lower()
    palaxi_live, palaxi_state, thrissur_state = [], "unknown", "unknown"

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=cfg["headless"], user_agent=UA,
            viewport={"width": 1366, "height": 900}, locale="en-IN",
            timezone_id="Asia/Kolkata")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # ---- 1. Palaxi HiLITE Thrissur ------------------------------------
        venue = palaxi_dated_url(page, cfg, st)
        if not venue:
            log.info("Couldn't open Palaxi Thrissur's BookMyShow page this time")
            palaxi_state = "page not reachable"
        else:
            palaxi_state = "not live yet"
            for d in dates_to_check(cfg):
                time.sleep(random.uniform(2, 5))
                status, text = open_page(page, with_date(venue, d), cfg)
                shown = date_of(page.url) or d   # BMS may redirect to another date
                found = status == "ok" and kw in main_section(text).lower()
                if status != "ok":
                    result = status.upper()
                elif found:
                    result = "LIVE" + (f" (on {pretty_date(shown)})"
                                       if shown != d else "")
                elif shown != d:
                    result = "no shows listed yet"
                else:
                    result = "not live"
                log.info("Palaxi Thrissur %-10s -> %s", pretty_date(d), result)
                if not found:
                    continue
                palaxi_state = "LIVE"
                if shown not in palaxi_live:
                    palaxi_live.append(shown)
                if should_alert(st, shown, cfg):
                    shot = None
                    try:
                        SHOTS_DIR.mkdir(exist_ok=True)
                        shot = SHOTS_DIR / f"palaxi_{shown}.png"
                        page.screenshot(path=str(shot))
                    except Exception:
                        shot = None
                    snippet = showtimes_snippet(main_section(text), kw)
                    notify(cfg,
                           "🎟️ AVENGERS: DOOMSDAY TICKETS ARE LIVE!\n"
                           "Palaxi Cinemas, HiLITE Mall Thrissur\n"
                           f"Date: {pretty_date(shown)}\n"
                           + (f"\n{snippet}\n" if snippet else "")
                           + f"\nBook now 👉 {page.url}", st, shot)
                    st["alerted_dates"][shown] = datetime.now().isoformat()
                    save_state(st)

        # ---- 2. Has booking opened anywhere in Thrissur? ------------------
        time.sleep(random.uniform(2, 5))
        movie = find_movie(page, cfg, st)
        if not movie:
            thrissur_state = "movie not listed yet"
        else:
            status, text = open_page(page, movie, cfg)
            if status != "ok":
                thrissur_state = status
            else:
                try:
                    has_button = page.get_by_role(
                        "button", name=re.compile(r"book tickets", re.I)).count() > 0 \
                        or page.get_by_role(
                        "link", name=re.compile(r"book tickets", re.I)).count() > 0
                except Exception:
                    has_button = "book tickets" in main_section(text).lower()
                if has_button:
                    thrissur_state = "OPEN"
                    if not st["thrissur_open_alerted"] and not palaxi_live:
                        notify(cfg,
                               "🍿 Heads-up: Avengers: Doomsday booking has opened "
                               "in Thrissur on BookMyShow — Palaxi HiLITE isn't "
                               "showing it yet. I'll message you the moment it is.\n"
                               f"{page.url}", st)
                        st["thrissur_open_alerted"] = True
                else:
                    thrissur_state = "not open yet"
        log.info("Thrissur booking -> %s", thrissur_state)

        ctx.close()

    # ---- Health tracking ---------------------------------------------------
    if PAGES_OK > 0:
        st["failures"], st["failure_alert_sent"] = 0, False
    else:
        st["failures"] += 1
        if (st["failures"] >= cfg["alert_after_consecutive_failures"]
                and not st["failure_alert_sent"]):
            notify(cfg, "⚠️ Ticket bot can't read BookMyShow "
                        f"({st['failures']} checks in a row). It keeps trying. "
                        + ("BookMyShow may be blocking GitHub's servers — if this "
                           "keeps happening, run the bot on your own PC instead."
                           if os.environ.get("CI") else
                           "If this continues, set \"headless\": false in "
                           "config.json and restart it."), st)
            st["failure_alert_sent"] = True

    st["last_summary"] = (f"Palaxi HiLITE Thrissur: {palaxi_state}"
                          + (f" ({', '.join(pretty_date(d) for d in palaxi_live)})"
                             if palaxi_live else "")
                          + f"\nThrissur booking: {thrissur_state}")
    save_state(st)


def maybe_daily_status(cfg, st):
    hhmm = cfg.get("daily_status_time")
    if not hhmm:
        return
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if st["last_daily"] != today and now.strftime("%H:%M") >= hhmm:
        notify(cfg, "🤖 Ticket bot is still watching.\n" + st["last_summary"]
               + f"\nChecked at {now:%d %b %I:%M %p}", st)
        st["last_daily"] = today
        save_state(st)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Avengers: Doomsday ticket bot")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test-telegram", "--test", action="store_true",
                    dest="test")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(message)s",
        datefmt="%d-%b %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(LOG_PATH, encoding="utf-8")])

    if args.reset:
        STATE_PATH.unlink(missing_ok=True)
        print("Reset done: alerts and saved links cleared.")
        return

    cfg = load_config()
    st = load_state()

    if args.test:
        ok = notify(cfg, "✅ Ticket bot test — Telegram alerts work! "
                         "You'll get a message here the moment Avengers: "
                         "Doomsday tickets open at Palaxi HiLITE Thrissur.", st)
        save_state(st)
        print("Sent!" if ok else "Failed — see the messages above / bot.log")
        sys.exit(0 if ok else 1)

    def after_check():
        if not st["started_sent"]:
            notify(cfg, "🤖 Ticket bot is live! I'll check Palaxi HiLITE "
                        f"Thrissur every {cfg['interval_minutes']} min "
                        f"(from {pretty_date(cfg['first_date'])} onwards) and "
                        "message you when Avengers: Doomsday tickets open.\n"
                        + st["last_summary"], st)
            st["started_sent"] = True
            st["last_daily"] = datetime.now().strftime("%Y-%m-%d")
            save_state(st)
        maybe_daily_status(cfg, st)

    if args.once:
        check_once(cfg, st)
        after_check()
        print("\n" + st["last_summary"])
        return

    log.info("Ticket bot started — checking every %s min. Ctrl+C to stop.",
             cfg["interval_minutes"])
    while True:
        try:
            check_once(cfg, st)
            after_check()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.exception("Check crashed: %s", e)
        wait = cfg["interval_minutes"] * 60 + random.randint(
            -cfg["jitter_seconds"], cfg["jitter_seconds"])
        log.info("Next check in %d min %d s", wait // 60, wait % 60)
        time.sleep(max(60, wait))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
