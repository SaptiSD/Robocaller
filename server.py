"""RoboCall AI - web server.

FastAPI serves the dashboard, a small JSON API, and the Twilio webhooks. The
dispatcher runs as a daemon thread in this process, so calls go out whether or
not a browser is open - closing the tab does not stop a campaign.

Run it:  python -m uvicorn server:app --port 8000
"""
from __future__ import annotations

import csv
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import compliance
import config
import db
import dispatcher
import scriptwriter
import telephony

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("robocall")

WEB_DIR = Path(__file__).parent / "web"
_engine: dispatcher.Dispatcher | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine
    archived, migrated = db.init()
    log.info("database: %s", db.DB_PATH)
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


# --- request models ---------------------------------------------------------

class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    message: str = ""
    voice: str = telephony.DEFAULT_VOICE
    frequency: str = "once"
    call_time: str = "10:00"
    weekday: int = 0
    timezone: str = "America/New_York"
    amd: str = "voicemail"
    require_consent: bool = True
    start_now: bool = False
    contacts: str = ""
    contacts_consented: bool = False


class CampaignPatch(BaseModel):
    name: str | None = None
    message: str | None = None
    voice: str | None = None
    frequency: str | None = None
    call_time: str | None = None
    weekday: int | None = None
    timezone: str | None = None
    amd: str | None = None
    require_consent: bool | None = None
    state: str | None = None


class ContactsIn(BaseModel):
    raw: str
    consented: bool = False


class SettingsIn(BaseModel):
    values: dict[str, str]


class TestCallIn(BaseModel):
    phone: str = ""
    message: str = ""
    voice: str = telephony.DEFAULT_VOICE


class DraftIn(BaseModel):
    brief: str = ""
    business_name: str = ""
    seconds: int = 25
    tone: str = "warm and direct"
    existing: str = ""


class PreviewIn(BaseModel):
    message: str = ""


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
    stats = db.query_one(
        "SELECT COUNT(*) AS total, "
        "SUM(state = 'done' AND status = 'completed') AS completed, "
        "SUM(state IN ('pending', 'deferred')) AS waiting, "
        "SUM(state = 'dialing') AS in_flight "
        "FROM call_tasks WHERE campaign_id = ?",
        (row["id"],),
    ) or {}
    return {
        **row,
        "contacts": counts.get("contacts") or 0,
        "consented": counts.get("consented") or 0,
        "calls_total": stats.get("total") or 0,
        "calls_completed": stats.get("completed") or 0,
        "calls_waiting": stats.get("waiting") or 0,
        "calls_in_flight": stats.get("in_flight") or 0,
    }


def require_campaign(campaign_id: int) -> dict:
    row = db.query_one("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
    if not row:
        raise HTTPException(404, "No such campaign.")
    return row


# --- dashboard --------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/overview")
def overview() -> dict:
    today = db.to_utc(db.utcnow() - timedelta(hours=24))
    totals = db.query_one(
        "SELECT COUNT(*) AS total, "
        "SUM(state = 'done' AND status = 'completed') AS completed, "
        "SUM(state = 'done' AND status IN ('busy', 'no-answer')) AS unanswered, "
        "SUM(state = 'done' AND status IN ('failed', 'canceled', 'unknown')) AS failed, "
        "SUM(state IN ('pending', 'deferred')) AS waiting, "
        "SUM(state = 'dialing') AS in_flight, "
        "SUM(state = 'skipped') AS skipped, "
        "SUM(answered_by LIKE 'machine%') AS voicemail "
        "FROM call_tasks"
    ) or {}
    last_day = db.query_one(
        "SELECT COUNT(*) AS n FROM call_tasks WHERE attempts > 0 AND updated_at >= ?",
        (today,),
    ) or {}
    campaigns = db.query_one(
        "SELECT SUM(state = 'active') AS active, COUNT(*) AS total FROM campaigns"
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
        "engine": _engine.health() if _engine else {"running": False},
        "settings": config.public_view(),
        "events": db.recent_events(12),
        "server_time": db.now_str(),
    }


# --- campaigns --------------------------------------------------------------

@app.get("/api/campaigns")
def list_campaigns() -> list[dict]:
    rows = db.query("SELECT * FROM campaigns ORDER BY id DESC")
    return [campaign_row(r) for r in rows]


@app.post("/api/campaigns")
def create_campaign(body: CampaignIn) -> dict:
    if body.frequency not in db.FREQUENCIES:
        raise HTTPException(400, f"Frequency must be one of {db.FREQUENCIES}.")
    if body.amd not in telephony.AMD_MODES:
        raise HTTPException(400, f"Answering-machine mode must be one of {telephony.AMD_MODES}.")
    if not body.message.strip():
        raise HTTPException(400, "A campaign needs a message for the voice to read.")
    problems = compliance.validate_script(body.message)
    if problems:
        raise HTTPException(400, " ".join(problems))
    if body.timezone not in dispatcher.KNOWN_TIMEZONES:
        raise HTTPException(400, f"Unknown timezone '{body.timezone}'.")
    if not dispatcher.valid_hhmm(body.call_time):
        raise HTTPException(400, f"'{body.call_time}' is not a valid time of day (HH:MM).")
    if not 0 <= body.weekday <= 6:
        raise HTTPException(400, "Weekday must be 0 (Monday) through 6 (Sunday).")

    now = db.utcnow()
    payload = body.model_dump()
    campaign_id = db.insert(
        "INSERT INTO campaigns (name, message, voice, frequency, call_time, weekday, "
        "timezone, state, amd, require_consent, created_at, next_run_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
        (
            body.name.strip(), body.message.strip(), body.voice, body.frequency,
            body.call_time, body.weekday, body.timezone, body.amd,
            1 if body.require_consent else 0, db.to_utc(now),
            db.to_utc(dispatcher.first_run_at(payload, body.start_now, now)),
        ),
    )

    added, invalid = 0, []
    if body.contacts.strip():
        added, invalid = _add_contacts(campaign_id, body.contacts, body.contacts_consented)

    db.log_event(f"Campaign '{body.name.strip()}' created with {added} contact(s).")
    return {
        "id": campaign_id,
        "contacts_added": added,
        "invalid": invalid,
        "campaign": campaign_row(require_campaign(campaign_id)),
    }


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
    if "timezone" in fields and fields["timezone"] not in dispatcher.KNOWN_TIMEZONES:
        raise HTTPException(400, f"Unknown timezone '{fields['timezone']}'.")
    if "call_time" in fields and not dispatcher.valid_hhmm(fields["call_time"]):
        raise HTTPException(400, f"'{fields['call_time']}' is not a valid time of day (HH:MM).")
    if "weekday" in fields and not 0 <= fields["weekday"] <= 6:
        raise HTTPException(400, "Weekday must be 0 (Monday) through 6 (Sunday).")
    if "message" in fields:
        problems = compliance.validate_script(fields["message"])
        if problems:
            raise HTTPException(400, " ".join(problems))

    sets = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE campaigns SET {sets} WHERE id = ?", (*fields.values(), campaign_id))

    # A schedule change (or a resume) needs the next run recomputed.
    updated = require_campaign(campaign_id)
    if updated["state"] == "active" and (
        {"frequency", "call_time", "weekday", "timezone", "state"} & fields.keys()
    ):
        now = db.utcnow()
        next_run = dispatcher.compute_next_run(updated, now)
        if next_run:
            db.execute(
                "UPDATE campaigns SET next_run_at = ? WHERE id = ?",
                (db.to_utc(next_run), campaign_id),
            )
        else:
            # A one-off has no next slot to compute. If its slot is already in the
            # past, resuming it must NOT silently re-dial the entire list - clear
            # the slot so "Run now" stays the only way to send it again.
            prior = db.from_utc(updated["next_run_at"])
            if prior is None or prior <= now:
                db.execute(
                    "UPDATE campaigns SET next_run_at = NULL WHERE id = ?", (campaign_id,)
                )
    return campaign_row(require_campaign(campaign_id))


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
    queued, skipped = dispatcher.enqueue_campaign(campaign_id)
    db.execute(
        "UPDATE campaigns SET last_run_at = ?, runs = runs + 1 WHERE id = ?",
        (db.now_str(), campaign_id),
    )
    return {"queued": queued, "skipped": skipped}


# --- contacts ---------------------------------------------------------------

def _add_contacts(campaign_id: int, raw: str, consented: bool) -> tuple[int, list[str]]:
    good, bad = parse_contacts(raw)
    if good:
        db.executemany(
            "INSERT INTO contacts (campaign_id, phone, name, consent, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(campaign_id, phone) DO UPDATE SET "
            "name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE contacts.name END, "
            "consent = MAX(contacts.consent, excluded.consent)",
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
    db.execute(
        "DELETE FROM contacts WHERE id = ? AND campaign_id = ?", (contact_id, campaign_id)
    )
    return {"ok": True}


# --- calls ------------------------------------------------------------------

@app.get("/api/calls")
def list_calls(campaign_id: int | None = None, limit: int = 200) -> list[dict]:
    limit = max(1, min(limit, 1000))
    if campaign_id:
        rows = db.query(
            "SELECT t.*, c.name AS campaign_name FROM call_tasks t "
            "LEFT JOIN campaigns c ON c.id = t.campaign_id "
            "WHERE t.campaign_id = ? ORDER BY t.id DESC LIMIT ?",
            (campaign_id, limit),
        )
    else:
        rows = db.query(
            "SELECT t.*, c.name AS campaign_name FROM call_tasks t "
            "LEFT JOIN campaigns c ON c.id = t.campaign_id "
            "ORDER BY t.id DESC LIMIT ?",
            (limit,),
        )
    for row in rows:
        row["pretty"] = compliance.pretty(row["phone"])
        row.pop("token", None)
    return rows


@app.post("/api/test-call")
def test_call(body: TestCallIn) -> dict:
    phone = compliance.normalize(body.phone or config.get("test_number"))
    if not phone:
        raise HTTPException(400, "Set a test number on the Settings page first.")
    if config.telephony() is None:
        raise HTTPException(400, "Twilio isn't configured yet - fill in Settings.")

    task_id = dispatcher.enqueue_single(phone, body.message.strip(), body.voice)
    # Restricted to this one task: pressing "Call me now" must never also dial
    # whoever is at the front of a campaign queue. The calling window is skipped
    # because the operator is deliberately calling their own number right now.
    placed = dispatcher.dispatch_pending(only_task_id=task_id, ignore_window=True)
    task = db.query_one("SELECT * FROM call_tasks WHERE id = ?", (task_id,))
    if task:
        task.pop("token", None)
    return {"task_id": task_id, "placed": placed, "task": task}


# --- scripts ----------------------------------------------------------------

@app.post("/api/script/preview")
def preview_script(body: PreviewIn) -> dict:
    final = compliance.build_script(
        body.message,
        business_name=config.get("business_name"),
        callback_number=config.get("callback_number"),
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
        "db_path": str(db.DB_PATH),
        "script_ai": scriptwriter.available(),
    }


@app.post("/api/settings")
def save_settings(body: SettingsIn) -> dict:
    values = {k: v for k, v in body.values.items() if k in config.DEFAULTS}
    # An empty secret means "leave it alone", not "erase it".
    for key in config.SECRET_KEYS:
        if key in values and not values[key].strip():
            values.pop(key)
    for key in ("test_number", "callback_number", "twilio_from_number"):
        if values.get(key):
            values[key] = compliance.normalize(values[key]) or values[key]
    config.set_many(values)
    return config.public_view()


@app.post("/api/settings/verify")
def verify_settings() -> dict:
    phone = config.telephony()
    if phone is None:
        raise HTTPException(400, "Enter the Account SID, Auth Token and From number first.")
    ok, detail = phone.verify()
    numbers, error = phone.owned_numbers() if ok else ([], "")
    from_ok, from_detail = phone.check_from_number() if ok else (False, "")
    return {
        "ok": ok, "detail": detail, "numbers": numbers, "error": error,
        "mode": phone.mode, "from_ok": from_ok, "from_detail": from_detail,
    }


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
    db.unsuppress(compliance.normalize(phone) or phone)
    return {"ok": True}


# --- Twilio webhooks (webhook mode only) ------------------------------------

def _validate(request: Request, form: dict) -> None:
    """Reject anything that isn't actually from Twilio.

    These endpoints can add a number to the do-not-call list, so they are not
    left open. Signature checking is skipped only when the auth token is absent.
    """
    from twilio.request_validator import RequestValidator

    token = config.get("twilio_auth_token")
    if not token:
        return
    signature = request.headers.get("X-Twilio-Signature", "")
    base = config.get("public_base_url").rstrip("/")
    url = f"{base}{request.url.path}" if base else str(request.url)
    if not RequestValidator(token).validate(url, form, signature):
        raise HTTPException(403, "Bad Twilio signature.")


def _task_for(token: str) -> dict:
    # `amd` is needed by the voice webhook to decide whether to hang up on a
    # machine; a test call has no campaign, so every joined column can be NULL.
    task = db.query_one(
        "SELECT t.*, c.message, c.voice, c.amd FROM call_tasks t "
        "LEFT JOIN campaigns c ON c.id = t.campaign_id WHERE t.token = ?",
        (token,),
    )
    if not task:
        raise HTTPException(404, "Unknown call token.")
    return task


@app.post("/twilio/voice/{token}")
async def twiml_for_call(token: str, request: Request) -> Response:
    form = dict(await request.form())
    _validate(request, form)
    task = _task_for(token)

    # 'Live answers only' - drop the call if a machine picked up.
    answered_by = str(form.get("AnsweredBy", ""))
    if task.get("amd") == "live_only" and answered_by.startswith("machine"):
        db.execute(
            "UPDATE call_tasks SET answered_by = ?, status = 'machine-skipped', "
            "updated_at = ? WHERE id = ?",
            (answered_by, db.now_str(), task["id"]),
        )
        return Response(telephony.build_twiml("", hangup_first=True), media_type="text/xml")

    script = compliance.build_script(
        task.get("script_override") or task.get("message") or "",
        business_name=config.get("business_name"),
        callback_number=config.get("callback_number"),
    )
    base = config.get("public_base_url").rstrip("/")
    script += " To be removed from this list, press 9 now."
    xml = telephony.build_twiml(
        script,
        voice=task.get("voice_override") or task.get("voice")
              or telephony.DEFAULT_VOICE,
        optout_url=f"{base}/twilio/optout/{token}" if base else "",
    )
    return Response(xml, media_type="text/xml")


@app.post("/twilio/optout/{token}")
async def handle_optout(token: str, request: Request) -> Response:
    form = dict(await request.form())
    _validate(request, form)
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


@app.post("/twilio/status/{token}")
async def handle_status(token: str, request: Request) -> Response:
    form = dict(await request.form())
    _validate(request, form)
    task = _task_for(token)
    dispatcher.record_status(
        task["id"],
        str(form.get("CallStatus", "")),
        str(form.get("AnsweredBy", "")),
        int(form.get("CallDuration") or 0),
    )
    return PlainTextResponse("", status_code=204)


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


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
