from datetime import date

from icalendar import Calendar

from cfpbot.config import Source
from cfpbot.fetch import Page, html_to_page, mentions_only_past_years, pick_links, same_site
from cfpbot.ics import build_calendar
from cfpbot.merge import empty_state, merge_records
from cfpbot.textnorm import contains_quote
from cfpbot.validate import quote_supports, validate_editions

TODAY = date(2026, 10, 21)
SRC = Source(id="mla", name="Music Library Association Annual Meeting", short="MLA",
             urls=["https://example.org/meetings"])
PAGE = """=== PAGE: https://example.org/meetings ===
The 96th Annual Meeting is scheduled to be held in Detroit, Michigan, March 3 – 6, 2027.
The Program Committee invites proposals by
11:59pm PDT on Saturday, November 7, 2026.
LINK: Call for Proposals -> https://example.org/call
"""


def edition(**over):
    base = {
        "year": "2027", "region": None, "name": "96th Annual Meeting",
        "start_date": "2027-03-03", "end_date": "2027-03-06", "location": "Detroit, Michigan",
        "format": "in-person", "cfp_deadline": "2026-11-07", "cfp_deadline_time": "11:59pm PDT",
        "cfp_kind": "proposal", "cfp_note": None, "cfp_url": "https://example.org/call",
        "info_url": None, "confidence": "high",
        "evidence": {"start_date": "held in Detroit, Michigan, March 3 - 6, 2027",
                     "end_date": "March 3 – 6, 2027",
                     "cfp_deadline": "11:59pm PDT on Saturday, November 7, 2026"},
    }
    base.update(over)
    return base


# ---- text matching ---------------------------------------------------------------------------
def test_quote_matching_ignores_dashes_quotes_spacing():
    assert contains_quote("March 3 – 6, 2027", "March 3-6, 2027")
    assert contains_quote("the “Annual”  Meeting", 'the "annual" meeting')
    assert contains_quote("invites proposals by\n11:59pm", "invites proposals by 11:59pm")
    assert not contains_quote("March 3 – 6, 2027", "March 4-6, 2027")


def test_quote_supports_checks_day_month_year():
    assert quote_supports("March 3 – 6, 2027", date(2027, 3, 6)) == (True, True)
    assert quote_supports("Deadline: January 15", date(2027, 1, 15)) == (True, False)
    assert quote_supports("03-05-2027", date(2027, 3, 5)) == (True, True)
    assert quote_supports("March 3 – 6, 2027", date(2027, 4, 6))[0] is False


# ---- validation ------------------------------------------------------------------------------
def test_valid_edition_passes():
    recs, notes = validate_editions(SRC, [edition()], PAGE, TODAY)
    assert len(recs) == 1
    r = recs[0]
    assert r["edition"] == "2027" and r["confidence"] == "high"
    assert r["start_date"] == "2027-03-03" and r["cfp_deadline"] == "2026-11-07"
    assert r["cfp_url"] == "https://example.org/call"


def test_date_with_quote_not_on_page_is_dropped():
    e = edition(cfp_deadline="2026-12-01",
                evidence={**edition()["evidence"], "cfp_deadline": "Proposals due December 1, 2026"})
    r = validate_editions(SRC, [e], PAGE, TODAY)[0][0]
    assert r["cfp_deadline"] is None
    assert any("not on the page" in i for i in r["issues"])


def test_invented_url_is_dropped():
    r = validate_editions(SRC, [edition(info_url="https://example.org/made-up")], PAGE, TODAY)[0][0]
    assert r["info_url"] is None


def test_cfp_after_start_is_low_confidence():
    page = PAGE + "Late proposals accepted until March 10, 2027.\n"
    e = edition(cfp_deadline="2027-03-10",
                evidence={**edition()["evidence"], "cfp_deadline": "until March 10, 2027"})
    r = validate_editions(SRC, [e], page, TODAY)[0][0]
    assert r["confidence"] == "low"


def test_date_too_far_ahead_dropped_and_past_edition_ignored():
    page = PAGE + "Future meeting: June 1-4, 2032.\nPast: June 1-4, 2025.\n"
    far = edition(year="2032", start_date="2032-06-01", end_date="2032-06-04", cfp_deadline=None,
                  evidence={"start_date": "June 1-4, 2032", "end_date": "June 1-4, 2032",
                            "cfp_deadline": None})
    past = edition(year="2025", start_date="2025-06-01", end_date="2025-06-04", cfp_deadline=None,
                   evidence={"start_date": "June 1-4, 2025", "end_date": "June 1-4, 2025",
                             "cfp_deadline": None})
    recs, notes = validate_editions(SRC, [far, past], page, TODAY)
    assert recs == []
    assert any("no usable dates" in n for n in notes)
    assert any("already over" in n for n in notes)


def test_region_filter():
    src = Source(id="cms-regional", name="CMS Chapter Conference", short="CMS Chapter",
                 urls=["https://example.org"], regions=["Southwest", "Northwest"])
    page = "2027 Southwest Chapter Conference Event Date 03-05-2027\n2027 Southern Event Date 02-04-2027\n"
    sw = edition(region="Southwest", start_date="2027-03-05", end_date=None, cfp_deadline=None,
                 evidence={"start_date": "Event Date 03-05-2027", "end_date": None, "cfp_deadline": None})
    so = edition(region="Southern", start_date="2027-02-04", end_date=None, cfp_deadline=None,
                 evidence={"start_date": "Event Date 02-04-2027", "end_date": None, "cfp_deadline": None})
    recs, _ = validate_editions(src, [sw, so], page, TODAY)
    assert [r["edition"] for r in recs] == ["2027-southwest"]


# ---- merge + feed ----------------------------------------------------------------------------
def test_change_updates_same_event_with_note():
    state = empty_state()
    recs, _ = validate_editions(SRC, [edition()], PAGE, TODAY)
    merge_records(state, "mla", recs, TODAY)
    page2 = PAGE.replace("November 7, 2026", "November 14, 2026")
    e2 = edition(cfp_deadline="2026-11-14",
                 evidence={**edition()["evidence"],
                           "cfp_deadline": "11:59pm PDT on Saturday, November 14, 2026"})
    recs2, _ = validate_editions(SRC, [e2], page2, date(2026, 10, 28))
    changes = merge_records(state, "mla", recs2, date(2026, 10, 28))
    assert changes == ["changed: mla|2027 cfp_deadline: 2026-11-07 -> 2026-11-14"]
    rec = state["records"]["mla|2027"]
    assert rec["sequence"] == 1 and rec["last_changed"] == "2026-10-28"

    cal = Calendar.from_ical(build_calendar(state, [SRC], {"reminder_days": [30, 7]},
                                            date(2026, 10, 28)))
    events = {str(e["uid"]): e for e in cal.walk("VEVENT")}
    assert "mla-2027-cfp@cfp-bot" in events and "mla-2027-conference@cfp-bot" in events
    cfp = events["mla-2027-cfp@cfp-bot"]
    assert cfp.decoded("dtstart") == date(2026, 11, 14)
    assert int(cfp["sequence"]) == 1
    assert "Changed Oct 28: deadline moved from Nov 7, 2026 to Nov 14, 2026" in str(cfp["description"])
    conf = events["mla-2027-conference@cfp-bot"]
    assert conf.decoded("dtend") == date(2027, 3, 7)  # day after the last day
    assert str(conf["summary"]) == "Music Library Association Annual Meeting — Detroit, Michigan"
    # 7-day heads-up is after first_seen, so present; 30-day one was already past when found
    assert "mla-2027-cfp-7d@cfp-bot" in events
    assert "mla-2027-cfp-30d@cfp-bot" not in events


def test_missing_value_is_kept():
    state = empty_state()
    merge_records(state, "mla", validate_editions(SRC, [edition()], PAGE, TODAY)[0], TODAY)
    e2 = edition(location=None)
    merge_records(state, "mla", validate_editions(SRC, [e2], PAGE, TODAY)[0], TODAY, page_text=PAGE)
    rec = state["records"]["mla|2027"]
    assert rec["location"] == "Detroit, Michigan" and rec["sequence"] == 0


def test_low_confidence_title_and_site_check_event():
    state = empty_state()
    recs = validate_editions(SRC, [edition(confidence="low")], PAGE, TODAY)[0]
    merge_records(state, "mla", recs, TODAY)
    state["sources"]["mla"] = {"consecutive_failures": 3, "last_attempt": "2026-10-21",
                               "last_error": "HTTP 403", "last_result": "failed"}
    cal = Calendar.from_ical(build_calendar(state, [SRC], {}, TODAY))
    titles = {str(e["summary"]) for e in cal.walk("VEVENT")}
    assert "(unconfirmed) CFP due: Music Library Association Annual Meeting" in titles
    assert "Bot couldn't read: MLA" in titles


def test_old_events_drop_out_after_a_year():
    state = empty_state()
    merge_records(state, "mla", validate_editions(SRC, [edition()], PAGE, TODAY)[0], TODAY)
    cal = Calendar.from_ical(build_calendar(state, [SRC], {}, date(2028, 4, 1)))
    assert list(cal.walk("VEVENT")) == []


# ---- fetching helpers ------------------------------------------------------------------------
def test_link_picking():
    html = """<html><body><nav><a href="/call-for-papers">Call for Papers</a></nav>
    <a href="/annual-symposium/">Annual Symposium</a> <a href="/2019-conference">2019 conference</a>
    <a href="https://other.org/call">Call elsewhere</a> <a href="/sponsor-conference">Sponsors</a>
    <a href="https://conferences.example.org/">Conference website</a>
    <p>Main text June 1, 2027</p></body></html>"""
    page = html_to_page("https://www.example.org/", html)
    assert "Call for Papers" not in page.text and "June 1, 2027" in page.text
    src = Source(id="x", name="X", short="X", urls=["https://www.example.org/"])
    links = pick_links([page], src, set(), this_year=2026)
    assert links[0] == "https://www.example.org/call-for-papers"
    assert "https://www.example.org/annual-symposium/" in links
    assert "https://conferences.example.org/" in links
    assert not any("2019" in u or "other.org" in u or "sponsor" in u for u in links)


def test_site_helpers():
    assert same_site("https://www.a.org/x", "https://conf.a.org/")
    assert not same_site("https://a.org", "https://b.org")
    assert mentions_only_past_years("presenters 2019 summer", 2026)
    assert not mentions_only_past_years("2027 winter workshop", 2026)
    assert not mentions_only_past_years("annual symposium", 2026)
