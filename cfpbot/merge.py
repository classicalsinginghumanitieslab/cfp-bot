"""Compare this week's records with the stored ones and record what changed."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .textnorm import contains_quote

TRACKED = ("start_date", "end_date", "location", "format", "cfp_deadline", "cfp_deadline_time",
           "cfp_url", "info_url")
SILENT = {"cfp_url", "info_url", "format"}  # updated without a change note: Gemini's choice varies
CARRIED = ("name", "region", "cfp_kind", "cfp_note", "confidence", "issues", "manual")


def empty_state() -> dict:
    return {"version": 1, "last_run": None, "sources": {}, "records": {}}


def load_state(path: Path) -> dict:
    if not path.exists():
        return empty_state()
    state = json.loads(path.read_text(encoding="utf-8"))
    for k, v in empty_state().items():
        state.setdefault(k, v)
    return state


def save_state(state: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")


def record_key(source_id: str, edition: str) -> str:
    return f"{source_id}|{edition}"


def source_records(state: dict, source_id: str) -> dict[str, dict]:
    return {k: r for k, r in state["records"].items() if r["source_id"] == source_id}


def merge_records(state: dict, source_id: str, new_records: list[dict], today: date,
                  page_text: str | None = None, manual: bool = False) -> list[str]:
    """Merge one source's fresh records into state. Returns human-readable change lines.

    manual=True merges only that source's hand-typed records (and removes ones deleted from
    sources.yaml); manual=False merges extracted records and leaves manual ones alone."""
    today_s = today.isoformat()
    changes: list[str] = []
    fresh_keys = set()
    for new in new_records:
        key = record_key(source_id, new["edition"])
        fresh_keys.add(key)
        old = state["records"].get(key)
        if old is None:
            rec = {**new, "first_seen": today_s, "last_changed": today_s, "last_checked": today_s,
                   "sequence": 0, "history": [], "missing_since": None}
            state["records"][key] = rec
            changes.append(f"new: {key}")
            continue

        changed = False
        evidence = dict(old.get("evidence") or {})
        issues = list(new.get("issues") or [])
        confidence = new.get("confidence", old.get("confidence"))
        for f in TRACKED:
            nv, ov = new.get(f), old.get(f)
            if nv == ov:
                if f in (new.get("evidence") or {}):
                    evidence[f] = new["evidence"][f]
                continue
            if nv is None and ov is not None and not manual:
                # Missing this week: keep last known value; trust it only if its quote is still there.
                if f not in SILENT:
                    issues.append(f"{f} not found this week; showing last known value")
                quote = evidence.get(f)
                if f in ("start_date", "end_date", "cfp_deadline") and page_text is not None \
                        and not (quote and contains_quote(page_text, quote)):
                    confidence = "low"
                continue
            old[f] = nv
            if f in (new.get("evidence") or {}):
                evidence[f] = new["evidence"][f]
            if f in SILENT:
                continue
            old.setdefault("history", []).append({"date": today_s, "field": f, "old": ov, "new": nv})
            if f in evidence and nv is None:
                evidence.pop(f)
            changed = True
            changes.append(f"changed: {key} {f}: {ov} -> {nv}")
        for f in CARRIED:
            if f in new:
                old[f] = new[f]
        old["issues"] = issues
        old["confidence"] = confidence
        old["evidence"] = evidence
        old["last_checked"] = today_s
        old["missing_since"] = None
        if changed:
            old["sequence"] = old.get("sequence", 0) + 1
            old["last_changed"] = today_s

    for key, rec in list(source_records(state, source_id).items()):
        if key in fresh_keys or bool(rec.get("manual")) != manual:
            continue
        if manual:
            del state["records"][key]  # removed from sources.yaml
            changes.append(f"removed: {key}")
        else:
            rec["last_checked"] = today_s
            if not rec.get("missing_since"):
                rec["missing_since"] = today_s
                changes.append(f"no longer listed: {key}")
    return changes


def mark_checked(state: dict, source_id: str, today: date) -> None:
    """Page unchanged since the last good read: the stored records still hold."""
    for rec in source_records(state, source_id).values():
        if not rec.get("manual"):
            rec["last_checked"] = today.isoformat()
