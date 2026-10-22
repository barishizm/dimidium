"""Benchmark metrics: transcript normalization, WER/CER, latency stats, text tables."""

import math
import unicodedata
from collections.abc import Sequence


def normalize_transcript(text: str) -> str:
    """Lowercase, drop punctuation/symbols, collapse whitespace. Keeps Lithuanian letters."""
    kept = [" " if unicodedata.category(char)[0] in "PSZC" else char for char in text.casefold()]
    return " ".join("".join(kept).split())


def error_rates(references: Sequence[str], hypotheses: Sequence[str]) -> tuple[float, float]:
    """Corpus-level (WER, CER) over normalized transcripts. Needs jiwer (bench extras)."""
    import jiwer

    pairs = [
        (normalize_transcript(ref), normalize_transcript(hyp))
        for ref, hyp in zip(references, hypotheses, strict=True)
    ]
    pairs = [(ref, hyp) for ref, hyp in pairs if ref]
    if not pairs:
        return math.nan, math.nan
    refs = [ref for ref, _ in pairs]
    hyps = [hyp for _, hyp in pairs]
    return jiwer.wer(refs, hyps), jiwer.cer(refs, hyps)


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile (0-100); nan for no values."""
    if not values:
        return math.nan
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100
    low = math.floor(rank)
    high = math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def fmt_ms(seconds: float | None) -> str:
    if seconds is None or math.isnan(seconds):
        return "-"
    return f"{seconds * 1000:.0f}"


def fmt_pct(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "-"
    return f"{value * 100:.1f}%"


def format_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(cell)) for w, cell in zip(widths, row, strict=True)]

    def line(cells: Sequence[str]) -> str:
        return "  ".join(cell.ljust(w) for cell, w in zip(cells, widths, strict=True)).rstrip()

    separator = "  ".join("-" * w for w in widths)
    return "\n".join([line(headers), separator, *(line(row) for row in rows)])
