"""Text normalisation shared by evidence checks and change detection."""
from __future__ import annotations

import re
import unicodedata

_PUNCT = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "―": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", "​": "",
})


def normalize(text: str) -> str:
    """Lower-case, unify quotes/dashes, collapse whitespace (incl. around dashes)."""
    text = unicodedata.normalize("NFKC", text).translate(_PUNCT).lower()
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def contains_quote(page_text: str, quote: str) -> bool:
    """True if the quote appears in the page text, ignoring spacing, case, quote and dash styles.

    Trailing punctuation and ellipses the model may add are ignored too."""
    q = normalize(quote).strip(" .,;:…").replace("…", "...")
    if len(q) < 4:
        return False
    hay = normalize(page_text)
    if q in hay:
        return True
    # Allow an ellipsis in the quote to stand for omitted words.
    parts = [p.strip() for p in q.split("...") if p.strip()]
    if len(parts) > 1:
        pos = 0
        for p in parts:
            i = hay.find(p, pos)
            if i < 0:
                return False
            pos = i + len(p)
        return True
    return False


_NOISE_LINE = re.compile(r"(©|copyright\s|all rights reserved)", re.I)


def change_fingerprint_text(text: str) -> str:
    """The text used to decide whether a page changed: drops copyright/footer noise."""
    lines = [ln for ln in text.splitlines() if not _NOISE_LINE.search(ln)]
    return normalize("\n".join(lines))
