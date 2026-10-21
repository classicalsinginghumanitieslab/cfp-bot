"""Run the weekly pipeline: fetch → (only if changed) Gemini → validate → merge → feed + status page.

    python -m cfpbot                 normal run (writes data/ and docs/)
    python -m cfpbot --dry-run       print what it found, write nothing
    python -m cfpbot --only tvf      one source (repeatable)
    python -m cfpbot --fixtures      read tests/fixtures/<id>.txt instead of the live sites
    python -m cfpbot --save-fixtures also save each fetched page text to tests/fixtures/
    python -m cfpbot --force         call Gemini even when a page hasn't changed
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import logging
import sys
from datetime import date, datetime, timezone

from . import config as cfgmod
from .config import Source, load_config
from .extract import ExtractError, Extractor
from .fetch import Fetcher, FetchResult, Page
from .ics import build_calendar
from .merge import load_state, mark_checked, merge_records, save_state, source_records
from .status import build_status_page
from .textnorm import change_fingerprint_text
from .validate import evidence_text, manual_records, validate_editions

log = logging.getLogger("cfpbot")


def fingerprint(text: str, model: str | None) -> str:
    prompt = cfgmod.PROMPT_FILE.read_text(encoding="utf-8")
    h = hashlib.sha256()
    for part in (prompt, model or "", change_fingerprint_text(text)):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def load_fixture(source: Source) -> FetchResult:
    path = cfgmod.FIXTURES_DIR / f"{source.id}.txt"
    if not path.exists():
        return FetchResult(error=f"no fixture file {path.name}")
    return FetchResult(pages=[Page(url=source.urls[0] if source.urls else "fixture", text="")],
                       fixture_text=path.read_text(encoding="utf-8"))


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cfpbot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print results; write no files")
    ap.add_argument("--only", action="append", metavar="ID", help="run just this source id")
    ap.add_argument("--fixtures", action="store_true", help="use saved page text, not live sites")
    ap.add_argument("--save-fixtures", action="store_true", help="save fetched text as fixtures")
    ap.add_argument("--force", action="store_true", help="re-extract even unchanged pages")
    ap.add_argument("--no-browser", action="store_true", help="never use headless Chromium")
    ap.add_argument("--today", help=argparse.SUPPRESS)  # for tests: pretend it's this date
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cfg = load_config()
    today = date.fromisoformat(args.today) if args.today else date.today()
    sources = cfg.sources
    if args.only:
        unknown = set(args.only) - {s.id for s in sources}
        if unknown:
            print(f"Unknown source id(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        sources = [s for s in sources if s.id in args.only]

    state = load_state(cfgmod.STATE_FILE)
    if args.dry_run:
        state = copy.deepcopy(state)
    extractor = Extractor()
    fetcher = None if args.fixtures else Fetcher(use_browser=not args.no_browser)
    page_texts: dict[str, str] = {}
    keep_days = int(cfg.settings.get("keep_after_end_days", 365))

    try:
        for source in sources:
            st = state["sources"].setdefault(source.id, {})
            st["last_attempt"] = today.isoformat()
            st["notes"] = []

            def fail(reason: str) -> None:
                st["last_result"] = "failed"
                st["last_error"] = reason
                st["consecutive_failures"] = st.get("consecutive_failures", 0) + 1
                log.warning("  %s FAILED: %s", source.id, reason)

            def succeed(result: str) -> None:
                st["last_result"] = result
                st["last_error"] = None
                st["consecutive_failures"] = 0
                st["last_success"] = today.isoformat()

            # Hand-typed dates always apply, whatever happens to the site.
            if source.manual:
                for line in merge_records(state, source.id, manual_records(source), today,
                                          manual=True):
                    log.info("  %s", line)
            if not source.urls:
                succeed("manual")
                continue

            log.info("%s: fetching", source.id)
            fetched = load_fixture(source) if args.fixtures else fetcher.fetch_source(source)
            if fetched.error:
                fail(fetched.error)
                continue
            text = fetched.combined_text()
            page_texts[source.id] = text
            st["pages"] = [p.url for p in fetched.pages] if not args.fixtures else st.get("pages", [])
            st["used_browser"] = any(p.via == "browser" for p in fetched.pages)
            st["notes"] = list(fetched.notes)
            if args.save_fixtures:  # allowed in dry runs: fixtures are test data
                cfgmod.FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
                (cfgmod.FIXTURES_DIR / f"{source.id}.txt").write_text(text, encoding="utf-8")

            fp = fingerprint(text, extractor.model)
            if fp == st.get("fingerprint") and not args.force:
                log.info("  unchanged since last good read; skipping Gemini")
                mark_checked(state, source.id, today)
                succeed("unchanged")
                continue

            try:
                raw = extractor.extract(source, text, today)
            except ExtractError as e:
                fail(str(e))
                continue
            records, notes = validate_editions(source, raw, text, today, keep_days)
            st["notes"].extend(notes)
            for line in merge_records(state, source.id, records, today,
                                      page_text=evidence_text(text)):
                log.info("  %s", line)
            st["fingerprint"] = fp
            succeed("ok")
            log.info("  %d edition(s) from Gemini, %d kept", len(raw), len(records))
    finally:
        if fetcher:
            fetcher.close()

    state["last_run"] = today.isoformat()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    ics_bytes = build_calendar(state, cfg.sources, cfg.settings, today, now)
    html = build_status_page(state, cfg.sources, cfg.settings, today)

    log.info("Gemini calls this run: %d", extractor.calls)
    if args.dry_run:
        for s in sources:
            st = state["sources"].get(s.id, {})
            print(f"\n== {s.id}: {st.get('last_result')} {st.get('last_error') or ''}")
            for n in st.get("notes") or []:
                print(f"   note: {n}")
            for key, rec in sorted(source_records(state, s.id).items()):
                print(f"   {rec['edition']}: {rec.get('start_date')}..{rec.get('end_date')} "
                      f"@ {rec.get('location')} | CFP {rec.get('cfp_deadline')} "
                      f"[{rec.get('confidence')}]")
                for f, q in (rec.get("evidence") or {}).items():
                    print(f"      {f}: “{q}”")
                for i in rec.get("issues") or []:
                    print(f"      check: {i}")
        print("\n(dry run: no files written)")
        return 0

    save_state(state, cfgmod.STATE_FILE)
    cfgmod.PAGES_DIR.mkdir(parents=True, exist_ok=True)
    for sid, text in page_texts.items():
        (cfgmod.PAGES_DIR / f"{sid}.txt").write_text(text, encoding="utf-8")
    cfgmod.DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (cfgmod.DOCS_DIR / "conferences.ics").write_bytes(ics_bytes)
    (cfgmod.DOCS_DIR / "index.html").write_text(html, encoding="utf-8")
    (cfgmod.DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")
    failed = [s.id for s in sources if state["sources"][s.id].get("last_result") == "failed"]
    log.info("Done. %d source(s), %d failed%s.", len(sources), len(failed),
             f" ({', '.join(failed)})" if failed else "")
    return 0


if __name__ == "__main__":
    sys.exit(run())
