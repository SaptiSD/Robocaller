"""End-to-end smoke test with a fake Twilio client.

Exercises: db init, campaign save, TwiML generation, scheduler firing,
status sync, and message-length guards. No real calls, no credentials.
Run:  python smoke_test.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import database

# Point the DB at a throwaway file BEFORE anything touches it.
database.DB_PATH = os.path.join(tempfile.mkdtemp(), "smoke.db")

import caller
import scheduler


class FakeCall:
    def __init__(self, sid, status):
        self.sid, self.status = sid, status

    def fetch(self):
        return self


class FakeCalls:
    def __init__(self, store):
        self.store = store

    def create(self, to, from_, twiml):
        sid = f"CA{len(self.store) + 1:032d}"
        self.store[sid] = {"to": to, "from": from_, "twiml": twiml}
        return FakeCall(sid, "queued")

    def __call__(self, sid):
        return FakeCall(sid, "completed")


class FakeClient:
    store = {}

    def __init__(self, sid, token):
        self.calls = FakeCalls(FakeClient.store)


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        check.failed = True


check.failed = False

caller.Client = FakeClient  # patch the provider

database.init_db()
database.set_setting("twilio_account_sid", "ACfake")
database.set_setting("twilio_auth_token", "faketoken")
database.set_setting("twilio_from_number", "+15550001111")

print("\n1. TwiML generation")
xml = caller.build_twiml('Sale starts today & <b>everything</b> is 30% off')
check("escapes XML special characters",
      "&amp;" in xml and "&lt;b&gt;" in xml and "<b>" not in xml)
check("uses a <Say> block with a neural voice", "<Say" in xml and caller.VOICE in xml)

print("\n2. Guards")
c = caller.TwilioCaller("ACfake", "tok")
ok, _, err = c.call("+15550002222", "+15550001111", "   ")
check("rejects an empty message", not ok and "empty" in err.lower())
ok, _, err = c.call("+15550002222", "+15550001111", "x" * (caller.MAX_MESSAGE_CHARS + 1))
check("rejects an over-long message", not ok and "limit" in err.lower())

print("\n3. Campaign -> scheduled call")
cid = database.save_campaign({
    "name": "Memorial Day Sale",
    "phone_numbers": "+15550002222\n+15550003333",
    "message": "Hi, this is Maple and Oak Furniture. Our Memorial Day sale starts today.",
    "frequency": "daily",
    "call_time": "09:00",
    "active": True,
    "next_call_at": (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
})
fired = scheduler.process_due_campaigns()
check("scheduler fired the due campaign", fired == 1)
logs = database.list_call_logs()
check("logged one call per number", len(logs) == 2)
check("both queued", all(l["status"] == "queued" for l in logs))
camp = database.get_campaign(cid)
check("calls_made incremented to 2", camp["calls_made"] == 2)
check("next_call_at rolled forward", camp["next_call_at"] > database.now_iso())
check("daily campaign stays active", camp["active"] == 1)

print("\n4. Status sync")
pending = database.list_pending_calls()
check("finds 2 pending calls", len(pending) == 2)
updated = scheduler.sync_call_statuses()
check("synced 2 statuses", updated == 2)
check("logs now completed", all(l["status"] == "completed" for l in database.list_call_logs()))
check("no calls left pending", database.list_pending_calls() == [])

print("\n5. 'once' campaign deactivates itself")
cid2 = database.save_campaign({
    "name": "One-off", "phone_numbers": "+15550004444",
    "message": "One time only.", "frequency": "once", "active": True,
})
scheduler.process_due_campaigns()
check("deactivated after running", database.get_campaign(cid2)["active"] == 0)
check("did not run twice", scheduler.process_due_campaigns() == 0)

print("\n6. Number parsing")
nums = scheduler._normalize_numbers("+1 (555) 000-2222\n+15550003333, +15550004444; junk")
check("parses 3 valid numbers, drops junk", len(nums) == 3)

print("\n7. Stats")
s = database.stats()
check("counts calls", s["calls_total"] == 3)

print("\n" + ("SOME CHECKS FAILED" if check.failed else "ALL CHECKS PASSED"))
sys.exit(1 if check.failed else 0)
