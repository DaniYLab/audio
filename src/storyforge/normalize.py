"""TextNormalizer — deterministic pre-TTS text normalization (M2-W1).

Vietnamese TTS voices read written shorthand poorly: ``10h``, ``200k``,
``1995``, abbreviations and numeric dates come out garbled. This module
expands them into spoken forms using **rules only, never an LLM**.

It runs between the story stage and the TTS stage and is gated by
``SF__TTS__NORMALIZE_TEXT``. The exact rule table is refined against the
50-sentence lint dataset (M2-Q5) in P3; this is the first cut.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_UNITS = ("không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín")

# High-frequency written abbreviations -> spoken form. Longest keys first so a
# dotted form like "PGS.TS" is expanded before its "GS"/"TS" fragments.
_ABBREVIATIONS: dict[str, str] = {
    "TP.HCM": "thành phố Hồ Chí Minh",
    "TPHCM": "thành phố Hồ Chí Minh",
    "PGS.TS": "phó giáo sư tiến sĩ",
    "GS.TS": "giáo sư tiến sĩ",
    "UBND": "ủy ban nhân dân",
    "THCS": "trung học cơ sở",
    "THPT": "trung học phổ thông",
    "PGS": "phó giáo sư",
    "v.v.": "vân vân",
    "ĐH": "đại học",
    "GS": "giáo sư",
    "TS": "tiến sĩ",
}
_ABBREVIATIONS_ORDERED = sorted(_ABBREVIATIONS.items(), key=lambda kv: -len(kv[0]))

_CLOCK_RE = re.compile(r"(\d{1,2})h(\d{1,2})?\b", re.IGNORECASE)
_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b")
_YEAR_RE = re.compile(r"\bnăm\s+(\d{4})\b", re.IGNORECASE)
_CURRENCY_K_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*k\b", re.IGNORECASE)  # 200k
_CURRENCY_TR_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*tr\b", re.IGNORECASE)  # 2tr
_CURRENCY_TY_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*tỷ\b", re.IGNORECASE)  # 3tỷ
_CURRENCY_D_RE = re.compile(r"(\d+)\s*(?:vnđ|đ)\b", re.IGNORECASE)  # 50đ / 50vnđ
_DECIMAL_RE = re.compile(r"(?<!\w)(\d+)[.,](\d+)(?!\w)")
_INTEGER_RE = re.compile(r"(?<!\w)(\d+)(?!\w)")


def _read_under_1000(n: int) -> str:
    """Vietnamese reading of an integer in 0..999."""
    if n == 0:
        return "không"
    parts: list[str] = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        parts.append(f"{_UNITS[hundreds]} trăm")
        if rest == 0:
            return " ".join(parts)
        if rest < 10:
            parts.extend(("lẻ", _UNITS[rest]))
            return " ".join(parts)
    tens, ones = divmod(rest, 10)
    if tens == 1:
        parts.append("mười")
    elif tens > 1:
        parts.append(f"{_UNITS[tens]} mươi")
    if ones:
        if tens >= 2 and ones == 1:
            parts.append("mốt")
        elif ones == 5 and tens >= 1:
            parts.append("lăm")
        elif tens >= 2 and ones == 4:
            parts.append("tư")
        else:
            parts.append(_UNITS[ones])
    return " ".join(parts)


def _number_to_words(n: int) -> str:
    """Vietnamese reading of a non-negative integer (up to ~10^12)."""
    if n < 0:
        return "âm " + _number_to_words(-n)
    if n == 0:
        return "không"
    groups: list[int] = []
    while n > 0:
        groups.append(n % 1000)
        n //= 1000
    labels = ("", "nghìn", "triệu", "tỷ")
    parts: list[str] = []
    for i in range(len(groups) - 1, -1, -1):
        value = groups[i]
        if value == 0:
            continue
        # "lẻ" marks a zero-hundreds in a non-leading group (e.g. 2024 -> "hai
        # nghìn lẻ hai mươi tư", 2005 -> "hai nghìn lẻ năm").
        if i < len(groups) - 1 and value < 100:
            parts.append("lẻ")
        word = _read_under_1000(value)
        if labels[i]:
            word = f"{word} {labels[i]}"
        parts.append(word)
    return " ".join(parts)


def _constant_sub(value: str) -> Callable[[re.Match[str]], str]:
    """A typed re.sub replacement that always yields ``value``."""

    def _sub(_match: re.Match[str]) -> str:
        return value

    return _sub


class TextNormalizer:
    """Pure rule/regex normalizer; stateless and safe to reuse."""

    def normalize(self, text: str) -> str:
        result = text
        result = self._expand_abbreviations(result)
        result = _DATE_RE.sub(self._sub_date, result)
        result = _YEAR_RE.sub(self._sub_year, result)
        result = _CLOCK_RE.sub(self._sub_clock, result)
        result = _CURRENCY_K_RE.sub(r"\1 nghìn", result)
        result = _CURRENCY_TR_RE.sub(r"\1 triệu", result)
        result = _CURRENCY_TY_RE.sub(r"\1 tỷ", result)
        result = _CURRENCY_D_RE.sub(r"\1 đồng", result)
        result = _DECIMAL_RE.sub(self._sub_decimal, result)
        result = _INTEGER_RE.sub(self._sub_integer, result)
        return result

    def _expand_abbreviations(self, text: str) -> str:
        result = text
        for key, value in _ABBREVIATIONS_ORDERED:
            dotted = "." in key
            pattern = re.compile(
                re.escape(key) if dotted else rf"\b{re.escape(key)}\b",
                re.IGNORECASE,
            )
            result = pattern.sub(_constant_sub(value), result)
        return result

    @staticmethod
    def _sub_date(match: re.Match[str]) -> str:
        day, month = match.group(1), match.group(2)
        result = f"{day} tháng {month}"
        if match.group(3):
            result += f" năm {match.group(3)}"
        return result

    @staticmethod
    def _sub_year(match: re.Match[str]) -> str:
        return "năm " + _number_to_words(int(match.group(1)))

    @staticmethod
    def _sub_clock(match: re.Match[str]) -> str:
        hour = match.group(1)
        minute = match.group(2)
        result = f"{hour} giờ"
        if minute:
            result += f" {minute} phút"
        return result

    @staticmethod
    def _sub_decimal(match: re.Match[str]) -> str:
        return f"{match.group(1)} phẩy {match.group(2)}"

    @staticmethod
    def _sub_integer(match: re.Match[str]) -> str:
        return _number_to_words(int(match.group(0)))


def normalize_text(text: str) -> str:
    """Module-level convenience: normalize with a shared instance."""
    return TextNormalizer().normalize(text)
