"""Check Gemini's output against the page. Dates without a quote on the page are dropped;
dates that fail a sanity check are kept but marked low confidence."""
from __future__ import annotations

import re
from datetime import date, timedelta

from .config import Source
from .textnorm import contains_quote, normalize

DATE_FIELDS = ("start_date", "end_date", "cfp_deadline")
FORMATS = {"in-person", "virtual", "hybrid"}
CFP_KINDS = {"presentation", "abstract", "paper", "poster", "proposal", "other"}
CONF_RANK = {"low": 0, "medium": 1, "high": 2}
MAX_YEARS_AHEAD = 4

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_FULL_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
                "september", "october", "november", "december"]
# Spanish/Portuguese month names (PAVA is trilingual)
_OTHER_MONTHS = [["enero", "janeiro"], ["febrero", "fevereiro"], ["marzo", "março"], ["abril"],
                 ["mayo", "maio"], ["junio", "junho"], ["julio", "julho"], ["agosto"],
                 ["septiembre", "setiembre", "setembro"], ["octubre", "outubro"],
                 ["noviembre", "novembro"], ["diciembre", "dezembro"]]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def evidence_text(combined: str) -> str:
    """The page text with the bot's own LINK lists and page headers removed."""
    return "\n".join(ln for ln in combined.splitlines()
                     if not ln.startswith("LINK: ") and not ln.startswith("=== PAGE: "))


def parse_iso(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def quote_supports(quote: str, d: date) -> tuple[bool, bool]:
    """(day and month are in the quote, year is in the quote)."""
    q = normalize(quote)
    day_ok = re.search(rf"(?<!\d)0?{d.day}(?!\d)", q) is not None
    m = d.month - 1
    names = [_MONTHS[m], _FULL_MONTHS[m], *_OTHER_MONTHS[m]]
    month_ok = any(re.search(rf"\b{n}", q) for n in names)
    if not month_ok:  # numeric dates like 03-05-2027 or 3/5/27
        month_ok = re.search(rf"(?<!\d)0?{d.month}[/.-]\d{{1,2}}|\d{{1,2}}[/.-]0?{d.month}(?!\d)",
                             q) is not None
    year_ok = str(d.year) in q or re.search(rf"[/.-]{d.year % 100:02d}(?!\d)", q) is not None
    return day_ok and month_ok, year_ok


def lower(conf: str, to: str) -> str:
    return to if CONF_RANK[to] < CONF_RANK.get(conf, 2) else conf


def validate_editions(source: Source, raw_editions: list[dict], combined_text: str,
                      today: date, keep_after_end_days: int = 365) -> tuple[list[dict], list[str]]:
    """Return (clean records, source-level notes)."""
    page = evidence_text(combined_text)
    page_norm_links = combined_text  # URLs are checked against the full text incl. link lists
    horizon = today + timedelta(days=365 * MAX_YEARS_AHEAD + 1)
    records: dict[str, dict] = {}
    notes: list[str] = []
    wanted_regions = [r.lower() for r in source.regions]

    for raw in raw_editions:
        if not isinstance(raw, dict):
            continue
        region = (raw.get("region") or "").strip() or None
        if wanted_regions:
            if not region or not any(w in region.lower() or region.lower() in w
                                     for w in wanted_regions):
                continue
            region = next(r for r in source.regions
                          if r.lower() in region.lower() or region.lower() in r.lower())
        year = re.sub(r"\D", "", str(raw.get("year") or ""))[:4]
        issues: list[str] = []
        confidence = raw.get("confidence") if raw.get("confidence") in CONF_RANK else "medium"
        evidence_in = raw.get("evidence") or {}
        rec = {
            "source_id": source.id,
            "region": region,
            "name": (raw.get("name") or source.name).strip(),
            "location": (raw.get("location") or "").strip() or None,
            "format": raw.get("format") if raw.get("format") in FORMATS else None,
            "cfp_deadline_time": (raw.get("cfp_deadline_time") or "").strip() or None,
            "cfp_kind": raw.get("cfp_kind") if raw.get("cfp_kind") in CFP_KINDS else None,
            "cfp_note": (raw.get("cfp_note") or "").strip() or None,
            "evidence": {},
        }
        for f in ("cfp_url", "info_url"):
            url = (raw.get(f) or "").strip()
            if url and url.startswith("http") and url.rstrip("/") in page_norm_links:
                rec[f] = url
            else:
                rec[f] = None
                if url:
                    issues.append(f"{f} {url} dropped: not a link on the page")

        dates: dict[str, date | None] = {}
        for f in DATE_FIELDS:
            d = parse_iso(raw.get(f))
            quote = (evidence_in.get(f) or "").strip()
            if raw.get(f) and d is None:
                issues.append(f"{f} {raw.get(f)!r} dropped: not a valid date")
            elif d is not None and not quote:
                issues.append(f"{f} {d} dropped: no quote given")
                d = None
            elif d is not None and not contains_quote(page, quote):
                issues.append(f"{f} {d} dropped: its quote is not on the page")
                d = None
            elif d is not None and d > horizon:
                issues.append(f"{f} {d} dropped: more than {MAX_YEARS_AHEAD} years ahead")
                d = None
            if d is not None:
                day_month_ok, year_ok = quote_supports(quote, d)
                if not day_month_ok:
                    confidence = lower(confidence, "low")
                    issues.append(f"{f} {d}: the quote doesn't show this day and month")
                elif not year_ok:
                    confidence = lower(confidence, "medium")
                rec["evidence"][f] = quote
            dates[f] = d

        start, end, cfp = dates["start_date"], dates["end_date"], dates["cfp_deadline"]
        if start is None and end is not None:
            issues.append(f"end_date {end} kept without a start date; treated as a one-day event")
            start, end = end, None
            rec["evidence"]["start_date"] = rec["evidence"].pop("end_date")
        if start and end and end < start:
            confidence = lower(confidence, "low")
            issues.append(f"end_date {end} is before start_date {start}; ignored")
            end = None
            rec["evidence"].pop("end_date", None)
        if cfp and start and cfp >= start:
            confidence = lower(confidence, "low")
            issues.append(f"CFP deadline {cfp} is not before the conference start {start}")
        if start is None and cfp is None:
            notes.append(f"edition {year or '?'}{' ' + region if region else ''}: "
                         "no usable dates" + (f" ({'; '.join(issues)})" if issues else ""))
            continue
        last_day = end or start
        if last_day and last_day < today and not (cfp and cfp >= today):
            notes.append(f"edition {year} ignored: already over ({last_day})")
            continue
        if not year:
            year = str((start or cfp).year)

        rec.update({
            "edition": year + (f"-{slug(region)}" if region else ""),
            "start_date": start.isoformat() if start else None,
            "end_date": end.isoformat() if end else None,
            "cfp_deadline": cfp.isoformat() if cfp else None,
            "confidence": confidence,
            "issues": issues,
        })
        key = rec["edition"]
        if key in records:
            notes.append(f"edition {key} reported twice; kept the first")
            continue
        records[key] = rec
    return list(records.values()), notes


def manual_records(source: Source) -> list[dict]:
    """Records typed in by hand under `manual:` in sources.yaml."""
    out = []
    for m in source.manual:
        start, end, cfp = (parse_iso(m.get(f)) for f in DATE_FIELDS)
        region = m.get("region")
        edition = str(m.get("edition") or (start or cfp).year)
        out.append({
            "source_id": source.id,
            "edition": edition,
            "region": region,
            "name": m.get("name") or source.name,
            "start_date": start.isoformat() if start else None,
            "end_date": end.isoformat() if end else None,
            "location": m.get("location"),
            "format": m.get("format") if m.get("format") in FORMATS else None,
            "cfp_deadline": cfp.isoformat() if cfp else None,
            "cfp_deadline_time": m.get("cfp_deadline_time"),
            "cfp_kind": m.get("cfp_kind"),
            "cfp_note": m.get("cfp_note"),
            "cfp_url": m.get("cfp_url"),
            "info_url": m.get("info_url"),
            "evidence": {},
            "confidence": "manual",
            "issues": [],
            "manual": True,
        })
    return out
