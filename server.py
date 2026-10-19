"""RoboCall AI - web server.

FastAPI serves the dashboard, a small JSON API, and the Telnyx webhooks. The
dispatcher runs as a daemon thread in this process, so calls go out whether or
not a browser is open - closing the tab does not stop a campaign.

Run it:  python -m uvicorn server:app --port 8000
"""
from __future__ import annotations

import base64
import csv
import hmac
import io
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlparse
from xml.sax.saxutils import escape, quoteattr
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import agent
import compliance
import config
import db
import dispatcher
import llm_proxy
import scriptwriter
import telephony
import wakeup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("robocall")

WEB_DIR = Path(__file__).parent / "web"
DOCS_DIR = Path(__file__).parent / "docs"
_engine: dispatcher.Dispatcher | None = None
# wakeup.status() reads pg_cron's tables, and every open dashboard asks for the
# overview every few seconds - once a minute is plenty for something that
# changes every five.
_wake_cache: tuple[float, dict] | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine
    archived, migrated = db.init()
    log.info("database: %s", db.describe())
    if db.dotenv_database_ignored():
        log.warning("DATABASE_URL in .env is ignored on purpose - this run uses the local "
                    "SQLite file, not the hosted database. Set DATABASE_URL in the shell "
                    "itself to run against Postgres.")
    if migrated:
        log.info("added missing columns: %s", ", ".join(migrated))
    if archived:
        log.warning(
            "An older prototype's database was at that path. It has been moved to "
            "%s and a fresh one created.", archived,
        )
    _engine = dispatcher.Dispatcher()
    _engine.start()
    yield
    if _engine:
        _engine.stop()


app = FastAPI(title="RoboCall AI", lifespan=lifespan)


@app.get("/healthz", include_in_schema=False)
def healthz() -> JSONResponse:
    """Liveness for uptime pingers and Render's health check. Reveals nothing else."""
    running = bool(_engine and _engine.is_alive())
    return JSONResponse({"ok": running}, status_code=200 if running else 503)


# --- request models ---------------------------------------------------------

class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    message: str = ""
    voice: str = telephony.DEFAULT_VOICE
    mode: str = "message"
    agent_info: str = ""
    business_name: str = ""
    callback_number: str = ""
    frequency: str = "once"
    call_time: str = "10:00"
    weekday: int = 0
    timezone: str = "America/New_York"
    amd: str = "voicemail"
    require_consent: bool = True
    starts_at: str = ""
    ends_at: str = ""
    start_now: bool = False
    contacts: str = ""
    contacts_consented: bool = False


class CampaignPatch(BaseModel):
    name: str | None = None
    message: str | None = None
    voice: str | None = None
    mode: str | None = None
    agent_info: str | None = None
    business_name: str | None = None
    callback_number: str | None = None
    frequency: str | None = None
    call_time: str | None = None
    weekday: int | None = None
    timezone: str | None = None
    amd: str | None = None
    require_consent: bool | None = None
    starts_at: str | None = None
    ends_at: str | None = None
    state: str | None = None


class ContactsIn(BaseModel):
    raw: str
    consented: bool = False


class ContactPatch(BaseModel):
    consent: bool


class SettingsIn(BaseModel):
    values: dict[str, str]


class VerifyIn(BaseModel):
    # What is typed on the Settings page right now, saved or not - "Test
    # connection" checks those, falling back to the saved value per field.
    values: dict[str, str] = {}


class TestCallIn(BaseModel):
    phone: str = ""
    message: str = ""
    voice: str = telephony.DEFAULT_VOICE
    mode: str = "message"


class CampaignTestIn(BaseModel):
    phone: str = ""


class DraftIn(BaseModel):
    brief: str = ""
    business_name: str = ""
    seconds: int = 25
    tone: str = "warm and direct"
    existing: str = ""


class PreviewIn(BaseModel):
    message: str = ""
    business_name: str = ""
    callback_number: str = ""


class SuppressIn(BaseModel):
    raw: str
    reason: str = "manual"


# --- helpers ----------------------------------------------------------------

def parse_contacts(raw: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Accept pasted lines or CSV: "phone", "phone,name" or "name,phone"."""
    good: list[tuple[str, str]] = []
    bad: list[str] = []
    seen: set[str] = set()

    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("phone", "number", "#")):
            continue
        fields = next(csv.reader([line]), [line])
        fields = [f.strip() for f in fields if f.strip()]
        if not fields:
            continue

        phone, name = "", ""
        for field in fields:
            candidate = compliance.normalize(field)
            if candidate and not phone:
                phone = candidate
            elif not name:
                name = field
        if not phone:
            bad.append(line)
            continue
        if phone in seen:
            continue
        seen.add(phone)
        good.append((phone, name[:80]))
    return good, bad


def campaign_row(row: dict) -> dict:
    counts = db.query_one(
        "SELECT COUNT(*) AS contacts, SUM(consent) AS consented "
        "FROM contacts WHERE campaign_id = ?",
        (row["id"],),
    ) or {}
    # Test calls ("call me with this campaign") are left out: they say nothing
    # about how the campaign itself is doing.
    stats = db.query_one(
        "SELECT COUNT(*) AS total, "
        f"SUM(CASE WHEN {dispatcher.DELIVERED_SQL} THEN 1 ELSE 0 END) AS completed, "
        "SUM(CASE WHEN state = 'done' THEN 1 ELSE 0 END) AS finished, "
        "SUM(CASE WHEN state IN ('pending', 'deferred') THEN 1 ELSE 0 END) AS waiting, "
        "SUM(CASE WHEN state = 'dialing' THEN 1 ELSE 0 END) AS in_flight, "
        "SUM(CASE WHEN state = 'skipped' THEN 1 ELSE 0 END) AS skipped "
        "FROM call_tasks WHERE campaign_id = ? AND run_key <> 'test'",
        (row["id"],),
    ) or {}
    return {
        **row,
        "contacts": counts.get("contacts") or 0,
        "consented": counts.get("consented") or 0,
        "calls_total": stats.get("total") or 0,
        "calls_completed": stats.get("completed") or 0,
        # Calls actually placed and finished - the honest denominator for
        # "delivered", since screened-out rows were never going to be dialled.
        "calls_finished": stats.get("finished") or 0,
        "calls_waiting": stats.get("waiting") or 0,
        "calls_in_flight": stats.get("in_flight") or 0,
        "calls_skipped": stats.get("skipped") or 0,
    }


def _phone_field(raw: str, label: str) -> str:
    """A phone number typed into a form, normalized - or a 400 that says which field."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    phone = compliance.normalize(raw)
    if not phone:
        raise HTTPException(400, f"{label} '{raw}' isn't a phone number that can be dialled. "
                                 "Use the full number with area code, like 617-555-0142.")
    return phone


def _campaign_moment(raw: str, label: str, tz_name: str = "UTC") -> str | None:
    """Parse a `datetime-local` value into the stored UTC string, or None.

    The browser sends wall-clock text with no offset ("2026-10-05T16:00"), and
    it is read in the campaign's own timezone - the same one `call_time` uses.
    Interpreting it as UTC instead would silently shift every window by the
    operator's offset, which is the kind of bug that only shows up as calls
    going out at the wrong hour.
    """
    text = (raw or "").strip()
    if not text:
        return None
    text = text.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        try:
            zone = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 - an unknown zone is reported elsewhere
            zone = timezone.utc
        return db.to_utc(naive.replace(tzinfo=zone))
    raise HTTPException(400, f"{label} timestamp must look like 2026-10-05 16:00.")


def _check_mode(mode: str) -> None:
    if mode not in db.CALL_MODES:
        raise HTTPException(400, f"Call type must be one of {db.CALL_MODES}.")
    if mode == "agent" and not agent.status()["ready"]:
        raise HTTPException(400, "The AI phone agent isn't set up yet - finish "
                                 "Settings > AI phone agent first.")


def _check_agent_info(info: str) -> None:
    if len(info or "") > agent.AGENT_INFO_MAX:
        raise HTTPException(400, f"The extra info for the AI is {len(info):,} characters - keep it "
                                 f"under {agent.AGENT_INFO_MAX:,}. It's sent with every reply, so "
                                 "length adds cost and delay to each call.")


def require_campaign(campaign_id: int) -> dict:
    row = db.query_one("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
    if not row:
        raise HTTPException(404, "No such campaign.")
    return row


# --- dashboard --------------------------------------------------------------

class NoCacheStatic(StaticFiles):
    """Serve the dashboard without letting the browser hold on to a stale copy.

    There is no build step here and no fingerprinted filenames, so an edited
    app.js keeps the same URL. Browsers will then happily reuse the cached copy
    and the dashboard runs code you have already changed - which looks exactly
    like a bug in the new code, and costs an hour before you think of the cache.
    `no-cache` still permits a conditional request, so the usual answer is a
    cheap 304 rather than the file again.
    """

    async def get_response(self, path: str, scope):  # noqa: ANN001
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/overview")
def overview() -> dict:
    today = db.to_utc(db.utcnow() - timedelta(hours=24))
    totals = db.query_one(
        "SELECT COUNT(*) AS total, "
        f"SUM(CASE WHEN {dispatcher.DELIVERED_SQL} THEN 1 ELSE 0 END) AS completed, "
        f"SUM(CASE WHEN {dispatcher.UNANSWERED_SQL} THEN 1 ELSE 0 END) AS unanswered, "
        f"SUM(CASE WHEN {dispatcher.FAILED_SQL} THEN 1 ELSE 0 END) AS failed, "
        "SUM(CASE WHEN state IN ('pending', 'deferred') THEN 1 ELSE 0 END) AS waiting, "
        "SUM(CASE WHEN state = 'dialing' THEN 1 ELSE 0 END) AS in_flight, "
        "SUM(CASE WHEN state = 'skipped' THEN 1 ELSE 0 END) AS skipped, "
        "SUM(CASE WHEN answered_by LIKE 'machine%' THEN 1 ELSE 0 END) AS voicemail "
        "FROM call_tasks"
    ) or {}
    last_day = db.query_one(
        "SELECT COUNT(*) AS n FROM call_tasks WHERE attempts > 0 AND updated_at >= ?",
        (today,),
    ) or {}
    campaigns = db.query_one(
        "SELECT SUM(CASE WHEN state = 'active' THEN 1 ELSE 0 END) AS active, COUNT(*) AS total FROM campaigns"
    ) or {}
    upcoming = db.query(
        "SELECT id, name, next_run_at, frequency, timezone FROM campaigns "
        "WHERE state = 'active' AND next_run_at IS NOT NULL "
        "ORDER BY next_run_at LIMIT 5"
    )

    return {
        "totals": {k: (v or 0) for k, v in totals.items()},
        "calls_24h": last_day.get("n") or 0,
        "campaigns": {k: (v or 0) for k, v in campaigns.items()},
        "upcoming": upcoming,
        "suppressed": len(db.suppression_set()),
        "engine": {**(_engine.health() if _engine else {"running": False}),
                   "wake_up": _wake_status(),
                   "downtime": dispatcher.recent_downtime(db.utcnow())},
        "settings": config.public_view(),
        "events": db.recent_events(12),
        "server_time": db.now_str(),
        # Calls held because their campaign is paused - they go out on Resume.
        "held": (db.query_one(
            "SELECT COUNT(*) AS n FROM call_tasks t JOIN campaigns c ON c.id = t.campaign_id "
            "WHERE t.state IN ('pending', 'deferred') AND c.state = 'paused' "
            "AND t.run_key <> 'test'") or {}).get("n") or 0,
    }


def _wake_status() -> dict:
    global _wake_cache
    if _wake_cache is None or time.monotonic() - _wake_cache[0] > 60:
        _wake_cache = (time.monotonic(), wakeup.status())
    return _wake_cache[1]


# --- campaigns --------------------------------------------------------------

@app.get("/api/campaigns")
def list_campaigns() -> list[dict]:
    rows = db.query("SELECT * FROM campaigns ORDER BY id DESC")
    return [campaign_row(r) for r in rows]


@app.get("/api/campaigns/{campaign_id}")
def get_campaign(campaign_id: int) -> dict:
    return campaign_row(require_campaign(campaign_id))


@app.post("/api/campaigns")
def create_campaign(body: CampaignIn) -> dict:
    if body.frequency not in db.FREQUENCIES:
        raise HTTPException(400, f"Frequency must be one of {db.FREQUENCIES}.")
    if body.amd not in telephony.AMD_MODES:
        raise HTTPException(400, f"Answering-machine mode must be one of {telephony.AMD_MODES}.")
    _check_mode(body.mode)
    _check_agent_info(body.agent_info)
    if not body.message.strip():
        raise HTTPException(400, "A campaign needs a message for the voice to read."
                            if body.mode == "message" else
                            "An AI campaign needs talking points - what the agent should cover.")
    problems = compliance.validate_script(body.message)
    if problems:
        raise HTTPException(400, " ".join(problems))
    if body.timezone not in dispatcher.KNOWN_TIMEZONES:
        raise HTTPException(400, f"Unknown timezone '{body.timezone}'.")
    if not dispatcher.valid_hhmm(body.call_time):
        raise HTTPException(400, f"'{body.call_time}' is not a valid time of day (HH:MM).")
    if not 0 <= body.weekday <= 6:
        raise HTTPException(400, "Weekday must be 0 (Monday) through 6 (Sunday).")
    business = _business_field(body.business_name)
    callback = _phone_field(body.callback_number, "The campaign's callback number")

    starts_at = _campaign_moment(body.starts_at, "Start", body.timezone)
    ends_at = _campaign_moment(body.ends_at, "End", body.timezone)
    if starts_at and ends_at and ends_at <= starts_at:
        raise HTTPException(400, "The end timestamp must be after the start timestamp.")

    now = db.utcnow()
    payload = {**body.model_dump(), "starts_at": starts_at, "ends_at": ends_at}
    first = dispatcher.first_run_at(payload, body.start_now, now)
    if ends_at and first is None:
        raise HTTPException(
            400,
            "That end timestamp leaves no room for a single call - it has already "
            "passed, or it falls before the first run would come round.",
        )
    campaign_id = db.insert(
        "INSERT INTO campaigns (name, message, voice, mode, agent_info, business_name, "
        "callback_number, frequency, call_time, weekday, timezone, state, amd, require_consent, "
        "created_at, starts_at, ends_at, next_run_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
        (
            body.name.strip(), body.message.strip(), body.voice, body.mode,
            body.agent_info.strip() if body.mode == "agent" else "", business, callback,
            body.frequency, body.call_time, body.weekday, body.timezone, body.amd,
            1 if body.require_consent else 0, db.to_utc(now),
            starts_at, ends_at, db.to_utc(first) if first else None,
        ),
    )

    added, invalid = 0, []
    if body.contacts.strip():
        added, invalid = _add_contacts(campaign_id, body.contacts, body.contacts_consented)

    db.log_event(f"Campaign '{body.name.strip()}' created with {added} contact(s).")
    return {
        "id": campaign_id,
        "contacts_added": added,
        # Contacts that will be screened out on every run - worth saying up front.
        "unconsented": added if body.require_consent and not body.contacts_consented else 0,
        "invalid": invalid,
        "campaign": campaign_row(require_campaign(campaign_id)),
    }


def _business_field(raw: str | None) -> str:
    name = (raw or "").strip()
    if len(name) > 120:
        raise HTTPException(400, "The business name is over 120 characters - it's read aloud "
                                 "at the start of every call, so keep it short.")
    return name


@app.patch("/api/campaigns/{campaign_id}")
def patch_campaign(campaign_id: int, body: CampaignPatch) -> dict:
    campaign = require_campaign(campaign_id)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        return campaign_row(campaign)

    if "require_consent" in fields:
        fields["require_consent"] = 1 if fields["require_consent"] else 0
    if fields.get("state") not in (None, *db.CAMPAIGN_STATES):
        raise HTTPException(400, f"State must be one of {db.CAMPAIGN_STATES}.")
    if "frequency" in fields and fields["frequency"] not in db.FREQUENCIES:
        raise HTTPException(400, f"Frequency must be one of {db.FREQUENCIES}.")
    if "amd" in fields and fields["amd"] not in telephony.AMD_MODES:
        raise HTTPException(400, f"Answering-machine mode must be one of {telephony.AMD_MODES}.")
    if "mode" in fields and fields["mode"] != campaign["mode"]:
        _check_mode(fields["mode"])
    elif "mode" in fields and fields["mode"] not in db.CALL_MODES:
        raise HTTPException(400, f"Call type must be one of {db.CALL_MODES}.")
    if "agent_info" in fields:
        _check_agent_info(fields["agent_info"])
        fields["agent_info"] = fields["agent_info"].strip()
    if "timezone" in fields and fields["timezone"] not in dispatcher.KNOWN_TIMEZONES:
        raise HTTPException(400, f"Unknown timezone '{fields['timezone']}'.")
    if "call_time" in fields and not dispatcher.valid_hhmm(fields["call_time"]):
        raise HTTPException(400, f"'{fields['call_time']}' is not a valid time of day (HH:MM).")
    if "weekday" in fields and not 0 <= fields["weekday"] <= 6:
        raise HTTPException(400, "Weekday must be 0 (Monday) through 6 (Sunday).")
    # An empty string here means "clear the window", which is distinct from the
    # field being absent - model_dump() already dropped the absent ones as None.
    zone = fields.get("timezone", campaign["timezone"])
    for key, label in (("starts_at", "Start"), ("ends_at", "End")):
        if key in fields:
            fields[key] = _campaign_moment(fields[key], label, zone)
    window = {
        "starts_at": fields.get("starts_at", campaign["starts_at"]),
        "ends_at": fields.get("ends_at", campaign["ends_at"]),
    }
    if window["starts_at"] and window["ends_at"] and window["ends_at"] <= window["starts_at"]:
        raise HTTPException(400, "The end timestamp must be after the start timestamp.")
    if "message" in fields:
        fields["message"] = fields["message"].strip()
        problems = compliance.validate_script(fields["message"])
        if problems:
            raise HTTPException(400, " ".join(problems))
    if "name" in fields:
        fields["name"] = fields["name"].strip()
        if not fields["name"] or len(fields["name"]) > 120:
            raise HTTPException(400, "A campaign needs a name of up to 120 characters.")
    if "business_name" in fields:
        fields["business_name"] = _business_field(fields["business_name"])
    if "callback_number" in fields:
        fields["callback_number"] = _phone_field(fields["callback_number"],
                                                 "The campaign's callback number")
    if fields.get("mode", campaign["mode"]) != "agent":
        fields.pop("agent_info", None)

    schedule = {"frequency", "call_time", "weekday", "timezone", "starts_at", "ends_at"}
    frequency = fields.get("frequency", campaign["frequency"])
    # Editing a finished campaign onto a recurring schedule brings it back to life;
    # that is the only reason to make a finished campaign recurring.
    if campaign["state"] == "finished" and frequency != "once" and schedule & fields.keys() \
            and "state" not in fields:
        fields["state"] = "active"

    sets = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE campaigns SET {sets} WHERE id = ?", (*fields.values(), campaign_id))

    # A schedule change (or a resume) needs the next run recomputed - the same
    # way a new campaign's first run is, so a start date is still respected.
    note = ""
    updated = require_campaign(campaign_id)
    if updated["state"] == "active" and (schedule | {"state"}) & fields.keys():
        now = db.utcnow()
        if frequency == "once" and updated["runs"]:
            # Already sent. Resuming must NOT silently re-dial the whole list;
            # Run now stays the only way to send it again.
            db.execute("UPDATE campaigns SET state = 'finished', next_run_at = NULL WHERE id = ?",
                       (campaign_id,))
            note = ("This one-off campaign has already gone out, so it's marked finished. "
                    "Use Run now to call the list again.")
        else:
            next_run = dispatcher.first_run_at(updated, False, now)
            if next_run is None:
                db.execute("UPDATE campaigns SET state = 'finished', next_run_at = NULL "
                           "WHERE id = ?", (campaign_id,))
                note = "Its end date has passed, so there's nothing left to schedule."
            else:
                db.execute("UPDATE campaigns SET next_run_at = ? WHERE id = ?",
                           (db.to_utc(next_run), campaign_id))
    return {**campaign_row(require_campaign(campaign_id)), "note": note}


@app.delete("/api/campaigns/{campaign_id}")
def delete_campaign(campaign_id: int) -> dict:
    require_campaign(campaign_id)
    db.execute(
        "UPDATE call_tasks SET state = 'skipped', status = 'campaign deleted' "
        "WHERE campaign_id = ? AND state IN ('pending', 'deferred')",
        (campaign_id,),
    )
    db.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
    return {"ok": True}


@app.post("/api/campaigns/{campaign_id}/run")
def run_campaign(campaign_id: int) -> dict:
    campaign = require_campaign(campaign_id)
    if not campaign["message"].strip():
        raise HTTPException(400, "This campaign has no message to read.")
    if campaign["state"] == "paused":
        # Its calls would be held by the pause anyway - better to say so than to
        # report "Queued 40 calls" that then sit there.
        raise HTTPException(409, "This campaign is paused. Resume it first, then Run now.")
    reasons: dict[str, int] = {}
    queued, skipped = dispatcher.enqueue_campaign(campaign_id, reasons=reasons)
    db.execute(
        "UPDATE campaigns SET last_run_at = ?, runs = runs + 1 WHERE id = ?",
        (db.now_str(), campaign_id),
    )
    note = ""
    if queued and campaign["frequency"] == "once" and campaign["state"] == "active" \
            and campaign["next_run_at"]:
        # Sending a one-off early IS its run. Leaving the slot in place called
        # the whole list a second time when the scheduled moment came round.
        db.execute("UPDATE campaigns SET state = 'finished', next_run_at = NULL WHERE id = ?",
                   (campaign_id,))
        note = "This was its one run, so the scheduled one is cancelled."
    return {"queued": queued, "skipped": skipped, "skip_reasons": reasons, "note": note}


# --- contacts ---------------------------------------------------------------

def _add_contacts(campaign_id: int, raw: str, consented: bool) -> tuple[int, list[str]]:
    good, bad = parse_contacts(raw)
    if good:
        db.executemany(
            "INSERT INTO contacts (campaign_id, phone, name, consent, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(campaign_id, phone) DO UPDATE SET "
            "name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE contacts.name END, "
            # Never downgrade consent. CASE, not MAX(a, b): Postgres has no scalar MAX.
            "consent = CASE WHEN excluded.consent > contacts.consent "
            "THEN excluded.consent ELSE contacts.consent END",
            [
                (campaign_id, phone, name, 1 if consented else 0, db.now_str())
                for phone, name in good
            ],
        )
    return len(good), bad[:20]


@app.get("/api/campaigns/{campaign_id}/contacts")
def list_contacts(campaign_id: int) -> list[dict]:
    require_campaign(campaign_id)
    rows = db.query(
        "SELECT * FROM contacts WHERE campaign_id = ? ORDER BY id", (campaign_id,)
    )
    suppressed = db.suppression_set()
    for row in rows:
        row["pretty"] = compliance.pretty(row["phone"])
        row["timezone"] = compliance.timezone_for(row["phone"]) or "unknown"
        row["suppressed"] = row["phone"] in suppressed
    return rows


@app.post("/api/campaigns/{campaign_id}/contacts")
def add_contacts(campaign_id: int, body: ContactsIn) -> dict:
    require_campaign(campaign_id)
    added, invalid = _add_contacts(campaign_id, body.raw, body.consented)
    return {"added": added, "invalid": invalid}


@app.delete("/api/campaigns/{campaign_id}/contacts/{contact_id}")
def delete_contact(campaign_id: int, contact_id: int) -> dict:
    # Taking someone off the list has to take them out of the queue as well,
    # or a call already waiting for them still goes out.
    cancelled = db.execute(
        "UPDATE call_tasks SET state = 'skipped', status = 'contact removed', updated_at = ? "
        "WHERE contact_id = ? AND campaign_id = ? AND state IN ('pending', 'deferred')",
        (db.now_str(), contact_id, campaign_id),
    )
    db.execute(
        "DELETE FROM contacts WHERE id = ? AND campaign_id = ?", (contact_id, campaign_id)
    )
    return {"ok": True, "cancelled": cancelled}


@app.patch("/api/campaigns/{campaign_id}/contacts/{contact_id}")
def set_contact_consent(campaign_id: int, contact_id: int, body: ContactPatch) -> dict:
    campaign = require_campaign(campaign_id)
    contact = db.query_one("SELECT * FROM contacts WHERE id = ? AND campaign_id = ?",
                           (contact_id, campaign_id))
    if not contact:
        raise HTTPException(404, "No such contact.")
    db.execute("UPDATE contacts SET consent = ? WHERE id = ?", (1 if body.consent else 0, contact_id))
    cancelled = 0
    if not body.consent and campaign["require_consent"]:
        # Withdrawn consent takes them out of the queue too, not just future runs.
        cancelled = db.execute(
            "UPDATE call_tasks SET state = 'skipped', status = 'no-consent', updated_at = ? "
            "WHERE contact_id = ? AND state IN ('pending', 'deferred')",
            (db.now_str(), contact_id))
    db.log_event(f"{compliance.pretty(contact['phone'])} marked as "
                 f"{'having' if body.consent else 'not having'} given consent.")
    return {"ok": True, "cancelled": cancelled}


@app.post("/api/campaigns/{campaign_id}/contacts/consent-all")
def consent_all(campaign_id: int) -> dict:
    campaign = require_campaign(campaign_id)
    changed = db.execute("UPDATE contacts SET consent = 1 WHERE campaign_id = ? AND consent = 0",
                         (campaign_id,))
    if changed:
        db.log_event(f"{changed} contact(s) in '{campaign['name']}' marked as having given consent.")
    return {"changed": changed}


# --- calls ------------------------------------------------------------------

CALL_OUTCOMES = {
    "delivered": dispatcher.DELIVERED_SQL,
    "unanswered": dispatcher.UNANSWERED_SQL,
    "failed": dispatcher.FAILED_SQL,
    "waiting": "state IN ('pending', 'deferred', 'dialing')",
    "skipped": "state = 'skipped'",
    "opted-out": "status = 'opted-out'",
}


def _calls(campaign_id: int | None, outcome: str, q: str, before_id: int | None,
           limit: int) -> list[dict]:
    where, params = [], []
    if campaign_id:
        where.append("t.campaign_id = ?")
        params.append(campaign_id)
    if outcome == "test":
        where.append("(t.campaign_id IS NULL OR t.run_key IN ('manual', 'test'))")
    elif outcome in CALL_OUTCOMES:
        where.append("(" + CALL_OUTCOMES[outcome].replace("state", "t.state")
                     .replace("status", "t.status") + ")")
    digits = "".join(ch for ch in (q or "") if ch.isdigit())
    if digits:
        where.append("t.phone LIKE ?")
        params.append(f"%{digits}%")
    if before_id:
        where.append("t.id < ?")
        params.append(before_id)
    rows = db.query(
        "SELECT t.*, c.name AS campaign_name, c.state AS campaign_state FROM call_tasks t "
        "LEFT JOIN campaigns c ON c.id = t.campaign_id "
        + ("WHERE " + " AND ".join(where) + " " if where else "")
        + "ORDER BY t.id DESC LIMIT ?",
        (*params, limit),
    )
    for row in rows:
        row["pretty"] = compliance.pretty(row["phone"])
        # So the dashboard can show a scheduled call in the recipient's own time.
        row["recipient_tz"] = compliance.timezone_for(row["phone"]) or ""
        row["is_test"] = dispatcher.is_test(row)
        row.pop("token", None)
    return rows


@app.get("/api/calls")
def list_calls(campaign_id: int | None = None, limit: int = 200, outcome: str = "",
               q: str = "", before_id: int | None = None) -> list[dict]:
    return _calls(campaign_id, outcome, q, before_id, max(1, min(limit, 1000)))


@app.get("/api/calls.csv", include_in_schema=False)
def export_calls(campaign_id: int | None = None, outcome: str = "", q: str = "") -> Response:
    rows = _calls(campaign_id, outcome, q, None, 100_000)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["id", "updated_utc", "scheduled_utc", "campaign", "phone", "state", "status",
                     "answered_by", "duration_s", "telnyx_sid", "error"])
    for r in rows:
        campaign = r["campaign_name"] or ("Test call" if r["is_test"] else "Deleted campaign")
        writer.writerow([r["id"], r["updated_at"], r["scheduled_for"], campaign, r["phone"],
                         r["state"], r["status"], r["answered_by"], r["duration"], r["sid"],
                         r["error"]])
    stamp = db.utcnow().strftime("%Y%m%d-%H%M")
    return Response(out.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="call-log-{stamp}.csv"'})


def _test_number(raw: str) -> str:
    phone = _phone_field(raw, "The number to call") if (raw or "").strip() \
        else compliance.normalize(config.get("test_number"))
    if not phone:
        raise HTTPException(400, "Type a number to call, or save your own number as "
                                 "'My test number' on the Settings page.")
    if config.telephony() is None:
        raise HTTPException(400, "Telnyx isn't configured yet - fill in Settings.")
    # A test call to a blocked number would be skipped at dial time and never
    # ring - which looks exactly like the app being broken.
    blocked = db.suppression_row(phone)
    if blocked:
        raise HTTPException(400, f"{compliance.pretty(phone)} is on your do-not-call list "
                                 f"({blocked['reason']}), so it can't be called - not even for "
                                 "a test. Remove it on the Do not call page first.")
    return phone


def _place_test(task_id: int) -> dict:
    # Restricted to this one task: pressing a test button must never also dial
    # whoever is at the front of a campaign queue. The calling window is skipped
    # because the operator is deliberately calling their own number right now.
    placed = dispatcher.dispatch_pending(only_task_id=task_id, ignore_window=True)
    task = db.query_one("SELECT * FROM call_tasks WHERE id = ?", (task_id,))
    if task:
        task.pop("token", None)
    return {"task_id": task_id, "placed": placed, "task": task}


@app.post("/api/test-call")
def test_call(body: TestCallIn) -> dict:
    phone = _test_number(body.phone)
    _check_mode(body.mode)
    voice = body.voice if body.voice in telephony.VOICE_IDS else telephony.DEFAULT_VOICE
    return _place_test(dispatcher.enqueue_single(phone, body.message.strip(), voice, body.mode))


@app.post("/api/campaigns/{campaign_id}/test-call")
def test_campaign(campaign_id: int, body: CampaignTestIn) -> dict:
    """Ring the operator with exactly this campaign's call - its script, voice,
    call type and caller identity - before it goes to the list."""
    campaign = require_campaign(campaign_id)
    phone = _test_number(body.phone)
    _check_mode(campaign["mode"])
    return _place_test(dispatcher.enqueue_single(phone, "", "", campaign["mode"],
                                                 campaign_id=campaign_id))


# --- scripts ----------------------------------------------------------------

@app.post("/api/script/preview")
def preview_script(body: PreviewIn) -> dict:
    # The campaign's own identity if the form has one, as the call will use it -
    # and the keypress opt-out only where webhook mode can actually take it.
    final = compliance.build_script(
        body.message,
        business_name=body.business_name.strip() or config.get("business_name"),
        callback_number=compliance.normalize(body.callback_number) or config.get("callback_number"),
        press_nine=bool(config.get("public_base_url")),
    )
    return {
        "script": final,
        "seconds": compliance.estimate_seconds(final),
        "characters": len(final),
        "problems": compliance.validate_script(final),
    }


@app.post("/api/script/draft")
def draft_script(body: DraftIn) -> dict:
    try:
        text = scriptwriter.draft(
            brief=body.brief,
            business_name=body.business_name or config.get("business_name"),
            seconds=max(10, min(body.seconds, 90)),
            tone=body.tone,
            existing=body.existing,
        )
    except scriptwriter.ScriptError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"message": text, "seconds": compliance.estimate_seconds(text)}


# --- settings & suppression -------------------------------------------------

@app.get("/api/settings")
def get_settings() -> dict:
    return {
        "settings": config.public_view(),
        "voices": [{"id": v, "label": label} for v, label in telephony.VOICES],
        "frequencies": list(db.FREQUENCIES),
        "amd_modes": list(telephony.AMD_MODES),
        "timezones": list(dispatcher.KNOWN_TIMEZONES),
        "database": db.where(),
        "script_ai": scriptwriter.available(),
        "agent": agent.status(),
    }


@app.post("/api/agent/setup")
def setup_agent() -> dict:
    """Create or update the Telnyx assistant. Writes to the Telnyx account."""
    phone = config.telephony()
    if phone is None:
        raise HTTPException(400, "Enter the Telnyx credentials first.")
    assistant_id, error = agent.setup(phone)
    if error:
        raise HTTPException(400, error)
    db.log_event(f"AI phone agent set up in Telnyx ({assistant_id}).")
    return agent.status()


PHONE_SETTINGS = {"test_number": "My test number", "callback_number": "The opt-out callback number",
                  "telnyx_from_number": "The 'call from' number"}


def _validate_settings(values: dict[str, str]) -> dict[str, str]:
    """Refuse values that would save fine and then quietly break calling."""
    for key, label in PHONE_SETTINGS.items():
        if key in values:
            values[key] = _phone_field(values[key], label)
    for key in ("window_start", "window_end"):
        if key in values and not dispatcher.valid_hhmm(values[key]):
            raise HTTPException(400, f"'{values[key]}' isn't a time of day - use HH:MM, like 09:00.")
    start = values.get("window_start", config.get("window_start"))
    end = values.get("window_end", config.get("window_end"))
    if dispatcher.parse_hhmm(start) >= dispatcher.parse_hhmm(end):
        raise HTTPException(400, f"The calling window {start}-{end} is empty: 'Don't call before' has "
                                 "to be earlier than 'Don't call after', or nothing is ever dialled.")
    for key, label, low, high in (("calls_per_minute", "Calls per minute", 1, 120),
                                  # Telnyx refuses a ring timeout over 120.
                                  ("ring_seconds", "Ring for", 10, 120)):
        if key in values:
            try:
                number = int(str(values[key]).strip())
            except ValueError:
                raise HTTPException(400, f"{label} must be a whole number.") from None
            if not low <= number <= high:
                raise HTTPException(400, f"{label} must be between {low} and {high}.")
            values[key] = str(number)
    if values.get("public_base_url"):
        url = values["public_base_url"].strip().rstrip("/")
        parsed = urlparse(url)
        local = parsed.hostname in ("localhost", "127.0.0.1")
        if not parsed.netloc or not (parsed.scheme == "https" or (parsed.scheme == "http" and local)):
            raise HTTPException(400, "The public base URL must be the https:// address of this "
                                     "server, like https://robocaller.onrender.com.")
        values["public_base_url"] = url
    if "business_name" in values:
        values["business_name"] = _business_field(values["business_name"])
    if values.get("agent_llm") not in (None, *agent.ROUTES):
        raise HTTPException(400, f"The agent's AI must run on one of {agent.ROUTES}.")
    if values.get("script_provider") not in (None, "", "anthropic", "perplexity"):
        raise HTTPException(400, "Scripts can be written by 'anthropic' or 'perplexity'.")
    if values.get("dispatch_paused") not in (None, "0", "1"):
        raise HTTPException(400, "dispatch_paused must be 0 or 1.")
    return values


@app.post("/api/settings")
def save_settings(body: SettingsIn) -> dict:
    values = {k: v for k, v in body.values.items() if k in config.DEFAULTS}
    # An empty secret means "leave it alone", not "erase it".
    for key in config.SECRET_KEYS:
        if key in values and not values[key].strip():
            values.pop(key)
    config.set_many(_validate_settings(values))
    return config.public_view()


def _inbound_url() -> str:
    base = config.get("public_base_url").rstrip("/")
    return f"{base}/telnyx/inbound" if base else ""


@app.post("/api/settings/verify")
def verify_settings(body: VerifyIn | None = None) -> dict:
    # Whatever is typed in the form right now, falling back to what's saved -
    # checking the saved values while the person is looking at different ones
    # gave a green tick to a number they hadn't saved.
    typed = {k: (v or "").strip() for k, v in ((body.values if body else {}) or {}).items()}

    def pick(key: str) -> str:
        return typed.get(key) or config.get(key).strip()

    from_number = compliance.normalize(pick("telnyx_from_number")) or pick("telnyx_from_number")
    if not (pick("telnyx_api_key") and pick("telnyx_app_id") and from_number):
        raise HTTPException(400, "Enter the Telnyx API key, TeXML application ID and From number first.")
    phone = telephony.Telephony(pick("telnyx_api_key"), pick("telnyx_app_id"), from_number,
                                pick("public_base_url"))
    ok, detail = phone.verify()
    numbers, error = phone.owned_numbers() if ok else ([], "")
    from_ok, from_detail = phone.check_from_number() if ok else (False, "")
    limits = phone.account_limits() if ok else {}
    expected = (pick("public_base_url").rstrip("/") + "/telnyx/inbound") if pick("public_base_url") else ""
    return {
        "ok": ok, "detail": detail, "numbers": numbers, "error": error,
        "mode": phone.mode, "from_ok": from_ok, "from_detail": from_detail,
        "limits": limits,
        "inbound_ready": bool(expected) and limits.get("inbound_url") == expected,
        "unsaved": sorted(k for k, v in typed.items() if v and v != config.get(k)),
    }


@app.get("/api/inbound")
def inbound_status() -> dict:
    """Where calls to the caller-ID number go. Asks Telnyx, so only on demand."""
    phone = config.telephony()
    expected = _inbound_url()
    if phone is None:
        return {"ready": False, "can_setup": False, "url": "", "expected": expected,
                "missing": ["Telnyx credentials"]}
    limits = phone.account_limits()
    missing = []
    if not expected:
        missing.append("a public base URL (webhook mode)")
    if not _clean_key(config.get("telnyx_public_key")):
        missing.append("the Telnyx public key, so callers' keypresses can be trusted")
    return {"ready": bool(expected) and limits.get("inbound_url") == expected,
            "can_setup": not missing, "missing": missing,
            "url": limits.get("inbound_url", ""), "expected": expected,
            "number": config.get("telnyx_from_number")}


@app.post("/api/inbound/setup")
def setup_inbound() -> dict:
    """Point the caller-ID number's inbound calls at this server. Writes to Telnyx."""
    status = inbound_status()
    if not status["can_setup"]:
        raise HTTPException(400, "Missing " + "; ".join(status["missing"]) + ".")
    error = config.telephony().set_inbound_url(status["expected"])
    if error:
        raise HTTPException(400, f"Telnyx refused the change: {error}")
    db.log_event(f"Calls to {compliance.pretty(status['number'])} now reach the callback line "
                 "(press 9 to opt out, or leave a message).")
    return inbound_status()


@app.get("/api/suppression")
def get_suppression() -> list[dict]:
    rows = db.list_suppressed()
    for row in rows:
        row["pretty"] = compliance.pretty(row["phone"])
    return rows


@app.post("/api/suppression")
def add_suppression(body: SuppressIn) -> dict:
    good, bad = parse_contacts(body.raw)
    for phone, _ in good:
        db.suppress(phone, body.reason)
    if good:
        db.log_event(f"{len(good)} number(s) added to the do-not-call list.")
    return {"added": len(good), "invalid": bad}


@app.delete("/api/suppression/{phone}")
def remove_suppression(phone: str) -> dict:
    removed = db.unsuppress(compliance.normalize(phone) or phone)
    if removed:
        # An opt-out is a legal record; taking one off the list leaves a trace.
        db.log_event(f"{compliance.pretty(removed['phone'])} was removed from the do-not-call list "
                     f"(it had been added {removed['created_at']} UTC: {removed['reason']}).", "warn")
    return {"ok": True, "removed": bool(removed)}


# --- documents --------------------------------------------------------------

def _document(name: str) -> Path:
    """Resolve a requested filename to a PDF that really is inside docs/.

    `name` arrives from the URL, so it is treated as hostile: a bare filename is
    required, the suffix is checked, and the resolved path has to still be a
    direct child of DOCS_DIR. That last check is the one that matters - it
    defeats `..`, absolute paths and symlinks pointing out of the folder, which
    the string checks alone would not.
    """
    if name != Path(name).name or not name.lower().endswith(".pdf"):
        raise HTTPException(404, "No such document.")
    path = (DOCS_DIR / name).resolve()
    if path.parent != DOCS_DIR.resolve() or not path.is_file():
        raise HTTPException(404, "No such document.")
    return path


@app.get("/api/documents")
def list_documents() -> list[dict]:
    """The PDFs sitting in docs/, newest first."""
    if not DOCS_DIR.is_dir():
        return []
    out = []
    for path in DOCS_DIR.glob("*.pdf"):
        stat = path.stat()
        out.append({
            "name": path.name,
            # Filenames are written as words joined by hyphens, so this reads
            # back as a title without needing to open the file.
            "title": path.stem.replace("-", " "),
            "bytes": stat.st_size,
            "modified": db.to_utc(datetime.fromtimestamp(stat.st_mtime, timezone.utc)),
            "url": f"/docs/{path.name}",
        })
    out.sort(key=lambda d: d["modified"], reverse=True)
    return out


@app.get("/docs/{name}")
def get_document(name: str) -> FileResponse:
    path = _document(name)
    # `inline` so the browser's own PDF viewer renders it in the iframe rather
    # than offering it as a download.
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{path.name}"'},
    )


# --- Telnyx webhooks (webhook mode only) ------------------------------------

# Telnyx signs with Ed25519 over "{timestamp}|{raw body}" and rejects nothing on
# its own end, so the freshness window is ours to enforce. Five minutes is what
# Telnyx's own examples use.
SIGNATURE_TOLERANCE_SECONDS = 5 * 60


def _validate(request: Request, body: bytes) -> None:
    """Reject anything that isn't actually from Telnyx.

    These endpoints can add a number to the do-not-call list, so they are not
    left open. Two independent gates cover them: the unguessable per-call token
    already in the path, and - once a public key is configured in Settings -
    Telnyx's signature over the raw body. Signature checking is skipped only
    when no public key has been entered, which mirrors how this behaved on
    Twilio when the auth token was absent.
    """
    public_key = _clean_key(config.get("telnyx_public_key"))
    if not public_key:
        return

    signature = request.headers.get("telnyx-signature-ed25519", "")
    timestamp = request.headers.get("telnyx-timestamp", "")
    if not signature or not timestamp:
        raise HTTPException(403, "Missing Telnyx signature headers.")

    try:
        age = abs(time.time() - float(timestamp))
    except ValueError as exc:
        raise HTTPException(403, "Malformed Telnyx timestamp.") from exc
    if age > SIGNATURE_TOLERANCE_SECONDS:
        raise HTTPException(403, "Telnyx signature is too old.")

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key))
    except Exception as exc:  # noqa: BLE001
        _warn_once("key", "TELNYX_PUBLIC_KEY isn't a valid Telnyx public key, so every Telnyx "
                          "webhook is being refused and recorded-message calls go silent. Copy "
                          "it again from Mission Control > Keys & Credentials > Public Key.")
        raise HTTPException(403, "TELNYX_PUBLIC_KEY is not a valid public key.") from exc
    try:
        key.verify(base64.b64decode(signature), b"%s|%s" % (timestamp.encode(), body))
    except Exception as exc:  # noqa: BLE001 - any failure here means "not Telnyx"
        _warn_once("signature", "A Telnyx webhook failed its signature check. If calls are going "
                                "silent, TELNYX_PUBLIC_KEY doesn't match the public key in Telnyx "
                                "Mission Control > Keys & Credentials.")
        raise HTTPException(403, "Bad Telnyx signature.") from exc


def _clean_key(raw: str) -> str:
    """The public key as pasted, made decodable.

    Base64 padding goes missing in transit - Render's .env import dropped this
    key's trailing "=", which made every webhook fail - and pasted values often
    carry quotes. Both are restored or stripped rather than failing every call.
    """
    key = (raw or "").strip().strip('"').strip("'").strip()
    return key + "=" * (-len(key) % 4) if key else ""


_warned: set[str] = set()


def _warn_once(kind: str, message: str) -> None:
    """Put a webhook failure in the Activity feed - once per process, since a
    single call sends several webhooks and each would otherwise log it again."""
    if kind not in _warned:
        _warned.add(kind)
        db.log_event(message, "error")


async def _signed_form(request: Request) -> dict:
    """The POST body as a dict, once its signature has been checked.

    The raw bytes are read first because the signature covers exactly those
    bytes - and parsed here rather than with Starlette's `request.form()`, which
    needs the separate python-multipart package. It wasn't in requirements.txt,
    so on Render every webhook crashed with a 500 once signatures started
    passing, and Telnyx answered recorded-message calls with "There was an
    error that occurred, goodbye". Telnyx sends plain urlencoded forms.
    """
    body = await request.body()
    _validate(request, body)
    return dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))


def _task_for(token: str) -> dict:
    # `amd` is needed by the voice webhook to decide whether to hang up on a
    # machine; a test call has no campaign, so every joined column can be NULL.
    task = db.query_one(
        "SELECT t.*, c.message, c.voice, c.amd, c.business_name AS campaign_business, "
        "c.callback_number AS campaign_callback FROM call_tasks t "
        "LEFT JOIN campaigns c ON c.id = t.campaign_id WHERE t.token = ?",
        (token,),
    )
    if not task:
        raise HTTPException(404, "Unknown call token.")
    return task


@app.post("/telnyx/voice/{token}")
async def texml_for_call(token: str, request: Request) -> Response:
    form = await _signed_form(request)
    task = _task_for(token)

    # 'Live answers only' - drop the call if a machine picked up.
    answered_by = str(form.get("AnsweredBy", ""))
    if task.get("amd") == "live_only" and answered_by.startswith("machine"):
        db.execute(
            "UPDATE call_tasks SET answered_by = ?, status = 'machine-skipped', "
            "updated_at = ? WHERE id = ?",
            (answered_by, db.now_str(), task["id"]),
        )
        return Response(telephony.build_texml("", hangup_first=True), media_type="text/xml")

    base = config.get("public_base_url").rstrip("/")
    script = compliance.build_script(
        task.get("script_override") or task.get("message") or dispatcher._fallback_message(),
        business_name=task.get("campaign_business") or config.get("business_name"),
        callback_number=task.get("campaign_callback") or config.get("callback_number"),
        press_nine=bool(base),
    )
    xml = telephony.build_texml(
        script,
        voice=task.get("voice_override") or task.get("voice")
              or telephony.DEFAULT_VOICE,
        optout_url=f"{base}/telnyx/optout/{token}" if base else "",
    )
    # The message is about to play - the call log may now say "Delivered".
    db.execute("UPDATE call_tasks SET played = 1 WHERE id = ?", (task["id"],))
    return Response(xml, media_type="text/xml")


@app.post("/telnyx/optout/{token}")
async def handle_optout(token: str, request: Request) -> Response:
    form = await _signed_form(request)
    task = _task_for(token)

    if str(form.get("Digits", "")) == "9":
        db.suppress(task["phone"], "opted out by keypress")
        db.log_event(f"{compliance.pretty(task['phone'])} opted out during a call.")
        db.execute(
            "UPDATE call_tasks SET status = 'opted-out', updated_at = ? WHERE id = ?",
            (db.now_str(), task["id"]),
        )
        return Response(
            "<Response><Say>You have been removed from our calling list. Goodbye."
            "</Say><Hangup/></Response>",
            media_type="text/xml",
        )
    return Response("<Response><Hangup/></Response>", media_type="text/xml")


# --- the callback line: people calling the caller-ID number back -------------
#
# Every recorded message without a callback number tells people to call the
# number on their caller ID, and plenty redial a missed call anyway. Without
# this the TeXML application's inbound URL pointed nowhere, so an opt-out by
# phone - the one route a voicemail recipient has - went unanswered.

def _texml(body: str) -> Response:
    return Response(f"<Response>{body}</Response>", media_type="text/xml")


def _caller(form: dict) -> str:
    return compliance.normalize(str(form.get("From", "")))


def _business_for_caller(phone: str) -> str:
    """The name on the last campaign that called this number, else the default."""
    row = db.query_one(
        "SELECT c.business_name FROM call_tasks t JOIN campaigns c ON c.id = t.campaign_id "
        "WHERE t.phone = ? AND t.attempts > 0 ORDER BY t.id DESC LIMIT 1", (phone,)) if phone else None
    return (row or {}).get("business_name") or config.get("business_name") or "us"


async def _signed_inbound(request: Request) -> dict:
    """Like _signed_form, but never unsigned. These requests carry no per-call
    token, and one of them adds the caller to the do-not-call list - so without a
    public key to check Telnyx's signature against, they are refused outright."""
    if not _clean_key(config.get("telnyx_public_key")):
        raise HTTPException(403, "The callback line needs TELNYX_PUBLIC_KEY to verify Telnyx.")
    body = await request.body()
    _validate(request, body)
    if body.lstrip().startswith(b"{"):
        # Some Telnyx callbacks (transcriptions) can arrive as JSON events.
        try:
            data = json.loads(body)
        except ValueError:
            return {}
        inner = data.get("data") if isinstance(data, dict) else None
        payload = inner.get("payload") if isinstance(inner, dict) else None
        source = payload if isinstance(payload, dict) else data if isinstance(data, dict) else {}
        return {k: v for k, v in source.items() if isinstance(v, (str, int, float))}
    return dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))


def _inbound_say(text: str) -> str:
    return f'<Say voice="{telephony.DEFAULT_VOICE}">{escape(text)}</Say>'


@app.post("/telnyx/inbound")
async def inbound_call(request: Request) -> Response:
    form = await _signed_inbound(request)
    base = config.get("public_base_url").rstrip("/")
    business = _business_for_caller(_caller(form))
    menu = (f"Thanks for calling {business}. You may have had an automated call from us. "
            "To be removed from our calling list, press 9. To leave a message, press 1, "
            "or just stay on the line.")
    return _texml(
        '<Pause length="1"/>'
        f'<Gather numDigits="1" timeout="6" method="POST" action={quoteattr(base + "/telnyx/inbound/choice")}>'
        f"{_inbound_say(menu)}</Gather>"
        f'<Redirect method="POST">{escape(base + "/telnyx/inbound/message")}</Redirect>'
    )


@app.post("/telnyx/inbound/choice")
async def inbound_choice(request: Request) -> Response:
    form = await _signed_inbound(request)
    phone = _caller(form)
    if str(form.get("Digits", "")) == "9" and phone:
        db.suppress(phone, "opted out by calling back")
        db.log_event(f"{compliance.pretty(phone)} called back and opted out - added to the "
                     "do-not-call list.")
        return _texml(_inbound_say("You've been removed from our calling list, and you won't "
                                   "be called again. Goodbye.") + "<Hangup/>")
    return await _inbound_message_texml()


@app.post("/telnyx/inbound/message")
async def inbound_message(request: Request) -> Response:
    await _signed_inbound(request)
    return await _inbound_message_texml()


async def _inbound_message_texml() -> Response:
    base = config.get("public_base_url").rstrip("/")
    return _texml(
        _inbound_say("Please leave a message after the tone. Press pound when you're done.")
        + f'<Record maxLength="120" timeout="5" finishOnKey="#" playBeep="true" '
          f'trim="trim-silence" method="POST" action={quoteattr(base + "/telnyx/inbound/recorded")} '
          f'transcription="true" transcriptionCallback={quoteattr(base + "/telnyx/inbound/transcript")}/>'
        + _inbound_say("Goodbye.") + "<Hangup/>"
    )


@app.post("/telnyx/inbound/recorded")
async def inbound_recorded(request: Request) -> Response:
    form = await _signed_inbound(request)
    seconds = str(form.get("RecordingDuration") or "").strip()
    db.log_event(f"Voicemail from {compliance.pretty(_caller(form)) or 'an unknown caller'}"
                 + (f" ({seconds}s)" if seconds else "")
                 + " - listen in Telnyx Mission Control > Recordings.", "warn")
    return _texml(_inbound_say("Thanks, we got your message. Goodbye.") + "<Hangup/>")


@app.post("/telnyx/inbound/transcript")
async def inbound_transcript(request: Request) -> Response:
    form = await _signed_inbound(request)
    text = next((str(form[k]) for k in ("TranscriptionText", "transcription_text", "Transcription")
                 if form.get(k)), "").strip()
    if text:
        db.log_event(f"Voicemail from {compliance.pretty(_caller(form)) or 'an unknown caller'}: "
                     f"\"{text[:400]}\"", "warn")
    return PlainTextResponse("", status_code=204)


@app.post("/telnyx/status/{token}")
async def handle_status(token: str, request: Request) -> Response:
    form = await _signed_form(request)
    task = _task_for(token)
    dispatcher.record_status(
        task["id"],
        str(form.get("CallStatus", "")),
        str(form.get("AnsweredBy", "")),
        int(form.get("CallDuration") or 0),
    )
    return PlainTextResponse("", status_code=204)


# --- the AI phone agent's model endpoint and tools ---------------------------

def _agent_authorized(presented: str) -> bool:
    token = agent.proxy_token()
    return bool(token) and hmac.compare_digest(presented.encode(), token.encode())


def _llm_error(status: int, message: str) -> JSONResponse:
    # OpenAI's error shape, since the caller is an OpenAI-compatible client.
    return JSONResponse({"error": {"message": message, "type": "invalid_request_error",
                                   "code": status}}, status_code=status)


def _bearer(request: Request) -> str:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


@app.get("/llm/v1/models")
def llm_models(request: Request) -> JSONResponse:
    if not _agent_authorized(_bearer(request)):
        return _llm_error(401, "Invalid or missing bearer token.")
    return JSONResponse({"object": "list", "data": [
        {"id": agent.model(), "object": "model", "owned_by": "perplexity"}]})


@app.post("/llm/v1/chat/completions")
async def llm_chat(request: Request) -> Response:
    """Telnyx's AI assistant asks here what to say next; Perplexity answers."""
    if not agent.proxy_token():
        return _llm_error(503, "AGENT_PROXY_TOKEN is not set on this server.")
    if not _agent_authorized(_bearer(request)):
        return _llm_error(401, "Invalid or missing bearer token.")
    key = config.get("perplexity_api_key")
    if not key:
        return _llm_error(503, "No Perplexity API key is configured.")
    try:
        body = await request.json()
    except ValueError:
        return _llm_error(400, "Request body must be JSON.")

    upstream = llm_proxy.to_agent_request(body, agent.model())
    model = upstream["model"]
    headers = {"Authorization": f"Bearer {key}"}
    client = llm_proxy.client()
    started = time.monotonic()

    if not upstream.get("stream"):
        try:
            res = await client.post(llm_proxy.AGENT_URL, headers=headers, json=upstream)
        except httpx.HTTPError as exc:
            return _llm_error(502, f"Could not reach Perplexity: {exc}")
        if res.status_code != 200:
            return _llm_error(502, f"Perplexity returned HTTP {res.status_code}: {res.text[:300]}")
        result = res.json()
        llm_proxy.log_turn(model, started, result.get("usage"))
        return JSONResponse(llm_proxy.to_chat_completion(result, model))

    # Open the upstream stream before answering, so a Perplexity failure comes
    # back as a real HTTP error rather than a 200 that goes quiet.
    try:
        res = await client.send(client.build_request(
            "POST", llm_proxy.AGENT_URL, headers=headers, json=upstream), stream=True)
    except httpx.HTTPError as exc:
        return _llm_error(502, f"Could not reach Perplexity: {exc}")
    if res.status_code != 200:
        detail = (await res.aread()).decode("utf-8", "replace")[:300]
        await res.aclose()
        return _llm_error(502, f"Perplexity returned HTTP {res.status_code}: {detail}")

    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

    async def relay():
        usage: dict = {}

        async def events():
            async for event in llm_proxy.agent_events(res.aiter_lines()):
                if event.get("type") == "response.completed":
                    usage.update((event.get("response") or {}).get("usage") or {})
                yield event

        try:
            async for line in llm_proxy.translate_stream(events(), model, include_usage):
                yield line
        finally:
            await res.aclose()
            llm_proxy.log_turn(model, started, usage)

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.post("/telnyx/agent/optout/{token}")
async def agent_optout(token: str, request: Request) -> JSONResponse:
    """The agent's add_to_do_not_call tool. Telnyx sends the shared token in a
    header; the call token in the path says whose number to block."""
    if not _agent_authorized(request.headers.get("x-agent-token", "")):
        raise HTTPException(403, "Bad agent token.")
    task = _task_for(token)
    db.suppress(task["phone"], "opted out on an AI call")
    db.execute("UPDATE call_tasks SET status = 'opted-out', updated_at = ? WHERE id = ?",
               (db.now_str(), task["id"]))
    db.log_event(f"{compliance.pretty(task['phone'])} asked the AI agent not to call again "
                 "- added to the do-not-call list.")
    return JSONResponse({"ok": True, "result": "Added to the do-not-call list. "
                                               "They will not be called again."})


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.api_route(
    "/api/{rest:path}",
    methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    include_in_schema=False,
)
def api_not_found(rest: str) -> JSONResponse:
    """Anything under /api that matched no route above.

    Without this the catch-all static mount answers with the dashboard's HTML,
    and the browser's `JSON.parse` fails with a syntax error that says nothing
    about the real problem.
    """
    raise HTTPException(404, f"No such API endpoint: /api/{rest}")


app.mount("/", NoCacheStatic(directory=str(WEB_DIR), html=True), name="web")
