"""Ask Gemini to turn page text into strict JSON. Code validates it afterwards (validate.py)."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date

from .config import PROMPT_FILE, Source

log = logging.getLogger(__name__)

MAX_INPUT_CHARS = 120_000
PAUSE_BETWEEN_CALLS = 5.0
MAX_TRIES = 3

_NULLABLE_STR = {"type": ["string", "null"]}
_DATE = {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD, or null"}

EDITION_SCHEMA = {
    "type": "object",
    "properties": {
        "year": {"type": "string", "description": "Year the conference takes place, e.g. 2027"},
        "region": {**_NULLABLE_STR, "description": "Regional chapter name, only for multi-region sites"},
        "name": {"type": "string", "description": "The edition's name as the page gives it"},
        "start_date": _DATE,
        "end_date": _DATE,
        "location": _NULLABLE_STR,
        "format": {**_NULLABLE_STR, "description": "in-person, virtual or hybrid; null if not stated"},
        "cfp_deadline": _DATE,
        "cfp_deadline_time": {**_NULLABLE_STR, "description": "e.g. 11:59 PM ET, as stated"},
        "cfp_kind": {**_NULLABLE_STR,
                     "description": "presentation, abstract, paper, poster, proposal or other"},
        "cfp_note": {**_NULLABLE_STR, "description": "Other deadlines, or the old date of an extended deadline"},
        "cfp_url": _NULLABLE_STR,
        "info_url": _NULLABLE_STR,
        "evidence": {
            "type": "object",
            "properties": {
                "start_date": _NULLABLE_STR,
                "end_date": _NULLABLE_STR,
                "cfp_deadline": _NULLABLE_STR,
            },
            "required": ["start_date", "end_date", "cfp_deadline"],
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["year", "region", "name", "start_date", "end_date", "location", "format",
                 "cfp_deadline", "cfp_deadline_time", "cfp_kind", "cfp_note", "cfp_url",
                 "info_url", "evidence", "confidence"],
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"editions": {"type": "array", "items": EDITION_SCHEMA}},
    "required": ["editions"],
}


class ExtractError(Exception):
    """Gemini could not be used this run (no key, outage, quota, bad output)."""


def build_prompt(source: Source, today: date) -> str:
    template = PROMPT_FILE.read_text(encoding="utf-8")
    return template.format(name=source.name, today=today.isoformat(),
                           hints=source.hints or "none")


def retry_delay_seconds(message: str) -> float | None:
    """Parse Gemini's 'Please retry in 3h57m48.07s' / 'retry in 36.5s' hint."""
    m = re.search(r"retry in ((?:\d+h)?(?:\d+m)?(?:[\d.]+s)?)", message or "")
    if not m or not m.group(1):
        return None
    parts = re.findall(r"([\d.]+)([hms])", m.group(1))
    return sum(float(v) * {"h": 3600, "m": 60, "s": 1}[u] for v, u in parts)


class Extractor:
    """Calls Gemini. GEMINI_MODEL may list several models, comma-separated; when one is
    overloaded or out of free daily quota, the next one is tried (each has its own quota)."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model or os.environ.get("GEMINI_MODEL") or ""
        self.models = [m.strip() for m in self.model.split(",") if m.strip()]
        self.exhausted: set[str] = set()  # out of daily quota (or unknown) for the rest of the run
        self.last_model: str | None = None
        self._client = None
        self._last_call = 0.0
        self.calls = 0

    def available(self) -> str | None:
        """None if ready, otherwise the reason it isn't."""
        if not self.api_key:
            return "GEMINI_API_KEY is not set"
        if not self.models:
            return "GEMINI_MODEL is not set"
        return None

    def _client_or_raise(self):
        problem = self.available()
        if problem:
            raise ExtractError(problem)
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def extract(self, source: Source, page_text: str, today: date) -> list[dict]:
        from google.genai import errors, types

        client = self._client_or_raise()
        prompt = build_prompt(source, today)
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=RESPONSE_SCHEMA,
            temperature=0,
        )
        contents = [prompt, page_text[:MAX_INPUT_CHARS]]
        errors_seen: list[str] = []
        for model in self.models:
            if model in self.exhausted:
                continue
            for attempt in range(MAX_TRIES):
                wait = PAUSE_BETWEEN_CALLS - (time.monotonic() - self._last_call)
                if wait > 0:
                    time.sleep(wait)
                self._last_call = time.monotonic()
                self.calls += 1
                try:
                    resp = client.models.generate_content(model=model, contents=contents,
                                                          config=config)
                except errors.APIError as e:
                    msg = f"{model}: HTTP {e.code}: {(e.message or '').splitlines()[0][:160]}"
                    if e.code == 429:
                        delay = retry_delay_seconds(str(e))
                        if delay is not None and delay <= 60 and attempt < MAX_TRIES - 1:
                            log.warning("%s; waiting %.0fs", msg, delay + 1)
                            time.sleep(delay + 1)  # per-minute limit: wait it out
                            continue
                        self.exhausted.add(model)  # daily quota gone: stop using it this run
                        log.warning("%s; switching model for the rest of this run", msg)
                    elif e.code in (500, 502, 503, 504) and attempt == 0:
                        log.warning("%s; retrying once in 20s", msg)
                        time.sleep(20)
                        continue
                    elif e.code in (400, 403, 404):
                        self.exhausted.add(model)  # bad/retired model name or no access
                        log.warning("%s; not using this model again this run", msg)
                    else:  # still overloaded after a retry: skip it for the rest of this run
                        self.exhausted.add(model)
                        log.warning("%s; trying the next model", msg)
                    errors_seen.append(msg)
                    break
                except Exception as e:  # network errors etc.
                    msg = f"{model}: {type(e).__name__}: {e}"
                    if attempt == 0:
                        time.sleep(20)
                        continue
                    errors_seen.append(msg)
                    break
                try:
                    data = json.loads(resp.text or "")
                    editions = data["editions"]
                    if not isinstance(editions, list):
                        raise TypeError("editions is not a list")
                    self.last_model = model
                    return editions
                except (ValueError, KeyError, TypeError) as e:
                    msg = f"{model}: unusable JSON: {e}"
                    log.warning(msg)
                    if attempt == MAX_TRIES - 1:
                        errors_seen.append(msg)
        if not errors_seen:
            errors_seen.append("every model is out of free quota for today")
        raise ExtractError("Gemini failed — " + "; ".join(errors_seen))
