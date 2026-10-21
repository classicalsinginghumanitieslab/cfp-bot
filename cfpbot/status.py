"""Build docs/index.html: a plain status page with every source and the subscribe link."""
from __future__ import annotations

from datetime import date
from html import escape

from .config import Source
from .ics import display_name, fmt_date
from .merge import source_records

RESULT_LABELS = {
    "ok": ("Read", "ok"),
    "unchanged": ("Read — no change", "ok"),
    "failed": ("Failed", "bad"),
    "manual": ("Manual entry", "warn"),
    None: ("Not run yet", "warn"),
}

CSS = """
:root { --bg:#fbfaf7; --fg:#1d1d1b; --muted:#6b6a65; --line:#e3e1da; --card:#ffffff;
        --ok:#1f7a3a; --bad:#b3261e; --warn:#8a5a00; --link:#1a5fb4; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#171716; --fg:#ecebe6; --muted:#a3a19a; --line:#34332f; --card:#1f1f1d;
          --ok:#6fcf8b; --bad:#ff8a80; --warn:#f0c060; --link:#8ab4f8; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:1.6rem; margin:0 0 4px; }
.sub { color:var(--muted); margin:0 0 20px; }
.subscribe { background:var(--card); border:1px solid var(--line); border-radius:8px;
             padding:14px 16px; margin-bottom:24px; }
.subscribe code { word-break:break-all; font-size:.9rem; }
a { color:var(--link); }
.table-wrap { overflow-x:auto; border:1px solid var(--line); border-radius:8px; background:var(--card); }
table { border-collapse:collapse; width:100%; min-width:760px; }
th, td { text-align:left; vertical-align:top; padding:10px 12px; border-bottom:1px solid var(--line); }
th { font-size:.8rem; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }
tr:last-child td { border-bottom:0; }
.ok { color:var(--ok); font-weight:600; } .bad { color:var(--bad); font-weight:600; }
.warn { color:var(--warn); font-weight:600; }
.small { font-size:.85rem; color:var(--muted); }
ul.eds { margin:0; padding-left:18px; }
"""


def build_status_page(state: dict, sources: list[Source], settings: dict, today: date) -> str:
    feed = settings.get("feed_url") or "conferences.ics"
    webcal = feed.replace("https://", "webcal://", 1) if feed.startswith("https://") else feed
    rows = []
    for s in sources:
        st = state["sources"].get(s.id, {})
        label, cls = RESULT_LABELS.get(st.get("last_result"), RESULT_LABELS[None])
        detail = []
        if st.get("last_result") == "failed":
            detail.append(f"{escape(st.get('last_error') or 'unknown error')}")
            if st.get("consecutive_failures"):
                detail.append(f"{st['consecutive_failures']} week(s) in a row")
        for n in (st.get("notes") or [])[:6]:
            detail.append(escape(n))
        eds = []
        for rec in sorted(source_records(state, s.id).values(),
                          key=lambda r: r.get("start_date") or r.get("cfp_deadline") or ""):
            parts = [escape(display_name(s, rec)) + f" <span class='small'>({escape(rec['edition'])})</span>"]
            if rec.get("start_date"):
                span = fmt_date(rec["start_date"])
                if rec.get("end_date") and rec["end_date"] != rec["start_date"]:
                    span += f" – {fmt_date(rec['end_date'])}"
                parts.append(escape(span))
            if rec.get("location"):
                parts.append(escape(rec["location"]))
            parts.append("CFP " + (escape(fmt_date(rec["cfp_deadline"])) if rec.get("cfp_deadline")
                                   else "not posted"))
            conf = rec.get("confidence")
            if conf in ("low", "medium", "manual"):
                parts.append(f"<span class='{'bad' if conf == 'low' else 'warn'}'>{conf}</span>")
            eds.append("<li>" + " · ".join(parts) + "</li>")
        pages = st.get("pages") or []
        rows.append(f"""<tr>
<td><strong>{escape(s.name)}</strong><br><span class="small">{escape(s.id)}</span><br>
{''.join(f'<a class="small" href="{escape(u)}">{escape(u)}</a><br>' for u in s.urls)}</td>
<td><span class="{cls}">{label}</span><br><span class="small">{'<br>'.join(detail)}</span></td>
<td>{escape(fmt_date(st.get('last_success')) if st.get('last_success') else 'never')}
<br><span class="small">{len(pages)} page(s){' via browser' if st.get('used_browser') else ''}</span></td>
<td>{'<ul class="eds">' + ''.join(eds) + '</ul>' if eds else '<span class="small">No upcoming dates found</span>'}</td>
</tr>""")
    last_run = fmt_date(state.get("last_run")) if state.get("last_run") else "never"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Conference & CFP Bot</title>
<style>{CSS}</style>
</head>
<body>
<main>
<h1>Conference &amp; CFP Bot</h1>
<p class="sub">Checks {len(sources)} conference sites every Monday. Last run: {escape(last_run)}.</p>
<div class="subscribe">
<strong>Subscribe to the calendar</strong><br>
<code>{escape(feed)}</code><br>
<span class="small">Google Calendar: Other calendars → + → From URL, paste the address above.
Apple Calendar: <a href="{escape(webcal)}">open this link</a>, or File → New Calendar Subscription.</span>
</div>
<div class="table-wrap">
<table>
<thead><tr><th>Source</th><th>This week</th><th>Last good read</th><th>What the bot found</th></tr></thead>
<tbody>
{''.join(rows)}
</tbody>
</table>
</div>
</main>
</body>
</html>
"""
