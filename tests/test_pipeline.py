"""End-to-end run on a fixture with a stand-in for Gemini (no network, no API key)."""
import re
from datetime import date

import pytest
from icalendar import Calendar

import cfpbot.__main__ as main
from cfpbot import config as cfgmod

FIXTURE = """=== PAGE: https://www.musiclibraryassoc.org/page/Meetings ===
96th Annual Meeting
The meeting is scheduled for Detroit, Michigan, March 3 – 6, 2027.
Proposals are due by 11:59pm PDT on Saturday, November 7, 2026.
LINK: Call for Proposals -> https://conferences.wp.musiclibraryassoc.org/community-choice-call/
"""
MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June",
                                      "July", "August", "September", "October", "November",
                                      "December"], 1)}


class FakeExtractor:
    """Reads the dates out of the fixture with regexes, the way Gemini would."""
    calls = 0
    model = "fake-model"
    last_model = "fake-model"

    def __init__(self, *a, **k):
        pass

    def extract(self, source, text, today):
        FakeExtractor.calls += 1
        conf = re.search(r"(Detroit, Michigan, (\w+) (\d+) – (\d+), (\d{4}))", text)
        cfp = re.search(r"(11:59pm PDT on Saturday, (\w+) (\d+), (\d{4}))", text)
        m, y = MONTHS[conf.group(2)], int(conf.group(5))
        return [{
            "year": str(y), "region": None, "name": "96th Annual Meeting",
            "start_date": date(y, m, int(conf.group(3))).isoformat(),
            "end_date": date(y, m, int(conf.group(4))).isoformat(),
            "location": "Detroit, Michigan", "format": "in-person",
            "cfp_deadline": date(int(cfp.group(4)), MONTHS[cfp.group(2)], int(cfp.group(3))).isoformat(),
            "cfp_deadline_time": "11:59pm PDT", "cfp_kind": "proposal", "cfp_note": None,
            "cfp_url": "https://conferences.wp.musiclibraryassoc.org/community-choice-call/",
            "info_url": None, "confidence": "high",
            "evidence": {"start_date": conf.group(1), "end_date": conf.group(1),
                         "cfp_deadline": cfp.group(1)},
        }]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "mla.txt").write_text(FIXTURE, encoding="utf-8")
    monkeypatch.setattr(cfgmod, "FIXTURES_DIR", tmp_path / "fixtures")
    monkeypatch.setattr(cfgmod, "STATE_FILE", tmp_path / "data" / "state.json")
    monkeypatch.setattr(cfgmod, "PAGES_DIR", tmp_path / "data" / "pages")
    monkeypatch.setattr(cfgmod, "DOCS_DIR", tmp_path / "docs")
    monkeypatch.setattr(main, "Extractor", FakeExtractor)
    FakeExtractor.calls = 0
    return tmp_path


def events(tmp_path):
    cal = Calendar.from_ical((tmp_path / "docs" / "conferences.ics").read_bytes())
    return {str(e["uid"]): e for e in cal.walk("VEVENT")}


def test_pipeline_change_detection_and_updates(sandbox):
    args = ["--fixtures", "--only", "mla", "--today", "2026-10-21"]
    assert main.run(args) == 0
    assert FakeExtractor.calls == 1
    ev = events(sandbox)
    assert ev["mla-2027-cfp@cfp-bot"].decoded("dtstart") == date(2026, 11, 7)
    assert (sandbox / "docs" / "index.html").exists()

    # Second run, nothing changed: no Gemini call.
    assert main.run(["--fixtures", "--only", "mla", "--today", "2026-10-28"]) == 0
    assert FakeExtractor.calls == 1

    # Edit a date in the fixture: same event updated, with a change note, no duplicate.
    f = sandbox / "fixtures" / "mla.txt"
    f.write_text(FIXTURE.replace("November 7, 2026", "November 21, 2026"), encoding="utf-8")
    assert main.run(["--fixtures", "--only", "mla", "--today", "2026-11-04"]) == 0
    assert FakeExtractor.calls == 2
    ev = events(sandbox)
    cfp_events = [u for u in ev if u.startswith("mla-2027-cfp@")]
    assert cfp_events == ["mla-2027-cfp@cfp-bot"]
    cfp = ev["mla-2027-cfp@cfp-bot"]
    assert cfp.decoded("dtstart") == date(2026, 11, 21)
    assert int(cfp["sequence"]) == 1
    assert "Changed Nov 4: deadline moved from Nov 7, 2026 to Nov 21, 2026" in str(cfp["description"])
    assert "Evidence: “11:59pm PDT on Saturday, November 21, 2026”" in str(cfp["description"])


def test_dry_run_writes_nothing(sandbox):
    assert main.run(["--fixtures", "--only", "mla", "--dry-run", "--today", "2026-10-21"]) == 0
    assert not (sandbox / "docs").exists() and not (sandbox / "data").exists()


def test_failure_is_reported_not_fatal(sandbox):
    (sandbox / "fixtures" / "mla.txt").unlink()
    for day in ("2026-10-21", "2026-10-28", "2026-11-04"):
        assert main.run(["--fixtures", "--only", "mla", "--today", day]) == 0
    ev = events(sandbox)
    assert str(ev["mla-sitecheck@cfp-bot"]["summary"]) == "Bot couldn't read: MLA"
    assert "Failed" in (sandbox / "docs" / "index.html").read_text()
