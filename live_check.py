"""Check a deployed server end to end, without placing a call.

    python live_check.py https://robocaller.onrender.com
    python live_check.py https://robocaller.onrender.com --write

The test suites prove the code against a fake Telnyx and a throwaway database.
This proves the deployment: the real host, the real Postgres, the real settings.
It is what would have caught a schedule that had quietly stopped firing - an
active campaign whose next run is in the past means the dispatcher isn't
running, whatever the dashboard's "Running" pill says.

Read-only by default. --write also takes a throwaway campaign through its whole
life - create, read, edit, pause, Run now, resume, Run now, delete - and it
cannot ring anyone, by construction:

  - its one contact is 617-555-0123, from the 555-0100..0199 block reserved for
    fiction, and is saved WITHOUT consent on a campaign that requires it, so it
    is screened out at queue time and again at dial time;
  - its schedule starts a week out, at 03:00, outside any calling window;
  - it is deleted at the end even when a check fails.

It does show up in Activity (created, queued 0, deleted), named so nobody
mistakes it for a real campaign. Opening the site this way also counts as a
visit, so it wakes a sleeping host and resets its idle timer.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import httpx

PASS, FAIL, WARN = 0, 0, 0
TEST_NAME = "Automated live check - safe to ignore"
TEST_NUMBER = "617-555-0123"


def check(label: str, condition: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}" + (f" -- {detail}" if detail else ""))
    return condition


def warn(label: str) -> None:
    global WARN
    WARN += 1
    print(f"  warn {label}")


def section(name: str) -> None:
    print(f"\n{name}")


def utc(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def read_only(client: httpx.Client) -> None:
    now = datetime.now(timezone.utc)

    section("the server is up")
    res = client.get("/healthz")
    check("/healthz answers ok", res.status_code == 200 and res.json() == {"ok": True}, res.text[:200])

    res = client.get("/api/overview")
    if not check("the dashboard's overview loads", res.status_code == 200, res.text[:200]):
        return
    overview = res.json()
    engine = overview["engine"]
    check("the dispatcher thread is running", engine.get("running") is True, str(engine))
    check("and its last tick raised nothing", not engine.get("last_error"), engine.get("last_error", ""))
    last_tick = utc(engine.get("last_tick"))
    check("and it ticked in the last minute", last_tick is not None and now - last_tick < timedelta(minutes=1),
          str(engine.get("last_tick")))
    if engine.get("paused"):
        warn("calling is paused - nothing will be dialled until it's resumed")

    keep = engine.get("keep_awake")
    if not check("the deployed build reports keep-awake (the Oct 19 fix is live)", isinstance(keep, dict), str(keep)):
        keep = {}
    if keep.get("url"):
        check("keep-awake knows its own public address", keep["url"].startswith("https://"), keep["url"])
    else:
        warn("no public address, so the server can't keep itself awake - fine on a host that never sleeps")

    section("campaigns")
    res = client.get("/api/campaigns")
    if not check("the campaign list loads", res.status_code == 200, res.text[:200]):
        return
    campaigns = res.json()
    print(f"       ({len(campaigns)} campaign(s))")
    check("every campaign carries its missed-run count",
          all(isinstance(c.get("missed_runs"), int) for c in campaigns),
          str([c.get("id") for c in campaigns if not isinstance(c.get("missed_runs"), int)]))
    active = [c for c in campaigns if c["state"] == "active" and c.get("next_run_at")]
    # The test of whether the schedule is actually being worked: a run that has
    # come due is picked up within a tick, so its next_run_at moves into the future.
    overdue = [f"{c['name']} (due {c['next_run_at']} UTC)" for c in active
               if utc(c["next_run_at"]) < now - timedelta(minutes=2)]
    check("no active campaign is overdue - the schedule is being worked", not overdue, "; ".join(overdue))
    # Keep-awake only holds the server up for a campaign with somebody it could call.
    callable_ = [c for c in active if c["contacts"] and (c["consented"] or not c["require_consent"])]
    if callable_ and not engine.get("paused"):
        check("with runs scheduled, keep-awake is holding the server up",
              bool(keep.get("holding")) or not keep.get("url"), str(keep))
        check("and its check-ins aren't failing", not keep.get("last_error"), keep.get("last_error", ""))
        started = utc(engine.get("started_at"))
        if started and now - started > timedelta(minutes=keep.get("every_minutes", 5) + 1):
            last_ok = utc(keep.get("last_ok"))
            check("and the last check-in was recent", last_ok is not None
                  and now - last_ok < timedelta(minutes=keep.get("every_minutes", 5) * 2 + 1),
                  str(keep.get("last_ok")))
    for c in campaigns:
        if c.get("missed_runs"):
            warn(f"'{c['name']}' has missed {c['missed_runs']} scheduled run(s) - see Activity")

    for c in campaigns:
        cid = c["id"]
        responses = [client.get(f"/api/campaigns/{cid}"),
                     client.get(f"/api/campaigns/{cid}/contacts"),
                     client.get("/api/calls", params={"campaign_id": cid, "limit": 50})]
        check(f"'{c['name']}': detail, contacts and calls all load",
              all(r.status_code == 200 for r in responses),
              str([r.status_code for r in responses]))

    section("the rest of the dashboard")
    for path in ("/api/calls", "/api/calls.csv", "/api/suppression", "/api/documents", "/api/inbound"):
        res = client.get(path)
        check(f"{path} loads", res.status_code == 200, f"HTTP {res.status_code}: {res.text[:120]}")
    res = client.get("/api/settings")
    if check("/api/settings loads", res.status_code == 200, res.text[:120]):
        settings = res.json()["settings"]
        check("secrets are never sent to the browser",
              settings.get("telnyx_api_key") == "" and settings.get("perplexity_api_key", "") == "")
        check("Telnyx is configured", settings.get("telnyx_ready") is True)
    page = client.get("/")
    script = client.get("/app.js")
    check("the dashboard page and its script are served",
          page.status_code == 200 and script.status_code == 200 and client.get("/styles.css").status_code == 200)
    check("the served script is the current build", "keepAwakeText" in script.text and "missedText" in script.text)


def round_trip(client: httpx.Client) -> None:
    section("a campaign's whole life (throwaway, cannot dial)")
    now = datetime.now(timezone.utc)
    starts = (now + timedelta(days=7)).strftime("%Y-%m-%d 03:00")
    ends = (now + timedelta(days=8)).strftime("%Y-%m-%d 04:00")
    res = client.post("/api/campaigns", json={
        "name": TEST_NAME,
        "message": "This is an automated check of the campaign feature. Nobody is called.",
        "frequency": "daily", "call_time": "03:00", "timezone": "America/New_York",
        "require_consent": True, "starts_at": starts, "ends_at": ends,
        "contacts": TEST_NUMBER, "contacts_consented": False,
    })
    if not check("a campaign can be created", res.status_code == 200, res.text[:300]):
        return
    created = res.json()
    cid = created["id"]
    try:
        camp = created["campaign"]
        check("with its contact saved, unconsented",
              created["contacts_added"] == 1 and created["unconsented"] == 1 and camp["consented"] == 0,
              str(created)[:300])
        check("scheduled for its start, a week out", camp["state"] == "active"
              and utc(camp["next_run_at"]) > now + timedelta(days=6), str(camp.get("next_run_at")))
        check("with no missed runs", camp["missed_runs"] == 0)

        res = client.patch(f"/api/campaigns/{cid}", json={"state": "paused"})
        check("it can be paused", res.status_code == 200 and res.json()["state"] == "paused", res.text[:200])
        res = client.post(f"/api/campaigns/{cid}/run")
        check("Run now on a paused campaign is refused", res.status_code == 409, res.text[:200])

        res = client.patch(f"/api/campaigns/{cid}", json={
            "message": "This is an automated check, edited. Nobody is called.", "call_time": "03:30"})
        check("it can be edited", res.status_code == 200
              and res.json()["message"].endswith("edited. Nobody is called."), res.text[:200])

        contacts = client.get(f"/api/campaigns/{cid}/contacts").json()
        check("its contact reads back with a local time zone",
              len(contacts) == 1 and contacts[0]["consent"] == 0
              and contacts[0]["timezone"] == "America/New_York", str(contacts))

        res = client.patch(f"/api/campaigns/{cid}", json={"state": "active"})
        check("it can be resumed, and is rescheduled", res.status_code == 200
              and res.json()["state"] == "active" and res.json()["next_run_at"], res.text[:200])
        res = client.post(f"/api/campaigns/{cid}/run")
        body = res.json() if res.status_code == 200 else {}
        check("Run now queues nobody without consent", res.status_code == 200 and body.get("queued") == 0
              and body.get("skip_reasons", {}).get("no-consent") == 1, res.text[:200])
        calls = client.get("/api/calls", params={"campaign_id": cid}).json()
        check("and the call log records the screen-out, with nothing dialled",
              len(calls) == 1 and calls[0]["state"] == "skipped" and calls[0]["status"] == "no-consent"
              and not calls[0].get("sid"), str(calls)[:300])
    finally:
        res = client.delete(f"/api/campaigns/{cid}")
        check("it can be deleted", res.status_code == 200, res.text[:200])
        check("and is gone", client.get(f"/api/campaigns/{cid}").status_code == 404)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    base = args[0].rstrip("/").split("#")[0].rstrip("/")
    print(f"checking {base}")
    # A host that was asleep takes up to a minute to answer its first request.
    with httpx.Client(base_url=base, timeout=httpx.Timeout(90.0, connect=30.0),
                      headers={"User-Agent": "RoboCallAI-live-check"}) as client:
        read_only(client)
        if "--write" in sys.argv:
            round_trip(client)
    print(f"\n{PASS} passed, {FAIL} failed, {WARN} warning(s)")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
