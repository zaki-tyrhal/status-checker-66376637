#!/usr/bin/env python3
"""
Crous housing monitor for trouverunlogement.lescrous.fr.

Polls the public search endpoint of one or more regions and alerts the moment
a NEW listing appears (one that was not present on the previous poll), and
again only the first time it appears. State (the set of already-seen listing
ids) is kept in CROUS_STATE_FILE.

Read-only: it only GETs the public search endpoint. It never books anything.
Stdlib only — no pip installs required.

Note on the search API:
  During heavy-load windows the site serves a static "Vous êtes trop
  nombreux !" HTML page for any non-404 route. We detect that and back off
  gracefully without alerting. Because of this, the exact JSON listing
  endpoint cannot be auto-discovered at write time; it is configurable per
  region in CROUS_REGIONS_FILE. The default value is the most-likely route
  (/api/tools/47/search). To confirm: open the region's /tools/47/search
  page in a browser, DevTools -> Network -> Fetch/XHR -> copy the full URL
  of the request that returns JSON with a list of residences, then paste it
  into the third column of crous-regions.txt.
"""

import json
import logging
import os
import random
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# Config loading (shared conventions with monitor.py)                          #
# --------------------------------------------------------------------------- #

SCRIPT_DIR = Path(__file__).resolve().parent


def load_config_file() -> None:
    path = Path(os.environ.get("CAPAGO_CONFIG", SCRIPT_DIR / "config.env"))
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


load_config_file()


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def env_bool(key: str, default: bool = False) -> bool:
    return env(key, "1" if default else "0").lower() in ("1", "true", "yes", "on")


def env_int(key: str, default: int) -> int:
    try:
        return int(env(key, str(default)))
    except ValueError:
        return default


# --- Polling --------------------------------------------------------------- #
POLL_SECONDS = env_int("CROUS_POLL_SECONDS", 120)
POLL_JITTER = env_int("CROUS_POLL_JITTER", 30)
REQUEST_TIMEOUT = env_int("CROUS_REQUEST_TIMEOUT", 30)
REALERT_SECONDS = env_int(
    "CROUS_REALERT_SECONDS", 3600
)  # re-blast a still-new list every hour
RETRY_BACKOFF_MAX = env_int("CROUS_RETRY_BACKOFF_MAX", 1800)
THROTTLE_BACKOFF = env_int(
    "CROUS_THROTTLE_BACKOFF", 600
)  # extra wait when the "trop nombreux" page hits
RUN_ONCE = env_bool("CROUS_RUN_ONCE", False)

REGIONS_FILE = Path(env("CROUS_REGIONS_FILE", str(SCRIPT_DIR / "crous-regions.txt")))
STATE_FILE = Path(env("CROUS_STATE_FILE", str(SCRIPT_DIR / "crous-state.json")))

# --- Alert channels (toggles) ---------------------------------------------- #
DESKTOP_ENABLED = env_bool("CROUS_DESKTOP_ENABLED", True)
SOUND_ENABLED = env_bool("CROUS_SOUND_ENABLED", True)
SOUND_FILE = env(
    "CROUS_SOUND_FILE", "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"
)
SOUND_REPEAT = env_int("CROUS_SOUND_REPEAT", 3)
OPEN_BROWSER = env_bool("CROUS_OPEN_BROWSER", True)

TELEGRAM_TOKEN = env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")

EMAIL_TO = env("EMAIL_TO")
SMTP_HOST = env("SMTP_HOST")
SMTP_PORT = env_int("SMTP_PORT", 587)
SMTP_USER = env("SMTP_USER")
SMTP_PASS = env("SMTP_PASS")
SMTP_FROM = env("SMTP_FROM", SMTP_USER)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("crous")


# --------------------------------------------------------------------------- #
# HTTP                                                                        #
# --------------------------------------------------------------------------- #

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Accept": "application/ld+json,application/json,text/plain,*/*",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Origin": "https://trouverunlogement.lescrous.fr",
    "Referer": "https://trouverunlogement.lescrous.fr/tools/47/search",
}


class RateLimited(Exception):
    """Raised on HTTP 429/503. retry_after = server-requested wait (s) or None."""

    def __init__(self, retry_after, code):
        self.retry_after = retry_after
        self.code = code
        super().__init__(f"HTTP {code}, Retry-After={retry_after}")


class Throttled(Exception):
    """Static 'Vous êtes trop nombreux !' page was served (site over-capacity)."""


def parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        dt = parsedate_to_datetime(value)
        return max(0, int(dt.timestamp() - time.time()))
    except Exception:
        return None


def http_get(url: str) -> dict | list | None:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            ct = resp.headers.get("Content-Type", "").lower()
            body = resp.read().decode("utf-8", "replace")
            # The throttle page is text/html and ~13 KB long.
            if "text/html" in ct and (
                "trop nombreux" in body or "Vous \u00eates" in body
            ):
                raise Throttled()
            if not body:
                return None
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                # Some API Platform errors are JSON; surface non-JSON to caller.
                log.warning(
                    "non-JSON response (ct=%s, len=%d): %s", ct, len(body), body[:160]
                )
                return None
    except urllib.error.HTTPError as e:
        if e.code in (429, 503):
            raise RateLimited(parse_retry_after(e.headers.get("Retry-After")), e.code)
        # Let other HTTP errors bubble up as a generic exception.
        raise RuntimeError(f"HTTP {e.code} {e.reason}") from e


# --------------------------------------------------------------------------- #
# Regions config                                                              #
# --------------------------------------------------------------------------- #


class Region:
    __slots__ = ("id", "name", "api_url", "viewer_url")

    def __init__(self, id_, name, api_url, viewer_url):
        self.id = id_
        self.name = name
        self.api_url = api_url
        self.viewer_url = viewer_url

    def __repr__(self):
        return f"<Region {self.id} {self.name}>"


def load_regions() -> list[Region]:
    if not REGIONS_FILE.is_file():
        log.error("regions file not found: %s", REGIONS_FILE)
        return []
    out: list[Region] = []
    for lineno, raw in enumerate(
        REGIONS_FILE.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 4:
            log.warning(
                "%s:%d: expected id|name|api_url|viewer_url — got %d fields",
                REGIONS_FILE,
                lineno,
                len(parts),
            )
            continue
        rid, name, api_url, viewer_url = (p.strip() for p in parts[:4])
        out.append(Region(rid, name, api_url, viewer_url))
    return out


# --------------------------------------------------------------------------- #
# Listing extraction (defensive against unknown JSON shape)                   #
# --------------------------------------------------------------------------- #


def _candidate_lists(data) -> list:
    """Return the list(s) inside `data` that look like a residence listing."""
    if data is None:
        return []
    if isinstance(data, list):
        return [data]
    if not isinstance(data, dict):
        return []
    # API Platform / JSON-LD naming first.
    candidates = []
    for key in (
        "hydra:member",
        "member",
        "results",
        "items",
        "data",
        "logements",
        "residences",
        "offers",
        "accommodations",
    ):
        v = data.get(key)
        if isinstance(v, list):
            candidates.append(v)
    # Last resort: any list-valued field whose name we haven't tried.
    if not candidates:
        for k, v in data.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                candidates.append(v)
                break
    return candidates


def _listing_id(item: dict) -> str | None:
    """Pick the most stable unique id of a listing item."""
    for key in ("id", "@id", "slug", "reference", "uid", "code"):
        v = item.get(key)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    # Fall back to a composite of obvious identity fields.
    composite = []
    for key in ("name", "title", "city", "address", "postcode"):
        v = item.get(key)
        if v:
            composite.append(str(v).strip())
    if composite:
        return "|".join(composite)
    # Last resort: hash the serialised entry.
    return "h:" + str(hash(json.dumps(item, sort_keys=True, ensure_ascii=False)))


def _listing_label(item: dict) -> str:
    """Human-readable label used in the alert."""
    for key in ("name", "title", "nom"):
        v = item.get(key)
        if v:
            return str(v)
    city = item.get("city") or item.get("ville")
    if city:
        return f"(no title) — {city}"
    return "(no title)"


def _listing_url(item: dict, region: Region) -> str:
    """Build the best deep link we can for a listing."""
    for key in ("url", "link", "@id", "href"):
        v = item.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    slug = item.get("slug") or item.get("id")
    if slug:
        return f"{region.viewer_url}#/{slug}"
    return region.viewer_url


def extract_listings(data, region: Region) -> list[tuple[str, str, str]]:
    """Return [(id, label, url), ...] for every listing in the response."""
    out: list[tuple[str, str, str]] = []
    for lst in _candidate_lists(data):
        for item in lst:
            if not isinstance(item, dict):
                continue
            lid = _listing_id(item)
            if not lid:
                continue
            label = _listing_label(item)
            url = _listing_url(item, region)
            out.append((lid, label, url))
    # De-dupe by id while preserving order.
    seen: set[str] = set()
    uniq = []
    for lid, label, url in out:
        if lid in seen:
            continue
        seen.add(lid)
        uniq.append((lid, label, url))
    return uniq


# --------------------------------------------------------------------------- #
# State                                                                        #
# --------------------------------------------------------------------------- #


def load_state() -> dict:
    if STATE_FILE.is_file():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    except Exception as e:
        log.warning("could not write state file: %s", e)


# --------------------------------------------------------------------------- #
# Alert channels (best-effort, mirrors monitor.py)                            #
# --------------------------------------------------------------------------- #


def alert_desktop(title: str, body: str) -> None:
    if not DESKTOP_ENABLED:
        return
    try:
        subprocess.run(
            ["notify-send", "-u", "critical", "-a", "Crous Monitor", title, body],
            check=False,
            timeout=10,
        )
    except Exception as e:
        log.warning("desktop notify failed: %s", e)


def alert_sound() -> None:
    if not SOUND_ENABLED or not Path(SOUND_FILE).is_file():
        return
    try:
        for _ in range(max(1, SOUND_REPEAT)):
            subprocess.run(["paplay", SOUND_FILE], check=False, timeout=15)
    except Exception as e:
        log.warning("sound failed: %s", e)


def alert_browser(url: str) -> None:
    if not OPEN_BROWSER:
        return
    try:
        subprocess.Popen(
            ["xdg-open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log.warning("browser open failed: %s", e)


def alert_telegram(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode(
            {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": "false",
            }
        ).encode()
        req = urllib.request.Request(url, data=payload)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            resp.read()
    except Exception as e:
        log.warning("telegram failed: %s", e)


def alert_email(subject: str, body: str) -> None:
    if not (EMAIL_TO and SMTP_HOST and SMTP_USER and SMTP_PASS):
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM or SMTP_USER
        msg["To"] = EMAIL_TO
        msg.set_content(body)
        ctx = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(
                SMTP_HOST, SMTP_PORT, timeout=REQUEST_TIMEOUT, context=ctx
            ) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=REQUEST_TIMEOUT) as s:
                s.starttls(context=ctx)
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
    except Exception as e:
        log.warning("email failed: %s", e)


def fire_all_alerts(title: str, body: str, url: str | None = None) -> None:
    log.info("🔔 ALERT: %s | %s", title, body)
    full = f"{title}\n{body}" + (f"\n{url}" if url else "")
    alert_desktop(title, body + (f"\n{url}" if url else ""))
    alert_sound()
    alert_telegram(f"🔔 {full}")
    alert_email(title, full)
    if url:
        alert_browser(url)


# --------------------------------------------------------------------------- #
# Main loop                                                                    #
# --------------------------------------------------------------------------- #


def poll_region(region: Region, state: dict) -> int:
    """Poll one region; return the delay (seconds) the loop should wait after.
    A return of -1 means 'use normal cadence'. Other positive values override.
    """
    key = region.id
    rstate = state.get(key, {"seen_ids": [], "last_realert": 0, "first_poll": True})
    seen_ids: set[str] = set(rstate.get("seen_ids", []))
    first_poll = rstate.get("first_poll", True)
    now = time.time()

    try:
        data = http_get(region.api_url)
    except Throttled:
        log.warning(
            "[%s] site throttle page ('Vous êtes trop nombreux !') — backing off %ss",
            key,
            THROTTLE_BACKOFF,
        )
        return THROTTLE_BACKOFF
    except RateLimited as e:
        wait = max(e.retry_after or 0, int(REQUEST_TIMEOUT))
        log.warning("[%s] HTTP 429/503 — honoring wait=%ss", key, wait)
        return wait
    except Exception as e:
        log.warning("[%s] fetch error: %s", key, e)
        return -1  # callers bump consecutive_errors count and apply backoff

    listings = extract_listings(data, region)
    current_ids = {lid for lid, _, _ in listings}
    new_ids = current_ids - seen_ids
    new_items = [(lid, label, url) for lid, label, url in listings if lid in new_ids]

    if first_poll:
        # Never alert on the very first poll; just record the baseline.
        log.info(
            "[%s] initial baseline: %d listings recorded (no alert).",
            key,
            len(current_ids),
        )
        rstate["seen_ids"] = sorted(current_ids)
        rstate["first_poll"] = False
        rstate["last_realert"] = now
        state[key] = rstate
        return -1

    if new_items:
        lines = []
        for lid, label, url in new_items:
            lines.append(f"• {label}\n  {url}")
        body = (
            f"{len(new_items)} nouvelle(s) annonce(s) « {region.name} » :\n\n"
            + "\n\n".join(lines)
            + f"\n\nVoir le site: {region.viewer_url}"
        )
        fire_all_alerts(
            f"🏠 Crous {region.name}: {len(new_items)} nouvelle(s) annonce(s)",
            body,
            region.viewer_url,
        )
        rstate["seen_ids"] = sorted(current_ids)
        rstate["last_realert"] = now
    else:
        # No new ones; re-alert occasionally to remind the user the monitor is alive.
        last = rstate.get("last_realert", 0)
        if REALERT_SECONDS > 0 and (now - last) >= REALERT_SECONDS:
            log.info("[%s] no new listings; sending periodic reassurance ping.", key)
            alert_telegram(
                f"💤 Crous {region.name}: toujours surveillé. "
                f"{len(current_ids)} annonce(s) connue(s), aucune nouvelle depuis "
                f"{int((now - last) / 60)} min."
            )
            rstate["last_realert"] = now

    state[key] = rstate
    return -1


def main() -> None:
    regions = load_regions()
    if not regions:
        log.error(
            "no regions configured in %s — nothing to monitor. Exiting.", REGIONS_FILE
        )
        return
    log.info(
        "Crous monitor starting. regions=%s poll=%ss jitter=%ss",
        [r.id for r in regions],
        POLL_SECONDS,
        POLL_JITTER,
    )
    log.info(
        "Channels: desktop=%s sound=%s telegram=%s email=%s browser=%s",
        DESKTOP_ENABLED,
        SOUND_ENABLED,
        bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
        bool(EMAIL_TO and SMTP_HOST),
        OPEN_BROWSER,
    )

    state = load_state()
    consecutive_errors = 0
    backoff = POLL_SECONDS

    while True:
        override_delay: int | None = None
        any_error = False
        for region in regions:
            d = poll_region(region, state)
            if d == -1:
                continue
            override_delay = max(override_delay or 0, d)

        save_state(state)

        # RUN_ONCE exits after a single sweep — for GitHub Actions / cron.
        if RUN_ONCE:
            log.info("RUN_ONCE done, exiting.")
            break

        if override_delay is not None:
            delay = override_delay
        else:
            delay = POLL_SECONDS + random.randint(-POLL_JITTER, POLL_JITTER)
        time.sleep(max(15, delay))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("stopped by user")
