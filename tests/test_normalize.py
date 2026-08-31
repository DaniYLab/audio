"""Tests for TextNormalizer (M2-W1) — pure rules, no network."""

from __future__ import annotations

import pytest

from storyforge.normalize import TextNormalizer, normalize_text


@pytest.fixture()
def norm() -> TextNormalizer:
    return TextNormalizer()


def test_clock_hour(norm: TextNormalizer):
    assert norm.normalize("10h đêm") == "mười giờ đêm"


def test_clock_with_minutes(norm: TextNormalizer):
    assert norm.normalize("10h30") == "mười giờ ba mươi phút"


def test_money_k(norm: TextNormalizer):
    assert norm.normalize("200k") == "hai trăm nghìn"


def test_money_tr(norm: TextNormalizer):
    assert norm.normalize("2tr") == "hai triệu"


def test_year_with_prefix(norm: TextNormalizer):
    assert norm.normalize("năm 1995") == "năm một nghìn chín trăm chín mươi lăm"


def test_standalone_year_number(norm: TextNormalizer):
    assert norm.normalize("1995") == "một nghìn chín trăm chín mươi lăm"


def test_year_with_zero_hundreds(norm: TextNormalizer):
    assert norm.normalize("năm 2024") == "năm hai nghìn lẻ hai mươi tư"


def test_date(norm: TextNormalizer):
    assert norm.normalize("20/11") == "hai mươi tháng mười một"


def test_date_with_year(norm: TextNormalizer):
    assert norm.normalize("20/11/2024") == "hai mươi tháng mười một năm hai nghìn lẻ hai mươi tư"


def test_abbreviation(norm: TextNormalizer):
    assert norm.normalize("TP.HCM") == "thành phố Hồ Chí Minh"


def test_abbreviation_academic(norm: TextNormalizer):
    assert norm.normalize("PGS.TS Lan") == "phó giáo sư tiến sĩ Lan"


def test_decimal(norm: TextNormalizer):
    assert norm.normalize("1.5") == "một phẩy năm"


def test_leaves_plain_words_untouched(norm: TextNormalizer):
    text = "bà Ngoại gánh hàng rong qua chợ"
    assert norm.normalize(text) == text


def test_module_level_function():
    assert normalize_text("10 giờ") == "mười giờ"
