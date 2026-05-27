"""Heuristic per-paragraph translation confidence.

No LLM cost. Combines a few cheap signals into a 0.0–1.0 score:

  * length_ratio        — len(target) / len(source). Languages translate to
                          a characteristic ratio (JA→EN ≈ 1.3–2.2, ZH→EN ≈
                          1.5–2.5, KO→EN ≈ 1.3–2.0). Wildly outside this
                          → suspicious (model truncated or hallucinated).
  * untranslated_pct    — fraction of source-script characters still present
                          in the target text. If 30% of the "English"
                          paragraph is still in kanji, something failed.
  * repeated_ngrams     — count of 5-token n-grams that repeat ≥ 3 times in
                          the target. Catches model loops (a known failure
                          mode of streaming LLM translation).

Each sub-score is mapped to [0, 1]; combined via a weighted geometric mean
so that any single failure mode drags the overall score down hard.

Confidence == 1.0 means "no red flags on these heuristics" — NOT "the
translation is correct". This is signal for triage, not a substitute for
review.
"""
from __future__ import annotations
import re
from collections import Counter
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Language-pair expectations
# ---------------------------------------------------------------------------

# Expected (min, max) ratio of len(target) / len(source) for each known
# (source, target) language pair. Values outside this band penalize confidence.
# Numbers are rough empirical bands — not literature-precise.
_LENGTH_BANDS: dict[tuple[str, str], tuple[float, float]] = {
    ("ja", "en"): (1.2, 2.4),
    ("zh", "en"): (1.4, 2.6),
    ("ko", "en"): (1.2, 2.2),
    ("ar", "en"): (0.8, 1.4),
    ("he", "en"): (0.9, 1.5),
    ("ru", "en"): (0.85, 1.3),
    ("th", "en"): (1.2, 2.2),
}

# Character ranges considered "source script" for each source language.
_SOURCE_SCRIPT_RANGES: dict[str, list[tuple[int, int]]] = {
    "ja": [(0x3040, 0x309F), (0x30A0, 0x30FF), (0x4E00, 0x9FFF)],
    "zh": [(0x4E00, 0x9FFF), (0x3400, 0x4DBF)],
    "ko": [(0xAC00, 0xD7AF), (0x1100, 0x11FF)],
    "ar": [(0x0600, 0x06FF)],
    "he": [(0x0590, 0x05FF)],
    "ru": [(0x0400, 0x04FF)],
    "th": [(0x0E00, 0x0E7F)],
}


# ---------------------------------------------------------------------------
# Sub-score helpers
# ---------------------------------------------------------------------------

@dataclass
class ConfidenceBreakdown:
    length_ratio:     float | None
    untranslated_pct: float
    repeated_ngrams:  int
    confidence:       float

    def to_dict(self) -> dict:
        return {
            "length_ratio":     self.length_ratio,
            "untranslated_pct": self.untranslated_pct,
            "repeated_ngrams":  self.repeated_ngrams,
            "confidence":       self.confidence,
        }


def _length_ratio(source: str, target: str) -> float | None:
    if not source:
        return None
    return len(target) / len(source)


def _length_ratio_score(ratio: float | None, source_lang: str, target_lang: str) -> float:
    """1.0 inside the expected band, dropping smoothly outside."""
    if ratio is None:
        return 1.0  # empty source — no signal, don't penalize
    band = _LENGTH_BANDS.get((source_lang, target_lang))
    if band is None:
        # No expectation for this pair — just sanity-check it's not absurd.
        if 0.3 < ratio < 4.0:
            return 1.0
        return 0.5
    lo, hi = band
    if lo <= ratio <= hi:
        return 1.0
    # Smooth fall-off: linearly to 0.4 over a band-width-equivalent on each side.
    width = max(hi - lo, 0.2)
    if ratio < lo:
        return max(0.4, 1.0 - 0.6 * (lo - ratio) / width)
    return max(0.4, 1.0 - 0.6 * (ratio - hi) / width)


def _untranslated_pct(target: str, source_lang: str) -> float:
    """Fraction of `target` characters that fall in the source script's
    Unicode ranges. Native-text-passthrough is the most common silent
    failure of LLM translation."""
    ranges = _SOURCE_SCRIPT_RANGES.get(source_lang)
    if not ranges or not target:
        return 0.0
    count = 0
    total = 0
    for ch in target:
        cp = ord(ch)
        if ch.isalnum() or cp >= 0x2E80:  # ignore whitespace/punctuation
            total += 1
            for lo, hi in ranges:
                if lo <= cp <= hi:
                    count += 1
                    break
    if total == 0:
        return 0.0
    return count / total


def _untranslated_score(pct: float) -> float:
    """Penalize hard: any nonzero untranslated content is suspicious for our
    target audience (reviewer expects clean target language)."""
    if pct == 0:
        return 1.0
    if pct < 0.05:
        return 0.9
    if pct < 0.15:
        return 0.7
    if pct < 0.30:
        return 0.4
    return 0.15


def _repeated_ngrams(target: str, n: int = 5, threshold: int = 3) -> int:
    """Count n-grams (by whitespace tokens) that appear ≥ threshold times.
    Catches model loops like 'and the same thing and the same thing and …'."""
    tokens = re.findall(r"\S+", target)
    if len(tokens) < n * 2:
        return 0
    grams = [" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]
    counts = Counter(grams)
    return sum(1 for g, c in counts.items() if c >= threshold)


def _repetition_score(repeated: int) -> float:
    if repeated == 0:
        return 1.0
    if repeated == 1:
        return 0.85
    if repeated < 4:
        return 0.6
    return 0.3


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute(source: str, target: str, *, source_lang: str, target_lang: str) -> ConfidenceBreakdown:
    """Compute heuristic confidence for a single paragraph.

    Combines sub-scores via geometric mean so any single red flag dominates
    (geometric mean of 1.0 × 1.0 × 0.3 = 0.67 — a meaningful drop)."""
    source = source or ""
    target = target or ""

    # Trivially-short paragraphs (heading numbers, single digits): don't
    # penalize, don't praise — return a neutral 0.95 so they don't drag the
    # filter's stats around.
    if len(source.strip()) <= 2 and len(target.strip()) <= 2:
        return ConfidenceBreakdown(
            length_ratio=None, untranslated_pct=0.0, repeated_ngrams=0, confidence=0.95
        )

    ratio = _length_ratio(source, target)
    s_ratio = _length_ratio_score(ratio, source_lang, target_lang)
    untrans_pct = _untranslated_pct(target, source_lang)
    s_untrans = _untranslated_score(untrans_pct)
    rep = _repeated_ngrams(target)
    s_rep = _repetition_score(rep)

    # Weighted geometric mean. Untranslated is the strongest signal, so it
    # gets the heaviest weight.
    weights = {"ratio": 1.0, "untrans": 2.0, "rep": 1.0}
    log_sum = (
        weights["ratio"]   * _safe_log(s_ratio) +
        weights["untrans"] * _safe_log(s_untrans) +
        weights["rep"]     * _safe_log(s_rep)
    )
    total_w = sum(weights.values())
    import math
    combined = math.exp(log_sum / total_w)

    return ConfidenceBreakdown(
        length_ratio=round(ratio, 3) if ratio is not None else None,
        untranslated_pct=round(untrans_pct, 4),
        repeated_ngrams=rep,
        confidence=round(combined, 3),
    )


def _safe_log(x: float) -> float:
    import math
    return math.log(max(x, 0.01))  # floor to avoid -inf
