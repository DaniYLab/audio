"""Rule table for TextNormalizer (M2-D1 spec §1.4).

Rules are applied in order; the FIRST rule that matches a span wins (a later
rule can only see what earlier rules left behind). ``Rule.apply`` receives the
match and returns the replacement text.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from pydantic import BaseModel

from storyforge.textnorm.numbers import num2words_vi

_UNITS = ("không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín")

# Currency shorthand -> unit words (spec §1.4 rule 5).
_CURRENCY_UNITS = {
    "k": "nghìn",
    "K": "nghìn",
    "tr": "triệu",
    "Tr": "triệu",
    "triệu": "triệu",
    "tỉ": "tỷ",
    "tỷ": "tỷ",
}

_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}

# Time words that mark a following 4-digit number as a year (spec §1.4.1).
_TIME_WORDS = "năm|từ|vào|tháng|ngày|đầu|cuối|giữa|thập niên"


class Rule(BaseModel):
    """One deterministic transformation step."""

    name: str  # stable id, recorded in NormalizedText.rules_applied
    pattern: str  # regex source, compiled at load time
    apply: Callable[[re.Match[str]], str]

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.UNICODE)


# --- apply helpers -----------------------------------------------------------


def _year_digit_reading(year: str) -> str:
    """Read a 4-digit year digit-by-digit: 1995 -> "một chín chín năm"."""
    return " ".join(_UNITS[int(digit)] for digit in year)


def _year_reading(year: str) -> str:
    if len(year) == 2:
        return num2words_vi(int(year))
    return _year_digit_reading(year)


def _apply_loanword(loanwords: dict[str, str]) -> Callable[[re.Match[str]], str]:
    def apply(match: re.Match[str]) -> str:
        return loanwords[match.group(0).lower()]

    return apply


def _apply_time_h(match: re.Match[str]) -> str:
    hour = int(match.group(1))
    minute = match.group(2)
    if hour > 23:
        return match.group(0)
    words = f"{num2words_vi(hour)} giờ"
    if minute:
        words += f" {num2words_vi(int(minute))}"
    return words


def _apply_time_colon(match: re.Match[str]) -> str:
    hour = int(match.group(1))
    minute = match.group(2)
    if hour > 23:
        return match.group(0)
    return f"{num2words_vi(hour)} giờ {num2words_vi(int(minute))}"


def _apply_percent(match: re.Match[str]) -> str:
    number = match.group(1).replace(",", ".")
    return f"{num2words_vi(float(number))} phần trăm"


def _apply_currency(match: re.Match[str]) -> str:
    raw = match.group(1).replace(",", ".")
    unit = _CURRENCY_UNITS[match.group(2)]
    if "." in raw:
        int_part, _, frac_part = raw.partition(".")
        if frac_part == "5":
            return f"{num2words_vi(int(int_part))} {unit} rưỡi"
        return f"{num2words_vi(float(raw))} {unit}"
    return f"{num2words_vi(int(raw))} {unit}"


def _apply_year(match: re.Match[str]) -> str:
    kw = match.group("kw")
    yr = match.group("yr")
    yr2 = match.group("yr2")
    punct = match.group("punct")
    yr_start = match.group("yr_start")
    if kw:
        return f"{kw} {_year_reading(yr)}"
    if punct:
        return f"{punct} {_year_reading(yr2)}"
    return _year_reading(yr_start)


def _roman_to_int(numeral: str) -> int | None:
    total = 0
    prev = 0
    for char in reversed(numeral):
        value = _ROMAN_VALUES.get(char)
        if value is None:
            return None
        if value < prev:
            total -= value
        else:
            total += value
            prev = value
    return total


def _apply_roman(match: re.Match[str]) -> str:
    kw, numeral = match.group(1), match.group(2)
    value = _roman_to_int(numeral)
    if value is None:
        return match.group(0)
    return f"{kw} {num2words_vi(value)}"


def _apply_decimal(match: re.Match[str]) -> str:
    whole, frac = match.group(1), match.group(2)
    return f"{num2words_vi(int(whole))} phẩy {num2words_vi(int(frac))}"


def _apply_integer(notes: list[str]) -> Callable[[re.Match[str]], str]:
    def apply(match: re.Match[str]) -> str:
        value = int(match.group(0))
        if value >= 10**12:
            notes.append("integer:overflow")
            return match.group(0)
        return num2words_vi(value)

    return apply


def _apply_whitespace(_match: re.Match[str]) -> str:
    return " "


# --- rule table (spec §1.4 — order matters) ----------------------------------

# Year rule (spec §1.4.1): a 4-digit number is a year only with time context —
# a time word before it ("năm 1995"), after a sentence break ("… đi. 2020"),
# or standing alone at the start of a sentence when followed by a comma
# ("1995, bà mới về").  A bare count like "2000 con vịt" falls through to the
# integer rule and reads "hai nghìn con vịt".
_YEAR_PATTERN = (
    r"(?:\b(?P<kw>năm|từ|vào|tháng|ngày|đầu|cuối|giữa|thập niên)\s+)"
    r"(?P<yr>\d{4}|\d{2})\b"
    r"|(?P<punct>[.!?…])\s+(?P<yr2>\d{4}|\d{2})\b"
    r"|(?P<yr_start>\b(?:1[0-9]{3}|20[0-9]{2}))(?=,)"
)


def build_rules(loanwords: dict[str, str], notes: list[str]) -> list[Rule]:
    """Assemble the ordered rule table.

    ``notes`` is a mutable list the integer rule appends overflow warnings to;
    the normalizer merges them into ``rules_applied``.
    """
    loanword_pattern = r"(?i)\b(" + "|".join(re.escape(k) for k in loanwords) + r")\b"
    return [
        Rule(name="loanword", pattern=loanword_pattern, apply=_apply_loanword(loanwords)),
        Rule(name="time_h", pattern=r"(\d{1,2})h(\d{1,2})?\b", apply=_apply_time_h),
        Rule(name="time_colon", pattern=r"(\d{1,2}):(\d{2})\b", apply=_apply_time_colon),
        Rule(
            name="percent",
            pattern=r"(\d+(?:[.,]\d+)?)\s*%",
            apply=_apply_percent,
        ),
        Rule(
            name="currency",
            pattern=r"(\d+(?:[.,]\d+)?)\s*(k|K|tr|Tr|triệu|tỉ|tỷ)\b",
            apply=_apply_currency,
        ),
        Rule(name="year", pattern=_YEAR_PATTERN, apply=_apply_year),
        Rule(
            name="roman",
            pattern=r"\b(chương|phần|quyển|tập)\s+([IVXLCDM]+)\b",
            apply=_apply_roman,
        ),
        Rule(
            name="decimal",
            pattern=r"(?<!\d)(\d+)[.,](\d+)(?!\d)",
            apply=_apply_decimal,
        ),
        # Guard against ":" so invalid clock times (24:14) stay untouched.
        Rule(
            name="integer",
            pattern=r"(?<!\d)(?<!:)\d+(?!\d)(?!:)",
            apply=_apply_integer(notes),
        ),
        Rule(name="whitespace", pattern=r"\s+", apply=_apply_whitespace),
    ]


RULES: list[Rule] = build_rules({}, [])
