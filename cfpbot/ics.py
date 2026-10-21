"""Build docs/conferences.ics from the stored records."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from icalendar import Alarm, Calendar, Event, vDuration

from .config import Source

FIELD_LABELS = {
    "start_date": "start date", "end_date": "end date", "cfp_deadline": "deadline",
    "cfp_deadline_time": "deadline time", "location": "location", "format": "format",
    "cfp_url": "CFP link", "info_url": "info link",
}
MAX_CHANGE_LINES = 5


def fmt_date(value: str | date | None) -> str:
    if not value:
        return "none"
    d = date.fromisoformat(value) if isinstance(value, str) else value
    return f"{d:%b} {d.day}, {d.year}"


def fmt_short(value: str) -> str:
    d = date.fromisoformat(value)
    return f"{d:%b} {d.day}"


def change_lines(rec: dict) -> list[str]:
    lines = []
    for h in reversed(rec.get("history") or []):
        label = FIELD_LABELS.get(h["field"], h["field"])
        if h["field"] in ("start_date", "end_date", "cfp_deadline"):
            old, new = fmt_date(h["old"]), fmt_date(h["new"])
        else:
            old, new = h["old"] or "none", h["new"] or "none"
        lines.append(f"Changed {fmt_short(h['date'])}: {label} moved from {old} to {new}"
                     if h["field"] in ("start_date", "end_date", "cfp_deadline")
                     else f"Changed {fmt_short(h['date'])}: {label} changed from {old} to {new}")
        if len(lines) >= MAX_CHANGE_LINES:
            break
    return lines


def display_name(source: Source, rec: dict, short: bool = False) -> str:
    base = source.short if short else source.name
    return f"{base} ({rec['region']})" if rec.get("region") else base


def describe(source: Source, rec: dict, status: dict, kind: str) -> str:
    out = []
    if rec.get("manual"):
        out.append("Entered by hand in sources.yaml (the bot can't read this site).")
    if rec.get("name") and rec["name"] != source.name:
        out.append(rec["name"])
    if rec.get("start_date"):
        span = fmt_date(rec["start_date"])
        if rec.get("end_date") and rec["end_date"] != rec["start_date"]:
            span += f" – {fmt_date(rec['end_date'])}"
        out.append(f"Conference: {span}")
    where = rec.get("location") or "not stated"
    if rec.get("format"):
        where += f" ({rec['format']})"
    out.append(f"Location: {where}")
    if rec.get("cfp_deadline"):
        dl = fmt_date(rec["cfp_deadline"])
        if rec.get("cfp_deadline_time"):
            dl += f", {rec['cfp_deadline_time']}"
        if rec.get("cfp_kind"):
            dl += f" ({rec['cfp_kind']})"
        out.append(f"CFP deadline: {dl}")
    else:
        out.append("CFP deadline: not posted yet")
    if rec.get("cfp_note"):
        out.append(f"CFP note: {rec['cfp_note']}")
    out.append(f"CFP link: {rec.get('cfp_url') or 'none found'}")
    out.append(f"Conference info: {rec.get('info_url') or (source.urls[0] if source.urls else 'none')}")
    ev = rec.get("evidence") or {}
    order = ("cfp_deadline", "start_date", "end_date") if kind == "cfp" else \
            ("start_date", "end_date", "cfp_deadline")
    quotes = []
    for f in order:
        q = ev.get(f)
        if q and q not in quotes:
            quotes.append(q)
    for q in quotes:
        out.append(f"Evidence: “{q}”")
    out.append(f"Confidence: {rec.get('confidence', 'unknown')}")
    checked = f"Last checked: {fmt_date(rec.get('last_checked'))}"
    if not rec.get("manual") and status.get("last_result") == "failed":
        checked += f" — not re-checked this week ({status.get('last_error') or 'error'})"
    out.append(checked)
    if rec.get("missing_since"):
        out.append(f"Note: no longer listed on the site since {fmt_date(rec['missing_since'])}; "
                   "showing the last known details.")
    out.extend(change_lines(rec))
    for issue in rec.get("issues") or []:
        out.append(f"Check: {issue}")
    if source.urls:
        out.append(f"Source: {source.urls[0]}")
    return "\n".join(out)


def _event(uid: str, summary: str, start: date, end_excl: date, description: str,
           rec: dict | None, now: datetime, url: str | None = None,
           location: str | None = None) -> Event:
    ev = Event()
    ev.add("uid", uid)
    ev.add("summary", summary)
    ev.add("dtstart", start)
    ev.add("dtend", end_excl)
    ev.add("dtstamp", now)
    ev.add("transp", "TRANSPARENT")
    ev.add("description", description)
    if rec is not None:
        ev.add("sequence", int(rec.get("sequence", 0)))
        ev.add("created", _midnight(rec["first_seen"]))
        ev.add("last-modified", _midnight(rec["last_changed"]))
    if url:
        ev.add("url", url)
    if location:
        ev.add("location", location)
    return ev


def _midnight(iso: str) -> datetime:
    return datetime.combine(date.fromisoformat(iso), time(0, 0), tzinfo=timezone.utc)


def build_calendar(state: dict, sources: list[Source], settings: dict, today: date,
                   now: datetime | None = None) -> bytes:
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    keep_after = timedelta(days=int(settings.get("keep_after_end_days", 365)))
    reminder_days = sorted({int(n) for n in settings.get("reminder_days", [30, 7])}, reverse=True)
    alert_after = int(settings.get("failure_alert_after", 3))
    by_id = {s.id: s for s in sources}

    cal = Calendar()
    cal.add("prodid", "-//cfp-bot//Conference & CFP Calendar//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", settings.get("calendar_name", "Conferences & CFPs"))
    cal.add("x-wr-caldesc", "Conference dates and call-for-presentations deadlines, "
                            "checked weekly by cfp-bot.")
    cal.add("refresh-interval", vDuration(timedelta(days=1)), parameters={"VALUE": "DURATION"})
    cal.add("x-published-ttl", "P1D")

    for key in sorted(state["records"]):
        rec = state["records"][key]
        source = by_id.get(rec["source_id"])
        if source is None:
            continue  # source removed from sources.yaml
        status = state["sources"].get(source.id, {})
        prefix = "(unconfirmed) " if rec.get("confidence") == "low" else ""
        name = display_name(source, rec)
        base_uid = f"{source.id}-{rec['edition']}"

        if rec.get("start_date"):
            start = date.fromisoformat(rec["start_date"])
            end = date.fromisoformat(rec["end_date"]) if rec.get("end_date") else start
            if end >= today - keep_after:
                title = f"{prefix}{name}" + (f" — {rec['location']}" if rec.get("location") else "")
                cal.add_component(_event(
                    f"{base_uid}-conference@cfp-bot", title, start, end + timedelta(days=1),
                    describe(source, rec, status, "conference"), rec, now,
                    url=rec.get("info_url") or (source.urls[0] if source.urls else None),
                    location=rec.get("location")))

        if rec.get("cfp_deadline"):
            deadline = date.fromisoformat(rec["cfp_deadline"])
            if deadline >= today - keep_after:
                desc = describe(source, rec, status, "cfp")
                ev = _event(f"{base_uid}-cfp@cfp-bot", f"{prefix}CFP due: {name}", deadline,
                            deadline + timedelta(days=1), desc, rec, now,
                            url=rec.get("cfp_url") or rec.get("info_url"))
                for n in reminder_days:
                    alarm = Alarm()
                    alarm.add("action", "DISPLAY")
                    alarm.add("description", f"CFP due in {n} days: {name}")
                    alarm.add("trigger", timedelta(days=-n))
                    ev.add_component(alarm)
                cal.add_component(ev)
                first_seen = date.fromisoformat(rec["first_seen"])
                for n in reminder_days:
                    when = deadline - timedelta(days=n)
                    if when < first_seen:
                        continue  # don't add reminders that were already in the past when found
                    cal.add_component(_event(
                        f"{base_uid}-cfp-{n}d@cfp-bot",
                        f"{prefix}CFP in {n} days: {display_name(source, rec, short=True)}",
                        when, when + timedelta(days=1), desc, rec, now,
                        url=rec.get("cfp_url") or rec.get("info_url")))

    for source in sources:
        status = state["sources"].get(source.id, {})
        if status.get("consecutive_failures", 0) >= alert_after and status.get("last_attempt"):
            day = date.fromisoformat(status["last_attempt"])
            desc = (f"The bot has failed to read {source.name} for "
                    f"{status['consecutive_failures']} weeks in a row.\n"
                    f"Last error: {status.get('last_error') or 'unknown'}\n"
                    f"Last successful read: {fmt_date(status.get('last_success'))}\n"
                    f"Check the site yourself: {source.urls[0] if source.urls else 'n/a'}\n"
                    "If the site blocks the bot, dates can be typed into sources.yaml under manual:.")
            cal.add_component(_event(f"{source.id}-sitecheck@cfp-bot",
                                     f"Bot couldn't read: {source.short}", day,
                                     day + timedelta(days=1), desc, None, now,
                                     url=source.urls[0] if source.urls else None))
    return cal.to_ical()
