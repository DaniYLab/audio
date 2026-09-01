"""Vietnamese number-to-words (M2-D1 spec §1.2).

``num2words_vi`` reads 0..999 tỷ (10^12-1), negatives and decimals. Numbers at
or above 10^12 are returned unchanged — the caller appends an
``integer:overflow`` warning to ``rules_applied``.
"""

from __future__ import annotations

_UNITS = ("không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín")
_TỶ = 10**9
_NGHÌN = 10**3


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


def _integer_words(n: int) -> str:
    """Vietnamese reading of a non-negative integer < 10^12.

    Groups of 3 are assembled from the highest down.  A non-leading group
    whose hundreds digit is 0 reads as "không trăm (lẻ) <n>" so that 1005 =
    "một nghìn không trăm lẻ năm" and 1020 = "một nghìn không trăm hai mươi".
    """
    if n == 0:
        return "không"
    groups: list[int] = []
    while n > 0:
        groups.append(n % 1000)
        n //= 1000
    names = ("", "nghìn", "triệu", "tỷ")
    parts: list[str] = []
    for i in range(len(groups) - 1, -1, -1):
        group = groups[i]
        if group == 0:
            continue
        word = _read_under_1000(group)
        if i < len(groups) - 1 and group < 100:
            marker = "không trăm lẻ " if group < 10 else "không trăm "
            word = marker + word
        if names[i]:
            word = f"{word} {names[i]}"
        parts.append(word)
    return " ".join(parts)


def num2words_vi(n: int | float) -> str:
    """Vietnamese words for an integer or decimal, or the original input when
    the integer part is >= 10^12 (overflow — caller records the warning)."""
    if isinstance(n, float):
        int_part = int(n)
        frac_text = f"{n:.6f}".rstrip("0").rstrip(".")
        _, _, frac = frac_text.partition(".")
        base = num2words_vi(int_part)
        if frac and int(frac) != 0:
            base += " phẩy " + _integer_words(int(frac))
        return base
    if n < 0:
        return "âm " + num2words_vi(-n)
    if n >= 10**12:
        return str(n)
    return _integer_words(n)
