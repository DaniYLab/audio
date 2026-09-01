"""M3-V1 ledger tests — record/query/supersede/conflicts/negation/ordering."""

from __future__ import annotations

from pathlib import Path

import pytest

from storyforge.kb.types import Fact, FactKind, FactOrigin
from storyforge.ledger import build_ledger, load_universe


def _fact(
    fact_id: str,
    statement: str,
    *,
    kind: FactKind = FactKind.CHARACTER,
    subject: str = "Bà Ngoại",
    episode: str = "ep_001",
    origin: FactOrigin = FactOrigin.INVENTED,
) -> Fact:
    return Fact(
        fact_id=fact_id,
        kind=kind,
        subject=subject,
        statement=statement,
        origin=origin,
        chunk_refs=["ep01:0001"] if origin is FactOrigin.CITED else [],
        episode_id=episode,
    )


@pytest.fixture()
def ledger(tmp_path: Path):
    return build_ledger(tmp_path / "ledgers" / "storyvu")


def test_record_and_query_roundtrip(ledger):
    facts = [
        _fact("f0001", "Bà Ngoại còn sống, ở cùng Lan"),
        _fact("f0002", "Lan là cháu ngoại của Bà Ngoại", subject="Lan"),
    ]
    ledger.record_episode("ep_001", facts)
    live = ledger.query()
    assert {f.fact_id for f in live} == {"f0001", "f0002"}
    by_subject = ledger.query(subject="Lan")
    assert [f.fact_id for f in by_subject] == ["f0002"]


def test_query_filters_by_kind(ledger):
    ledger.record_episode(
        "ep_001",
        [
            _fact("f0001", "Bà Ngoại gánh hàng rong"),
            _fact(
                "f0002",
                "Chợ Đồng Xuân họp từ tờ mờ đất",
                kind=FactKind.SETTING,
                subject="Chợ Đồng Xuân",
            ),
        ],
    )
    assert [f.fact_id for f in ledger.query(kind=FactKind.SETTING)] == ["f0002"]


def test_query_excludes_superseded_and_orders_newest_first(ledger):
    ledger.record_episode("ep_001", [_fact("f0001", "Bà Ngoại còn sống")])
    ledger.record_episode("ep_002", [_fact("f0002", "Lan tìm thấy chiếc lá đỏ")])
    replacement = _fact("f0041", "Bà Ngoại đã qua đời mùa đông năm ấy", episode="ep_015")
    ledger.supersede("f0001", replacement, actor="human")

    live = ledger.query()
    # Newest episode first: ep015 (f0041) before ep002 (f0002).
    assert [f.fact_id for f in live] == ["f0041", "f0002"]
    history = ledger.query(include_superseded=True)
    assert {f.fact_id for f in history} == {"f0001", "f0002", "f0041"}


def test_record_rejects_duplicate_fact_id(ledger):
    ledger.record_episode("ep_001", [_fact("f0001", "Bà Ngoại còn sống")])
    with pytest.raises(ValueError, match="duplicate"):
        ledger.record_episode("ep_002", [_fact("f0001", "Lan ở nhà")])
    with pytest.raises(ValueError, match="duplicate"):
        ledger.record_episode("ep_002", [_fact("f0003", "A"), _fact("f0003", "B")])


def test_conflict_alive_vs_dead(ledger):
    ledger.record_episode("ep_003", [_fact("f0007", "Bà Ngoại còn sống")])
    candidate = _fact("f0041", "Bà Ngoại đã qua đời mùa đông năm ấy", episode="ep_015")
    report = ledger.find_conflicts(candidate)
    assert report.verdict.value == "conflict"
    assert [f.fact_id for f in report.conflicts] == ["f0007"]


def test_negation_detection_no_trap(ledger):
    """'không còn sống' must read as DEAD (negation window), not ALIVE."""
    ledger.record_episode("ep_003", [_fact("f0007", "Bà Ngoại không còn sống")])
    candidate = _fact("f0041", "Bà Ngoại qua đời mùa đông", episode="ep_015")
    report = ledger.find_conflicts(candidate)
    # Same polarity (both dead) -> complementary, NOT a conflict.
    assert report.verdict.value == "no_conflict"


def test_negation_detection_reversed_trap(ledger):
    ledger.record_episode("ep_003", [_fact("f0007", "Bà Ngoại còn sống")])
    candidate = _fact("f0041", "Bà Ngoại không còn sống sau cơn bão", episode="ep_015")
    report = ledger.find_conflicts(candidate)
    assert report.verdict.value == "conflict"


def test_duplicate_statement_is_no_conflict(ledger):
    ledger.record_episode("ep_003", [_fact("f0007", "Bà Ngoại còn sống")])
    candidate = _fact("f0041", "Bà Ngoại còn sống", episode="ep_015")
    report = ledger.find_conflicts(candidate)
    assert report.verdict.value == "no_conflict"


def test_complementary_fact_is_no_conflict(ledger):
    ledger.record_episode("ep_003", [_fact("f0007", "Bà Ngoại gánh hàng rong")])
    candidate = _fact("f0041", "Bà Ngoại hay kể chuyện buổi chiều", episode="ep_015")
    report = ledger.find_conflicts(candidate)
    assert report.verdict.value == "no_conflict"


def test_supersede_writes_audit_and_keeps_history(ledger):
    ledger.record_episode("ep_003", [_fact("f0007", "Bà Ngoại còn sống")])
    replacement = _fact("f0041", "Bà Ngoại đã qua đời", episode="ep_015")
    ledger.supersede("f0007", replacement, actor="human")

    entries = ledger.get_audit_log(fact_id="f0007")  # oldest first
    supersede_entries = [e for e in entries if e.action == "supersede"]
    assert supersede_entries and supersede_entries[0].actor == "human"
    assert supersede_entries[0].before == "Bà Ngoại còn sống"
    assert supersede_entries[0].after == "Bà Ngoại đã qua đời"

    # The replacement fact lands in its own episode file and becomes live.
    assert [f.fact_id for f in ledger.query(subject="Bà Ngoại")] == ["f0041"]

    # The superseded fact's statement is untouched on disk.
    raw = (ledger.universe_dir / "s0" / "ep003.yaml").read_text(encoding="utf-8")
    assert "Bà Ngoại còn sống" in raw and "superseded_by: f0041" in raw


def test_supersede_unknown_fact_id(ledger):
    with pytest.raises(ValueError, match="unknown fact_id"):
        ledger.supersede("f9999", _fact("f0041", "x"), actor="human")


def test_episode_ordering_season2_after_season1(tmp_path: Path):
    ledger = build_ledger(tmp_path / "ledgers" / "vu")
    ledger.record_episode("s1ep_010", [_fact("f0010", "S1 fact", episode="s1ep_010")])
    ledger.record_episode("s2ep_001", [_fact("f0101", "S2 fact", episode="s2ep_001")])
    universe = load_universe(ledger.universe_dir)
    assert [(e.season, e.episode_number) for e in universe.episodes] == [(1, 10), (2, 1)]
    # query(): newest (s2) first.
    assert [f.fact_id for f in ledger.query()] == ["f0101", "f0010"]


def test_corrupt_file_skipped_not_fatal(tmp_path: Path):
    ledger = build_ledger(tmp_path / "ledgers" / "vu")
    ledger.record_episode("ep_001", [_fact("f0001", "Bà Ngoại còn sống")])
    (ledger.universe_dir / "s0" / "ep002.yaml").write_text(
        "facts:\n  - fact_id: [broken\n", encoding="utf-8"
    )
    universe = load_universe(ledger.universe_dir)
    assert universe.skipped == ["ep002.yaml"]
    assert [f.fact_id for f in ledger.query()] == ["f0001"]  # ep001 still loads


def test_cited_fact_requires_chunk_refs():
    with pytest.raises(ValueError, match="chunk_refs"):
        Fact(
            fact_id="f0001",
            kind=FactKind.EVENT,
            subject="Lan",
            statement="Lan mất chiếc lá",
            origin=FactOrigin.CITED,
            chunk_refs=[],
            episode_id="ep_001",
        )


def test_extract_prompt_placeholder_exists():
    prompt = Path("prompts/review_extract.txt")
    assert prompt.exists()
