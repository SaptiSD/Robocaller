"""RoboCall AI - Streamlit dashboard for scripted AI voice calls."""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import database
from caller import MAX_MESSAGE_CHARS, TwilioCaller
from scheduler import process_due_campaigns, start_scheduler, sync_call_statuses

load_dotenv()
database.init_db()

st.set_page_config(page_title="RoboCall AI", page_icon="📞", layout="wide")

PHONE_RE = re.compile(r"^[+0-9][0-9\s\-().]{5,}$")


@st.cache_resource(show_spinner=False)
def get_scheduler():
    return start_scheduler()


get_scheduler()

PAGES = ["Dashboard", "New Campaign", "Campaigns", "Call Logs", "Settings"]


def _phone_numbers_from_text(raw: str) -> str:
    rows = [n.strip() for n in re.split(r"[\n,;]+", raw or "") if n.strip()]
    return "\n".join(rows)


def normalize_phone(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit() or ch == "+").strip()


# ---------------------------------------------------------------- helpers
def settings_summary() -> dict:
    return {
        "twilio_configured": bool(
            database.get_setting("twilio_account_sid")
            or os.getenv("TWILIO_ACCOUNT_SID")
        ),
        "google_number": database.get_setting("google_phone_number")
        or os.getenv("GOOGLE_PHONE_NUMBER", ""),
        "twilio_from": database.get_setting("twilio_from_number")
        or os.getenv("TWILIO_FROM_NUMBER", ""),
        "test_number": database.get_setting("test_destination_number")
        or os.getenv("TEST_DESTINATION_NUMBER", ""),
    }


def make_test_call(message: str) -> str:
    sid = database.get_setting("twilio_account_sid") or os.getenv("TWILIO_ACCOUNT_SID")
    token = database.get_setting("twilio_auth_token") or os.getenv("TWILIO_AUTH_TOKEN")
    from_number = database.get_setting("twilio_from_number") or os.getenv("TWILIO_FROM_NUMBER")
    to_number = database.get_setting("test_destination_number") or os.getenv("TEST_DESTINATION_NUMBER")
    to_number = normalize_phone(to_number)

    if not (sid and token and from_number):
        return "❌ Configure Twilio credentials in Settings first."
    if not to_number:
        return "❌ Set your test/destination number in Settings first."
    if not PHONE_RE.match(to_number):
        return f"❌ '{to_number}' doesn't look like a valid phone number."

    caller = TwilioCaller(sid, token)
    ok, call_sid, err = caller.call(to_number, from_number, message)
    database.log_call(None, to_number, "queued" if ok else "failed", call_sid, err)
    return f"✅ Call placed (SID {call_sid})" if ok else f"❌ {err}"


# ------------------------------------------------------------------ pages
def page_dashboard():
    st.title("📞 RoboCall AI")
    st.caption("AI makes the call and reads your script. Phase 1: play-and-read, no live listening.")

    s = settings_summary()
    stats = database.stats()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Active campaigns", stats["active_campaigns"])
    c2.metric("Calls today", stats["calls_today"])
    c3.metric("Total calls", stats["calls_total"])
    c4.metric("Scheduler", "Running" if get_scheduler().running else "Stopped")

    if s["google_number"]:
        st.info(f"Google Voice number on file: **{s['google_number']}** (calls originate from your "
                f"Twilio number **{s['twilio_from'] or 'unset'}**)")
    elif not s["twilio_configured"]:
        st.warning("Twilio is not configured yet. Open the **Settings** page and add your "
                   "Account SID, Auth Token, and From number to start placing calls.")

    st.divider()
    st.subheader("🧪 Test call")
    st.write(f"Places an immediate call to your test number **{s['test_number'] or 'unset'}**.")
    test_msg = st.text_area(
        "Message to read aloud",
        value="Hi there! This is a test call from RoboCall AI. "
              "This is just a scripted announcement. Have a great day!",
        height=110,
    )
    if st.button("📞 Call my test number now", type="primary"):
        with st.spinner("Placing call…"):
            st.success(make_test_call(test_msg))


def page_new_campaign():
    st.title("✨ New Campaign")

    with st.form("new_campaign", clear_on_submit=True):
        name = st.text_input("Campaign name", placeholder="e.g. Memorial Day Sale")
        phone_raw = st.text_area(
            "Phone numbers to call (one per line)",
            placeholder="+15551234567\n+15559876543",
            help="Include country code. On Twilio's free trial this must be a verified number.",
        )
        message = st.text_area(
            "Message script (AI reads this verbatim)",
            height=130,
            placeholder="Hi, this is Maple & Oak Furniture calling to let you know our "
                        "Memorial Day sale starts today. Save 30% on all living room sets through Monday.",
        )
        col1, col2, col3 = st.columns(3)
        frequency = col1.selectbox(
            "Call frequency",
            ["once", "hourly", "daily", "weekly"],
            help="'once' calls every number one time, then disables the campaign.",
        )
        call_time = col2.time_input("Time of day (daily/weekly)", value=datetime.now().time())
        active = col3.checkbox("Active", value=True)

        submitted = st.form_submit_button("Save campaign", type="primary")

    if submitted:
        numbers = _phone_numbers_from_text(phone_raw)
        err = None
        if not name:
            err = "Give the campaign a name."
        elif not numbers:
            err = "Enter at least one phone number."
        elif not message:
            err = "Enter a message script."
        elif len(message) > MAX_MESSAGE_CHARS:
            err = (f"Message is {len(message)} characters; the limit is "
                   f"{MAX_MESSAGE_CHARS}. Shorten the script.")
        elif frequency == "once" and not active:
            err = "A 'once' campaign should be active so it runs."
        if err:
            st.error(err)
        else:
            kwargs = dict(
                name=name,
                phone_numbers=numbers,
                message=message,
                frequency=frequency,
                call_time=call_time.strftime("%H:%M"),
                active=active,
            )
            if frequency in ("daily", "weekly"):
                kwargs["next_call_at"] = _first_run(call_time.strftime("%H:%M"))
            cid = database.save_campaign(kwargs)
            st.success(f"Campaign #{cid} saved. " +
                       ("It will run at the next scheduled slot." if kwargs.get("next_call_at")
                        else "It will run on the next scheduler tick (~30s)."))
            process_due_campaigns()


def _first_run(call_time: str) -> str:
    hh, mm = call_time.split(":")
    day = datetime.combine(datetime.now().date(), datetime.now().time().__class__(int(hh), int(mm)))
    if day <= datetime.now():
        day = day + timedelta(days=1)
    return day.strftime("%Y-%m-%d %H:%M:%S")


def page_campaigns():
    st.title("🗂️ Campaigns")
    campaigns = database.list_campaigns()
    if not campaigns:
        st.info("No campaigns yet. Create one in **New Campaign**.")
        return

    rows = []
    for c in campaigns:
        n = len(_phone_numbers_from_text(c["phone_numbers"]))
        rows.append({
            "ID": c["id"],
            "Name": c["name"],
            "Numbers": n,
            "Frequency": c["frequency"],
            "Active": "✅" if c["active"] else "⏸️",
            "Calls made": c["calls_made"],
            "Last called": c["last_called_at"] or "–",
            "Next call": c["next_call_at"] or "now",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Manage")
    target = st.selectbox("Select campaign", [f"#{c['id']} — {c['name']}" for c in campaigns])
    cid = int(target.split(" — ")[0][1:])
    camp = database.get_campaign(cid)
    if camp:
        with st.expander(f"📋 {camp['name']} — details", expanded=True):
            st.text_area("Message", value=camp["message"], height=120, disabled=True)
            st.caption(f"Numbers:\n{camp['phone_numbers']}")
            act = "Deactivate" if camp["active"] else "Activate"
        col1, col2, col3 = st.columns(3)
        if col1.button("📞 Run now", type="primary"):
            with st.spinner("Calling every number now…"):
                creds = _creds()
                if not creds:
                    st.error("Configure Twilio in Settings first.")
                else:
                    process_now(cid)
                    st.success("Calls placed. See **Call Logs**.")
                    st.rerun()
        if col2.button(act):
            database.update_campaign(cid, {"active": 0 if camp["active"] else 1})
            st.rerun()
        if col3.button("🗑️ Delete", type="secondary"):
            database.delete_campaign(cid)
            st.rerun()


def _creds():
    sid = database.get_setting("twilio_account_sid") or os.getenv("TWILIO_ACCOUNT_SID")
    token = database.get_setting("twilio_auth_token") or os.getenv("TWILIO_AUTH_TOKEN")
    frm = database.get_setting("twilio_from_number") or os.getenv("TWILIO_FROM_NUMBER")
    if sid and token and frm:
        return sid, token, frm
    return None


def process_now(campaign_id: int) -> None:
    from caller import TwilioCaller
    sid, token, frm = _creds()
    caller = TwilioCaller(sid, token)
    camp = database.get_campaign(campaign_id)
    nums = _phone_numbers_from_text(camp["phone_numbers"])
    for num in nums:
        ok, call_sid, err = caller.call(num, frm, camp["message"])
        database.log_call(campaign_id, num, "queued" if ok else "failed", call_sid, err)
    if (camp["frequency"] or "once").lower() == "once":
        database.update_campaign(campaign_id, {"active": 0})
    database.update_campaign(campaign_id, {
        "last_called_at": database.now_iso(),
        "calls_made": (camp.get("calls_made") or 0) + len(nums),
    })


def page_logs():
    st.title("🧾 Call Logs")
    col_a, col_b = st.columns([1, 3])
    auto = col_a.checkbox("Auto-refresh every 5s", value=False)
    if col_b.button("🔄 Refresh call outcomes from Twilio"):
        n = sync_call_statuses()
        st.toast(f"Updated {n} call(s).")
    st.caption("A call starts as `queued`; the scheduler asks Twilio for the outcome "
               "every 20s and updates it to completed / no-answer / busy / failed.")

    logs = database.list_call_logs(limit=500)
    if not logs:
        st.info("No calls yet.")
        return

    df = pd.DataFrame(logs)
    status_colors = {"queued": "🟦", "completed": "🟩", "failed": "🟥", "busy": "🟧", "no-answer": "🟨"}
    df["status"] = df["status"].map(lambda s: f"{status_colors.get(s, '▪️')} {s}")
    st.dataframe(df[["started_at", "campaign_id", "to_number", "status", "call_sid", "error"]],
                 use_container_width=True, hide_index=True)

    if auto:
        time.sleep(5)
        st.rerun()


def page_settings():
    st.title("⚙️ Settings")
    st.caption("Values entered here are stored locally and override `.env`.")
    st.caption(f"Database: `{database.DB_PATH}`")
    if "Dropbox" in database.DB_PATH:
        st.warning("Your database is inside a Dropbox folder. Dropbox replaces files it "
                   "syncs — including a database this app has open — which silently wipes "
                   "campaigns and call logs. Set `ROBOCALL_DB` to a non-synced path.")

    with st.form("settings_form"):
        sid = st.text_input("Twilio Account SID", value=database.get_setting("twilio_account_sid"),
                            placeholder=os.getenv("TWILIO_ACCOUNT_SID", ""), type="password")
        token = st.text_input("Twilio Auth Token", value=database.get_setting("twilio_auth_token"),
                              placeholder=os.getenv("TWILIO_AUTH_TOKEN", ""), type="password")
        frm = st.text_input("Twilio From number (your Twilio phone number)",
                            value=database.get_setting("twilio_from_number"),
                            placeholder=os.getenv("TWILIO_FROM_NUMBER", ""))
        google = st.text_input("Google Voice number (yours — information only)",
                               value=database.get_setting("google_phone_number"),
                               placeholder=os.getenv("GOOGLE_PHONE_NUMBER", ""))
        test = st.text_input("Test destination number (your mobile, to verify in Twilio)",
                             value=database.get_setting("test_destination_number"),
                             placeholder=os.getenv("TEST_DESTINATION_NUMBER", ""),
                             help="Twilio's free trial can only call numbers you verify in your console.")
        saved = st.form_submit_button("Save settings", type="primary")

    if saved:
        for key, val in {
            "twilio_account_sid": sid.strip(),
            "twilio_auth_token": token.strip(),
            "twilio_from_number": normalize_phone(frm),
            "google_phone_number": normalize_phone(google),
            "test_destination_number": normalize_phone(test),
        }.items():
            if val:
                database.set_setting(key, val)
        st.success("Settings saved.")

    col1, col2 = st.columns(2)
    if col1.button("Run scheduler check now"):
        n = process_due_campaigns()
        st.info(f"Scheduler tick done. {n} campaign(s) fired.")
    if col2.button("Sync call outcomes now"):
        n = sync_call_statuses()
        st.info(f"Updated {n} call log(s) from Twilio.")


# --------------------------------------------------------------- navigation
def main():
    with st.sidebar:
        st.markdown("### 📞 RoboCall AI")
        page = st.radio("Go to", PAGES, label_visibility="collapsed")
        s = settings_summary()
        st.caption(f"Twilio: **{'configured' if s['twilio_configured'] else '✗ not set'}**\n"
                   f"Test number: **{s['test_number'] or 'unset'}**")
        st.divider()
        st.caption("Phase 1 — scripted calls. The AI reads your message; "
                   "live listening/interaction comes later.")

    pages = {
        "Dashboard": page_dashboard,
        "New Campaign": page_new_campaign,
        "Campaigns": page_campaigns,
        "Call Logs": page_logs,
        "Settings": page_settings,
    }
    pages[page]()


if __name__ == "__main__":
    main()