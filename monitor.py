#!/usr/bin/env python3
"""
Capago Alger visa-appointment monitor.

Polls the public Capago backend and alerts the moment a watched option opens
inside the "Votre projet" menu — by default: Long séjour (> 90 jours) → Études.

Read-only: it only GETs the public menu endpoint. It never books anything.
Stdlib only — no pip installs required.
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
# Config loading: read config.env (KEY=VALUE) next to this file, then env vars #
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
        # env vars already set (e.g. by systemd) take precedence
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


# --- Core target ----------------------------------------------------------- #
API_BASE = env("API_BASE", "https://visa-fr-dz.capago.eu/rendezvous_alger")
CENTER_ID = env("CENTER_ID", "capago_ALG")
BOOKING_URL = env("BOOKING_URL", "https://appointment-alg.capago.eu/")

# Watch targets: comma-separated "stay_duration_id:reason_id".
# Default = Long séjour → Études.
WATCH_TARGETS = env("WATCH_TARGETS", "long_stay_visa:study")

# --- Polling --------------------------------------------------------------- #
POLL_SECONDS = env_int("POLL_SECONDS", 90)
POLL_JITTER = env_int("POLL_JITTER", 20)       # +/- random seconds
REQUEST_TIMEOUT = env_int("REQUEST_TIMEOUT", 30)
# Re-send the "OPEN" alert every N seconds while it stays open (0 = only once).
REALERT_SECONDS = env_int("REALERT_SECONDS", 1800)
# Upper cap for exponential backoff after errors / rate limiting.
RETRY_BACKOFF_MAX = env_int("RETRY_BACKOFF_MAX", 1800)
# RUN_ONCE=1 → do a single check and exit (used by GitHub Actions / cron).
RUN_ONCE = env_bool("RUN_ONCE", False)

# --- Alert channels (toggles) ---------------------------------------------- #
DESKTOP_ENABLED = env_bool("DESKTOP_ENABLED", True)
SOUND_ENABLED = env_bool("SOUND_ENABLED", True)
SOUND_FILE = env("SOUND_FILE", "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga")
SOUND_REPEAT = env_int("SOUND_REPEAT", 5)
OPEN_BROWSER = env_bool("OPEN_BROWSER", True)

TELEGRAM_TOKEN = env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")

EMAIL_TO = env("EMAIL_TO")
SMTP_HOST = env("SMTP_HOST")
SMTP_PORT = env_int("SMTP_PORT", 587)
SMTP_USER = env("SMTP_USER")
SMTP_PASS = env("SMTP_PASS")
SMTP_FROM = env("SMTP_FROM", SMTP_USER)

STATE_FILE = Path(env("STATE_FILE", str(SCRIPT_DIR / "state.json")))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("capago")


# --------------------------------------------------------------------------- #
# HTTP                                                                         #
# --------------------------------------------------------------------------- #

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Accept": "application/json",
    "Origin": "https://appointment-alg.capago.eu",
    "Referer": "https://appointment-alg.capago.eu/",
}


class RateLimited(Exception):
    """Raised on HTTP 429/503. retry_after = server-requested wait (s) or None."""

    def __init__(self, retry_after, code):
        self.retry_after = retry_after
        self.code = code
        super().__init__(f"HTTP {code}, Retry-After={retry_after}")


def parse_retry_after(value: str | None) -> int | None:
    """Retry-After may be a number of seconds or an HTTP-date. Return seconds."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        dt = parsedate_to_datetime(value)  # HTTP-date form
        return max(0, int(dt.timestamp() - time.time()))
    except Exception:
        return None


def http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (429, 503):
            raise RateLimited(parse_retry_after(e.headers.get("Retry-After")), e.code)
        raise


def fetch_visa_types() -> dict:
    q = urllib.parse.urlencode({"capago_center_id": CENTER_ID})
    return http_get_json(f"{API_BASE}/WebSite_getApplicableVisaTypeList?{q}")


# --------------------------------------------------------------------------- #
# Detection                                                                    #
# --------------------------------------------------------------------------- #

def parse_targets(spec: str) -> list[tuple[str, str]]:
    targets = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        stay, _, reason = part.partition(":")
        targets.append((stay.strip(), reason.strip()))
    return targets


def open_reasons_for(data: dict, stay_id: str) -> list[dict]:
    """Return the list of currently-open reason sub-options under a stay duration."""
    for pl in data.get("product_line_list", []):
        if pl.get("product_line_id") == stay_id:
            return pl.get("product_line_list", [])
    return []


def is_target_open(data: dict, stay_id: str, reason_id: str) -> bool:
    for sub in open_reasons_for(data, stay_id):
        if sub.get("product_line_id") == reason_id:
            return True
        # fallback: match on the visible title (e.g. "Études")
        if reason_id.lower() in str(sub.get("product_line", "")).lower():
            return True
    return False


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
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log.warning("could not write state file: %s", e)


# --------------------------------------------------------------------------- #
# Alert channels (all best-effort)                                             #
# --------------------------------------------------------------------------- #

def alert_desktop(title: str, body: str) -> None:
    if not DESKTOP_ENABLED:
        return
    try:
        subprocess.run(
            ["notify-send", "-u", "critical", "-a", "Capago Monitor", title, body],
            check=False, timeout=10,
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
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log.warning("browser open failed: %s", e)


def alert_telegram(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": "false",
        }).encode()
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
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=REQUEST_TIMEOUT, context=ctx) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=REQUEST_TIMEOUT) as s:
                s.starttls(context=ctx)
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
    except Exception as e:
        log.warning("email failed: %s", e)


def fire_all_alerts(title: str, body: str) -> None:
    log.info("🔔 ALERT: %s | %s", title, body)
    full = f"{title}\n{body}\n{BOOKING_URL}"
    alert_desktop(title, f"{body}\n{BOOKING_URL}")
    alert_sound()
    alert_telegram(f"🔔 {full}")
    alert_email(title, full)
    alert_browser(BOOKING_URL)


# --------------------------------------------------------------------------- #
# Main loop                                                                    #
# --------------------------------------------------------------------------- #

def main() -> None:
    targets = parse_targets(WATCH_TARGETS)
    log.info("Capago monitor starting. Center=%s targets=%s poll=%ss",
             CENTER_ID, targets, POLL_SECONDS)
    log.info("Channels: desktop=%s sound=%s telegram=%s email=%s browser=%s",
             DESKTOP_ENABLED, SOUND_ENABLED,
             bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
             bool(EMAIL_TO and SMTP_HOST), OPEN_BROWSER)

    state = load_state()
    consecutive_errors = 0
    backoff = POLL_SECONDS  # grows on errors, resets to POLL_SECONDS on success

    while True:
        delay = None  # set by error handlers; None => normal cadence
        try:
            data = fetch_visa_types()
            consecutive_errors = 0
            backoff = POLL_SECONDS
            now = time.time()

            for stay_id, reason_id in targets:
                key = f"{stay_id}:{reason_id}"
                tstate = state.get(key, {"open": False, "last_alert": 0})
                open_now = is_target_open(data, stay_id, reason_id)

                if open_now:
                    was_open = tstate.get("open", False)
                    last_alert = tstate.get("last_alert", 0)
                    should_realert = (
                        REALERT_SECONDS > 0 and (now - last_alert) >= REALERT_SECONDS
                    )
                    if not was_open:
                        fire_all_alerts(
                            "✅ Capago: « Long séjour → Études » est OUVERT !",
                            "L'option vient d'apparaître dans « Votre projet ». "
                            "Réserve maintenant, vite.",
                        )
                        tstate["last_alert"] = now
                    elif should_realert:
                        fire_all_alerts(
                            "⏰ Capago: « Études » TOUJOURS ouvert",
                            "L'option est encore disponible — pense à réserver.",
                        )
                        tstate["last_alert"] = now
                    else:
                        log.info("[%s] still OPEN (alert suppressed)", key)
                    tstate["open"] = True
                else:
                    if tstate.get("open"):
                        log.info("[%s] closed again.", key)
                    else:
                        log.info("[%s] not open yet.", key)
                    tstate["open"] = False

                state[key] = tstate

            save_state(state)

        except RateLimited as e:
            consecutive_errors += 1
            # Respect server's Retry-After; never wait less than the current backoff.
            wait = max(e.retry_after or 0, backoff)
            backoff = min(backoff * 2, RETRY_BACKOFF_MAX)
            log.warning("rate limited (HTTP %s). honoring Retry-After=%s -> waiting %ss",
                        e.code, e.retry_after, wait)
            delay = wait

        except Exception as e:
            consecutive_errors += 1
            log.warning("poll error (#%d): %s -> backoff %ss", consecutive_errors, e, backoff)
            delay = backoff
            backoff = min(backoff * 2, RETRY_BACKOFF_MAX)
            # If the site is down for a long time, let the user know once.
            if consecutive_errors == 10:
                fire_all_alerts(
                    "⚠️ Capago monitor: erreurs répétées",
                    f"Le site ne répond pas depuis {consecutive_errors} tentatives. "
                    "Le monitor continue d'essayer.",
                )

        if RUN_ONCE:  # single check (GitHub Actions / cron) — no loop, no sleep
            log.info("RUN_ONCE done, exiting.")
            break

        if delay is None:  # success: normal cadence with jitter
            delay = POLL_SECONDS + random.randint(-POLL_JITTER, POLL_JITTER)
        time.sleep(max(15, delay))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("stopped by user")
