"""Tests for TextNormalizer — pure rules, no network (M2-D1 test plan).

Per spec §1.7: each rule ≥ 3 correct cases + non-match cases.  The AC
integration test at the end covers the full pipeline.
"""

from __future__ import annotations

import re

import pytest

from storyforge.textnorm import NormalizedText, TextNormalizer, normalize_text


@pytest.fixture()
def norm() -> TextNormalizer:
    return TextNormalizer()


def _text(norm: TextNormalizer, input: str) -> str:
    """Shorthand: call normalize and return the normalized string."""
    return norm.normalize(input).normalized


# --- loanword (rule 1) -------------------------------------------------------


def test_loanword_ok(norm: TextNormalizer):
    assert _text(norm, "OK") == "ô kê"


def test_loanword_case_insensitive(norm: TextNormalizer):
    assert _text(norm, "ok") == "ô kê"


def test_loanword_video(norm: TextNormalizer):
    assert "vi đi ô" in _text(norm, "xem video này")


def test_loanword_non_match_no_word_boundary(norm: TextNormalizer):
    """'okay' does not match 'ok' because \\b prevents it."""
    assert _text(norm, "okay, anh ấy nói") == "okay, anh ấy nói"


# --- time_h (rule 2) ---------------------------------------------------------


def test_time_h_with_minutes(norm: TextNormalizer):
    assert _text(norm, "10h30") == "mười giờ ba mươi"


def test_time_h_hour_only(norm: TextNormalizer):
    assert _text(norm, "8h") == "tám giờ"


def test_time_h_word(norm: TextNormalizer):
    assert _text(norm, "10h30 tối") == "mười giờ ba mươi tối"


def test_time_h_non_match_plain_text(norm: TextNormalizer):
    """'h' alone without digits is not a time."""
    assert _text(norm, "chúng tôi họp") == "chúng tôi họp"


# --- time_colon (rule 3) -----------------------------------------------------


def test_time_colon(norm: TextNormalizer):
    assert _text(norm, "21:05") == "hai mươi mốt giờ năm"


def test_time_colon_leading_zero_minutes(norm: TextNormalizer):
    assert _text(norm, "9:30") == "chín giờ ba mươi"


# --- percent (rule 4) --------------------------------------------------------


def test_percent_integer(norm: TextNormalizer):
    assert _text(norm, "50%") == "năm mươi phần trăm"


def test_percent_decimal(norm: TextNormalizer):
    assert _text(norm, "12.5%") == "mười hai phẩy năm phần trăm"


def test_percent_non_match_no_digit(norm: TextNormalizer):
    assert _text(norm, "dấu %") == "dấu %"


# --- currency (rule 5) -------------------------------------------------------


def test_currency_k(norm: TextNormalizer):
    assert _text(norm, "200k") == "hai trăm nghìn"


def test_currency_tr_ruoi(norm: TextNormalizer):
    assert _text(norm, "1.5tr") == "một triệu rưỡi"


def test_currency_ty(norm: TextNormalizer):
    assert _text(norm, "3 tỷ") == "ba tỷ"


def test_currency_tr(norm: TextNormalizer):
    assert _text(norm, "2tr") == "hai triệu"


# --- year (rule 6) ------------------------------------------------------------


def test_year_with_prefix(norm: TextNormalizer):
    assert _text(norm, "năm 1995") == "năm một chín chín năm"


def test_year_from(norm: TextNormalizer):
    assert _text(norm, "từ 2020") == "từ hai không hai không"


def test_year_start_of_sentence(norm: TextNormalizer):
    assert _text(norm, "1995, bà mới về") == "một chín chín năm, bà mới về"


def test_year_after_punct(norm: TextNormalizer):
    assert _text(norm, "… đi. 2020 mẹ mất.") == "… đi. hai không hai không mẹ mất."


def test_year_non_match_plain_number(norm: TextNormalizer):
    """'2000 con vịt' (no time context) reads as a number, not a year."""
    assert _text(norm, "2000 con vịt") == "hai nghìn con vịt"


# --- roman (rule 7) ----------------------------------------------------------


def test_roman_chapter(norm: TextNormalizer):
    assert _text(norm, "chương XIV") == "chương mười bốn"


def test_roman_volume(norm: TextNormalizer):
    assert _text(norm, "tập II") == "tập hai"


def test_roman_standalone_not_touched(norm: TextNormalizer):
    """Roman numerals not preceded by a trigger word remain unchanged."""
    assert _text(norm, "IV là 4") == "IV là bốn"


# --- decimal (rule 8) --------------------------------------------------------


def test_decimal_comma(norm: TextNormalizer):
    assert _text(norm, "3,14") == "ba phẩy mười bốn"


def test_decimal_dot(norm: TextNormalizer):
    assert _text(norm, "1.5") == "một phẩy năm"


# --- integer (rule 9) --------------------------------------------------------


def test_integer_small(norm: TextNormalizer):
    assert _text(norm, "25 con") == "hai mươi lăm con"


def test_integer_hundred(norm: TextNormalizer):
    assert _text(norm, "100") == "một trăm"


def test_integer_large(norm: TextNormalizer):
    assert _text(norm, "1000000") == "một triệu"


# --- whitespace (rule 10) ----------------------------------------------------


def test_whitespace_collapse(norm: TextNormalizer):
    assert _text(norm, "a    b") == "a b"


# --- rules_applied / NormalizedText fields -----------------------------------


def test_rules_applied_normalized_text_fields(norm: TextNormalizer):
    result = norm.normalize("10h đêm")
    assert isinstance(result, NormalizedText)
    assert result.original == "10h đêm"
    assert "time_h" in result.rules_applied


# --- AC integration test (spec §1.7) ------------------------------------------


def test_ac_normalize_pipeline(norm: TextNormalizer):
    """Spec acceptance criteria: a full sentence with hours, years, and money
    must be completely normalized with no digits left."""
    text = "Vào khoảng 10h30 tối năm 1995, bà bán được 200k."
    result = _text(norm, text)
    assert "mười giờ ba mươi" in result
    assert "một chín chín năm" in result
    assert "hai trăm nghìn" in result
    assert not re.search(r"\d", result), f"digits remain: {result}"


# --- module-level convenience -------------------------------------------------


def test_module_level_function():
    result = normalize_text("10 giờ")
    assert result.normalized == "mười giờ"
