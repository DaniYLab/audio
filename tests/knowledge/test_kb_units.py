"""KB unit tests — pure logic, no network, no models (CONVENTIONS.md §9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from storyforge.core.types import (
    SourceRef,
    Transcript,
    TranscriptSegment,
)
from storyforge.kb.alias import AliasStore
from storyforge.kb.entities import extract_entities, guess_type
from storyforge.kb.ingest import content_hash, prepare
from storyforge.kb.types import SearchQuery


@pytest.fixture()
def alias_path(tmp_path: Path) -> Path:
    return tmp_path / "kb" / "demo" / "aliases.yaml"


@pytest.fixture()
def alias(alias_path: Path) -> AliasStore:
    return AliasStore(alias_path)


@pytest.fixture()
def transcript() -> Transcript:
    return Transcript(
        source=SourceRef(id="ep01", title="Sample podcast", duration_seconds=120),
        language="vi",
        audio_path=Path("fake.m4a"),
        segments=[
            TranscriptSegment(
                start=0.0, end=5.0, text="Hôm nay bà Ngoại kể chuyện xưa.", speaker="SPEAKER_00"
            ),
            TranscriptSegment(
                start=5.0,
                end=10.0,
                text="Bà Ngoại gánh hàng rong qua chợ Đồng Xuân.",
                speaker="SPEAKER_00",
            ),
            TranscriptSegment(
                start=10.0, end=15.0, text="Lan theo bà ra chợ từ tờ mờ đất.", speaker="SPEAKER_01"
            ),
        ],
    )


# --- entities.py ---------------------------------------------------------------


def test_extracts_kinship_title_names():
    found = extract_entities("Hôm qua bà Ngoại và ông Tư ngồi uống trà.")
    assert "bà Ngoại" in found
    assert "ông Tư" in found


def test_extracts_place_suffix_entities():
    found = extract_entities("Chúng tôi đến chợ Đồng Xuân rất sớm.")
    assert "chợ Đồng Xuân" in found


def test_skets_first_word_of_sentence():
    # "Bà" at sentence start should not produce a bare-capital false positive;
    # the kinship rule still catches the full "bà Ngoại".
    found = extract_entities("Bà Ngoại tốt lắm. Lan thương bà.")
    assert "bà Ngoại" in found


def test_guess_type_person_and_place():
    assert guess_type("bà Ngoại") == "person"
    assert guess_type("chợ Đồng Xuân") == "place"
    assert guess_type("Vũ Đại làng") == "place"


# --- alias.py -------------------------------------------------------------------


def test_alias_resolve_case_insensitive(alias: AliasStore):
    alias.add_pending("Bà Ngoại", "person")
    assert alias.resolve("bà ngoại") == "Bà Ngoại"
    assert alias.resolve("BÀ NGOẠI") == "Bà Ngoại"


def test_alias_add_pending_is_idempotent(alias: AliasStore):
    assert alias.add_pending("Lan", "person") is True
    assert alias.add_pending("lan", "person") is False  # case-insensitive dedup


def test_alias_save_and_reload(alias_path: Path, alias: AliasStore):
    alias.add_pending("Bà Ngoại", "person")
    alias.save()
    reloaded = AliasStore(alias_path)
    assert reloaded.resolve("bà ngoại") == "Bà Ngoại"
    entry = reloaded.entry_for("Bà Ngoại")
    assert entry is not None and entry.status == "pending"


def test_unmapped_report_ranks_by_frequency(alias: AliasStore):
    alias.add_pending("Lan", "person")
    report = alias.unmapped_report({"Lan": 10, "Hà Giang": 3, "ông Tư": 7})
    names = [row["name"] for row in report]
    assert "Lan" not in names
    assert names.index("ông Tư") < names.index("Hà Giang")


# --- ingest.py -------------------------------------------------------------------


def test_content_hash_stable_and_content_sensitive(transcript: Transcript):
    first = content_hash(transcript)
    second = content_hash(transcript)
    assert first == second
    transcript.segments[0].text = "Khác với ban đầu."
    assert content_hash(transcript) != first


def test_prepare_stamps_universe_and_hash(transcript: Transcript, alias: AliasStore):
    prepared = prepare(
        transcript,
        universe_id="demo",
        alias=alias,
        chunk_size_tokens=600,
        overlap_tokens=90,
    )
    assert prepared.universe_id == "demo"
    assert prepared.content_hash
    assert prepared.chunks
    for chunk in prepared.chunks:
        assert chunk.metadata["universe_id"] == "demo"
        assert chunk.metadata["content_hash"] == prepared.content_hash
        # Payload carries the full retrieval metadata set (design v4 §3).
        for key in ("source_id", "start_ts", "end_ts", "speaker", "language", "entities"):
            assert key in chunk.metadata


def test_prepare_normalizes_entities_via_alias(transcript: Transcript, alias: AliasStore):
    alias.add_pending("Bà Ngoại", "person")
    # Simulate a human-confirmed alias entry: lowercase variant -> canonical.
    from storyforge.kb.alias import AliasEntry

    alias._entries = [AliasEntry("Bà Ngoại", ["ba Ngoai", "ngoại"], type_="person")]
    prepared = prepare(
        transcript,
        universe_id="demo",
        alias=alias,
        chunk_size_tokens=600,
        overlap_tokens=90,
    )
    all_entities = {e for c in prepared.chunks for e in c.metadata["entities"]}
    assert "Bà Ngoại" in all_entities


def test_prepare_registers_new_entities_as_pending(transcript: Transcript, alias: AliasStore):
    prepare(
        transcript,
        universe_id="demo",
        alias=alias,
        chunk_size_tokens=600,
        overlap_tokens=90,
    )
    assert prepared_entities(alias) != []


def prepared_entities(alias: AliasStore) -> list[str]:
    return [e.canonical for e in alias.all_entries()]


# --- types.py ---------------------------------------------------------------------


def test_search_query_defaults():
    query = SearchQuery(text="tuổi thơ")
    assert query.intent.value == "theme"
    assert query.top_k == 8
    assert query.group_by_source is False
