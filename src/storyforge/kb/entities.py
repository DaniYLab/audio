"""Rule-based entity/alias extraction for the ingest write path.

No LLM in the default write path (design v4 §5): ASR-spoken Vietnamese makes
LLM NER unstable and ingest must run unattended at night. Rules:
  1. Kinship-title + Name patterns ("bà Ngoại", "ông Tư", "chú Ba", "cô Lan")
  2. Place suffix + Name ("chợ Đồng Xuân") or Name + suffix ("Vũ Đại làng")
  3. Capitalized tokens not at sentence start / not common stopwords

Token-based matching (``str.isupper``) instead of Unicode character ranges:
ranges like ``[À-Ỹ]`` silently include lowercase Vietnamese letters ("đ"),
which produced false names such as "chợ đến".
"""

from __future__ import annotations

import re

# Vietnamese kinship / address titles that prefix a proper name.
_KINSHIP_TITLES = frozenset(
    {
        "bà",
        "ông",
        "anh",
        "chị",
        "chú",
        "cô",
        "dì",
        "thím",
        "bác",
        "em",
        "cậu",
        "mợ",
        "thầy",
        "tổ",
    }
)

# Common place suffixes: suffix + name or name + suffix = place entity.
_PLACE_SUFFIXES = frozenset({"chợ", "làng", "xóm", "thôn", "phố", "sông", "núi", "đồi"})

# Words that look like names (capitalized mid-sentence) but never are.
_STOPWORDS = frozenset(
    {
        "Ngày",
        "Tháng",
        "Năm",
        "Hôm",
        "Đây",
        "Đó",
        "Khi",
        "Và",
        "Nhưng",
        "Rồi",
        "Còn",
        "Với",
        "Trong",
        "Ngoài",
        "Từ",
        "Chúng",
        "Câu",
        "Tôi",
    }
)

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _is_name(word: str) -> bool:
    """A proper-name-shaped word: first letter upper, the rest lower."""
    return len(word) >= 2 and word[0].isupper() and any(c.islower() for c in word[1:])


def _is_generic(word: str) -> bool:
    return word.lower() in _KINSHIP_TITLES or word.lower() in _PLACE_SUFFIXES or word in _STOPWORDS


def extract_entities(text: str) -> list[str]:
    """Return distinct entity mentions found in one chunk of raw text."""
    found: list[str] = []
    for sentence in re.split(r"[.!?]+", text):
        words = _WORD_RE.findall(sentence)
        i = 0
        while i < len(words):
            word = words[i]
            lowered = word.lower()

            # Kinship title + Name ("bà Ngoại", "ông Tư").
            if lowered in _KINSHIP_TITLES and i + 1 < len(words) and _is_name(words[i + 1]):
                found.append(f"{lowered} {words[i + 1]}")
                i += 2
                continue

            # Place: suffix + Name(s) ("chợ Đồng Xuân") — greedily take the
            # following run of name-shaped words.
            if lowered in _PLACE_SUFFIXES and i + 1 < len(words) and _is_name(words[i + 1]):
                name_words = [words[i + 1]]
                j = i + 2
                while j < len(words) and _is_name(words[j]) and not _is_generic(words[j]):
                    name_words.append(words[j])
                    j += 1
                found.append(f"{lowered} {' '.join(name_words)}")
                i = j
                continue

            # Place: Name + suffix ("Vũ Đại làng") — takes the preceding name run.
            if _is_name(word) and i + 1 < len(words) and words[i + 1].lower() in _PLACE_SUFFIXES:
                start = i
                while (
                    start > 0 and _is_name(words[start - 1]) and not _is_generic(words[start - 1])
                ):
                    start -= 1
                found.append(f"{' '.join(words[start:i])} {words[i + 1].lower()}")
                i += 2
                continue

            # Bare capitalized token: skip the first word of each sentence
            # (capitalized by convention, not because it is a name).
            if i > 0 and _is_name(word) and not _is_generic(word):
                found.append(word)

            i += 1

    # Preserve first-seen order, dedup exact duplicates.
    seen: set[str] = set()
    ordered: list[str] = []
    for name in found:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def guess_type(entity: str) -> str:
    """Guess person | place | other from the mention's shape."""
    lowered = entity.lower()
    first = lowered.split()[0] if lowered.split() else ""
    if first in _KINSHIP_TITLES:
        return "person"
    if any(lowered.endswith(f" {s}") or lowered.startswith(f"{s} ") for s in _PLACE_SUFFIXES):
        return "place"
    return "other"
