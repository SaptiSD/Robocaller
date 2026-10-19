"""Keeps the server from being put to sleep while it has calls to make.

Render's free tier stops a web service after 15 minutes without an inbound
request, and the dispatcher is a thread inside that service - so while it
sleeps, nothing scheduled is dialled. That is how a daily campaign went quiet
for four days in October 2026: each 11:00 run came due with nobody on the
dashboard, and the one call that did go out was released when someone happened
to open the site 51 minutes later.

So while any campaign has a run scheduled, or any call is still waiting to go
out, this requests the server's own public /healthz every few minutes. The
request leaves the machine and comes back in through Render's proxy, which is
what counts as traffic there. With nothing scheduled it stops asking, and the
host is free to sleep: an idle server dials nothing, and on Render's free plan
it then stops using the plan's monthly hours.

GitHub's scheduled workflow (.github/workflows/keepalive.yml) can't do this job
on its own - GitHub ran its 10-minute schedule about once every three hours.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import Callable
from urllib.parse import urlparse

import httpx

import config
import db

log = logging.getLogger("robocall.keepawake")

# Comfortably inside Render's 15 idle minutes, with room for one failed check-in.
INTERVAL_SECONDS = 5 * 60
# After a failed check-in, try again this soon rather than a full interval later:
# two misses five minutes apart would already be most of the way to fifteen.
RETRY_SECONDS = 60
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def own_url() -> str:
    """This server's public address, or "" when it has none worth calling.

    Render publishes each service's own address as RENDER_EXTERNAL_URL. Elsewhere
    the Settings page's public base URL is, by definition, where this server is
    reached from outside. A local address is no use: a request that never leaves
    the machine is not traffic to anything that might put it to sleep.
    """
    url = (os.getenv("RENDER_EXTERNAL_URL") or config.get("public_base_url") or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname \
            or parsed.hostname in _LOCAL_HOSTS:
        return ""
    return url


def work_scheduled() -> str:
    """Why the server has to stay up right now, in words, or "" when it needn't."""
    # Paused means nothing is dialled; resuming happens from the dashboard, and
    # opening the dashboard wakes the server anyway. The same goes for a server
    # with no Telnyx credentials, which can't dial whatever is queued.
    if config.get("dispatch_paused") == "1" or config.telephony() is None:
        return ""
    # Only a campaign with somebody it could call: an empty list, or one where
    # consent is required and nobody has given it, would hold the host up around
    # the clock for runs that dial no one.
    campaign = db.query_one(
        "SELECT name FROM campaigns c WHERE state = 'active' AND next_run_at IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM contacts ct WHERE ct.campaign_id = c.id "
        "AND (c.require_consent = 0 OR ct.consent = 1)) "
        "ORDER BY next_run_at LIMIT 1")
    if campaign:
        return f"campaign '{campaign['name']}' has a run scheduled"
    waiting = (db.query_one(
        "SELECT COUNT(*) AS n FROM call_tasks t LEFT JOIN campaigns c ON c.id = t.campaign_id "
        "WHERE t.state IN ('pending', 'deferred', 'dialing') "
        "AND (c.state IS NULL OR c.state <> 'paused')") or {}).get("n") or 0
    if waiting:
        return f"{waiting} call(s) still to go out or finish"
    return ""


def _note(message: str, level: str = "info") -> None:
    """Activity, best effort: a database that can't take the note mustn't stop a check-in."""
    try:
        db.log_event(message, level)
    except Exception:  # noqa: BLE001
        log.warning("couldn't record in Activity: %s", message)


def _get(url: str) -> tuple[bool, str]:
    try:
        res = httpx.get(url, timeout=TIMEOUT, follow_redirects=True,
                        headers={"User-Agent": "RoboCallAI-keepawake"})
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if res.status_code != 200:
        return False, f"HTTP {res.status_code}"
    return True, ""


class KeepAwake(threading.Thread):
    def __init__(self, interval: float = INTERVAL_SECONDS,
                 get: Callable[[str], tuple[bool, str]] = _get,
                 retry: float = RETRY_SECONDS) -> None:
        super().__init__(name="keepawake", daemon=True)
        self._stop = threading.Event()
        self.interval = interval
        self.retry = min(retry, interval)
        self._get = get
        self.url = ""
        self.reason = ""
        self.last_ok: datetime | None = None
        self.last_error = ""

    def run(self) -> None:
        # The first check waits a full interval: whatever started the server -
        # a deploy, or a visitor waking it - has already bought it 15 minutes.
        wait = self.interval
        while not self._stop.wait(wait):
            try:
                ok = self.check()
            except Exception as exc:  # noqa: BLE001 - must outlive anything
                self.last_error = str(exc) or type(exc).__name__
                log.exception("keep-awake check failed")
                ok = False
            wait = self.next_wait(ok)

    def next_wait(self, ok: bool) -> float:
        """Seconds until the next check: sooner while holding the server up and failing."""
        return self.retry if self.reason and not ok else self.interval

    def stop(self) -> None:
        self._stop.set()

    def check(self) -> bool:
        """One pass: work out whether staying up matters, and if so, check in.

        Returns True when the server reached itself. Activity hears about each
        change - starting to hold the server up, letting go, losing contact -
        and not about every five-minute check-in.
        """
        try:
            url = own_url()
            reason = work_scheduled() if url else ""
        except Exception as exc:  # noqa: BLE001
            # The database is unreachable. That is no reason to let the host
            # sleep: when the database came back there would be nothing awake to
            # notice, which is exactly how scheduled runs went missing. Keep doing
            # whatever the last answer said.
            if not (self.url and self.reason):
                raise
            log.warning("keep-awake couldn't check for scheduled work (%s); checking in anyway", exc)
            url, reason = self.url, self.reason
        if reason and not self.reason:
            _note(f"Keeping the server awake: {reason}. It checks in with itself every "
                  f"{INTERVAL_SECONDS // 60} minutes so the host doesn't put it to sleep.")
        elif self.reason and not reason:
            _note("Nothing left to call, so the server no longer keeps itself awake. "
                  "Opening the dashboard wakes it.")
        self.url, self.reason = url, reason
        if not reason:
            return False

        ok, error = self._get(f"{url}/healthz")
        if ok:
            if self.last_error:
                _note(f"The server is reaching itself at {url} again.")
            self.last_ok, self.last_error = db.utcnow(), ""
            return True
        if not self.last_error:
            _note(f"The server couldn't reach itself at {url} to stay awake ({error}). If the "
                  "host puts it to sleep, scheduled calls won't go out until someone opens the "
                  "dashboard.", "error")
        log.warning("keep-awake check-in to %s failed: %s", url, error)
        self.last_error = error or "no answer"
        return False

    def status(self) -> dict:
        url = own_url()
        return {
            "url": url,
            # Worked out now rather than read from the last check, which is up to
            # five minutes old - and absent for the first five after a start.
            "holding": work_scheduled() if url else "",
            "last_ok": db.to_utc(self.last_ok) if self.last_ok else None,
            "last_error": self.last_error,
            "every_minutes": INTERVAL_SECONDS // 60,
        }
