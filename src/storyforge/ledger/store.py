"""YAML ledger store — the only M3 implementation (M3-V1).

Append-only per episode: shipped episodes are never rewritten; new facts
belong to the new episode. Corrections go through ``supersede`` with an
audit entry, never a delete (design §4).

``find_conflicts`` is rule-based for M3 (LLM-assist is a later CR):
  1. identical statement -> NO_CONFLICT (duplicate)
  2. same subject+kind AND same semantic slot with opposite polarity
     -> CONFLICT (e.g. alive vs dead)
  3. everything else -> NO_CONFLICT (complementary fact)

Negation detection (M3 §1.3 — mandatory per review): polarity keywords are
matched against the statement with a ~3-word lookbehind for negators, so
"không còn sống" counts as DEAD even though it contains "còn sống".
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from storyforge.core.types import utc_now
from storyforge.kb.types import ConflictReport, ConflictVerdict, Fact, FactKind
from storyforge.ledger.loader import EpisodeFile, UniverseLedger, load_universe

_NEGATORS = ("không", "chưa", "chẳng", "đã không", "không còn", "vẫn chưa")

# Slot tables: slot -> polarity -> keywords. Facts sharing a SLOT with
# DIFFERENT polarity conflict; same polarity = complementary (no conflict).
# "vitality" is one slot with two poles (alive/dead) — that is what makes
# alive-vs-dead a conflict rather than two unrelated slots.
_SLOTS: dict[FactKind, dict[str, dict[str, list[str]]]] = {
    FactKind.CHARACTER: {
        "vitality": {
            "alive": ["còn sống", "sống", "còn"],
            "dead": ["qua đời", "mất", "chết", "không còn sống"],
        },
        "whereabouts": {
            "present": ["ở cùng", "sống cùng", "đang ở"],
        },
        "occupation": {
            "job": ["nghề", "làm", "bán", "gánh"],
        },
    },
    FactKind.SETTING: {
        "name": {"named": ["tên", "gọi là"]},
        "location": {"placed": ["nằm", "ở", "thuộc"]},
        "activity": {"schedule": ["họp", "mở cửa", "buổi"]},
    },
    FactKind.EVENT: {
        "time": {"dated": ["hôm ấy", "mùa", "năm", "lúc"]},
        "outcome": {
            "lost": ["mất", "thua", "rơi"],
            "gained": ["được", "thắng"],
        },
    },
    FactKind.RELATION: {
        "kinship": {
            "related": ["là", "cháu", "con", "vợ", "chồng", "em", "anh", "chị"],
        },
    },
    FactKind.ITEM: {
        "ownership": {"owned": ["của", "thuộc về"]},
        "state": {
            "lost": ["mất", "đã cho"],
            "kept": ["còn"],
        },
    },
}

_NEGATION_WINDOW_WORDS = 3


class LedgerStore(Protocol):
    def record_episode(self, episode_id: str, facts: list[Fact]) -> None: ...

    def query(
        self,
        subject: str | None = None,
        kind: FactKind | None = None,
        include_superseded: bool = False,
        as_of_episode: str | None = None,  # M6-V2: facts valid at that episode
    ) -> list[Fact]: ...

    def find_conflicts(self, candidate: Fact) -> ConflictReport: ...

    def supersede(self, fact_id: str, replacement: Fact, *, actor: str) -> None: ...

    def get_audit_log(self, fact_id: str | None = None) -> list[AuditEntry]: ...


class AuditEntry:
    """One mutation record from the append-only audit log."""

    def __init__(
        self,
        ts: str,
        actor: str,
        action: str,
        fact_id: str,
        before: str | None = None,
        after: str | None = None,
    ) -> None:
        self.ts = ts
        self.actor = actor
        self.action = action
        self.fact_id = fact_id
        self.before = before
        self.after = after


class YamlLedgerStore:
    """FactLedger bound to one universe directory (``data/ledgers/<universe>``)."""

    def __init__(self, universe_dir: Path) -> None:
        self.universe_dir = universe_dir
        self._universe: UniverseLedger | None = None

    # -- loading ------------------------------------------------------------

    def _load(self) -> UniverseLedger:
        if self._universe is None:
            self._universe = load_universe(self.universe_dir)
        return self._universe

    def _reload(self) -> UniverseLedger:
        self._universe = load_universe(self.universe_dir)
        return self._universe

    # -- FactLedger protocol --------------------------------------------------

    def record_episode(self, episode_id: str, facts: list[Fact]) -> None:
        """Append facts of one FINAL episode. Rejects duplicate fact_ids.

        ``episode_id`` looks like ``ep_012`` (season 0) or ``s2ep_101`` —
        the file lands at ``s<season>/ep<number>.yaml``.
        """
        season, number = _parse_episode_id(episode_id)
        existing = {f.fact_id for f in self._load().all_facts}
        incoming = [f.fact_id for f in facts]
        duplicates = sorted(set(incoming) & existing)
        if duplicates:
            raise ValueError(f"duplicate fact_id(s): {duplicates}")
        if len(set(incoming)) != len(incoming):
            raise ValueError("duplicate fact_id(s) within the episode batch")

        payload = {
            "episode": episode_id,
            "facts": [f.model_dump(mode="json", exclude_none=True) for f in facts],
        }
        out_dir = self.universe_dir / f"s{season}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"ep{number:03d}.yaml"
        _atomic_write_yaml(out_path, payload)
        for fact in facts:
            self._audit(actor="reviewer", action="add", fact_id=fact.fact_id, after=fact.statement)
        self._reload()

    def query(
        self,
        subject: str | None = None,
        kind: FactKind | None = None,
        include_superseded: bool = False,
        as_of_episode: str | None = None,  # M6-V2: facts valid at that episode
    ) -> list[Fact]:
        """Live facts (superseded excluded by default), newest episode first.

        When ``as_of_episode`` is set, only facts that were established at or
        before that episode AND not yet superseded at that point are returned
        (M6-V2 bi-temporal — ``as_of`` answers "what did the ledger know at
        episode N?").
        """
        facts = self._load().all_facts
        if as_of_episode is None and not include_superseded:
            facts = [f for f in facts if f.superseded_by is None]
        if subject is not None:
            lowered = subject.strip().lower()
            facts = [f for f in facts if f.subject.lower() == lowered]
        if kind is not None:
            facts = [f for f in facts if f.kind is kind]
        if as_of_episode is not None:
            facts = self._filter_as_of(facts, as_of_episode)
        # Newest first: reverse release order, stable within an episode.
        return list(reversed(facts))

    def _filter_as_of(self, facts: list[Fact], as_of_episode: str) -> list[Fact]:
        """M6-V2: keep only facts VALID at the given episode.

        A fact is valid at episode N when it was established at or before N
        AND not superseded by a replacement established at or before N.
        An unparseable anchor behaves like "current time".
        """
        try:
            anchor = _parse_episode_id(as_of_episode)
        except ValueError:
            anchor = None  # unknown anchor -> current time
        # episode order of each fact's replacement (if any)
        superseded_orders: dict[str, tuple[int, int]] = {}
        for f in facts:
            if f.superseded_by:
                rep = _find_fact(f.superseded_by, self._load().episodes)
                if rep is not None:
                    with suppress(ValueError):
                        superseded_orders[f.fact_id] = _parse_episode_id(
                            getattr(rep, "episode_id", "")
                        )
        out: list[Fact] = []
        for f in facts:
            if anchor is None:
                if f.superseded_by:
                    continue  # current time: superseded facts are gone
            else:
                try:
                    f_order = _parse_episode_id(f.episode_id)
                except ValueError:
                    continue  # fact without a valid episode anchor -> skip
                if f_order > anchor:
                    continue  # established after the anchor
                if f.superseded_by:
                    sup_order = superseded_orders.get(f.fact_id)
                    if sup_order is None or sup_order <= anchor:
                        continue  # already superseded by the anchor (or unknown)
            out.append(f)
        return out

    def find_conflicts(self, candidate: Fact) -> ConflictReport:
        live = self.query(subject=candidate.subject, kind=candidate.kind)
        if not live:
            return ConflictReport(
                candidate=candidate, verdict=ConflictVerdict.NO_CONFLICT, reason="no prior facts"
            )

        normalized = _normalize_statement(candidate.statement)
        conflicts: list[Fact] = []
        reason = ""
        for fact in live:
            if _normalize_statement(fact.statement) == normalized:
                continue  # duplicate, not a conflict
            if _same_slot_opposite_polarity(candidate.statement, fact.statement, candidate.kind):
                conflicts.append(fact)
                reason = f"slot polarity clash with {fact.fact_id}"
        if conflicts:
            return ConflictReport(
                candidate=candidate,
                conflicts=conflicts,
                verdict=ConflictVerdict.CONFLICT,
                reason=reason,
            )
        return ConflictReport(
            candidate=candidate,
            verdict=ConflictVerdict.NO_CONFLICT,
            reason="complementary fact",
        )

    def find_uncertain(self, candidate: Fact) -> list[Fact]:
        """M4-B4: Live facts sharing a slot with the candidate (same polarity,
        different statement) — the rule engine cannot decide these; escalate
        to the LLM arbiter.

        Empty list means rule-based is decisive (NO_CONFLICT is safe).
        """
        live = self.query(subject=candidate.subject, kind=candidate.kind)
        normalized = _normalize_statement(candidate.statement)
        cand_slots = slot_polarities_for_kind(candidate.statement, candidate.kind)
        if not cand_slots:
            return []
        out: list[Fact] = []
        for fact in live:
            if _normalize_statement(fact.statement) == normalized:
                continue
            fact_slots = slot_polarities_for_kind(fact.statement, fact.kind)
            if set(cand_slots) & set(fact_slots):
                out.append(fact)
        return out

    def supersede(self, fact_id: str, replacement: Fact, *, actor: str) -> None:
        """Controlled retcon: point the old fact at the replacement.

        The replacement fact is appended to its own episode file (created if
        missing); the old fact's file is rewritten ONLY to set
        ``superseded_by`` — the statement itself is never touched.
        """
        universe = self._load()
        target_file: Path | None = None
        target_fact: Fact | None = None
        for episode in universe.episodes:
            for fact in episode.facts:
                if fact.fact_id == fact_id:
                    target_file, target_fact = episode.path, fact
                    break
        if target_file is None or target_fact is None:
            raise ValueError(f"unknown fact_id: {fact_id}")

        import yaml

        raw = yaml.safe_load(target_file.read_text(encoding="utf-8")) or {}
        for row in raw.get("facts", []):
            if isinstance(row, dict) and row.get("fact_id") == fact_id:
                row["superseded_by"] = replacement.fact_id
        _atomic_write_yaml(target_file, raw)

        # Append the replacement to its own episode file.
        season, number = _parse_episode_id(replacement.episode_id)
        replacement_path = self.universe_dir / f"s{season}" / f"ep{number:03d}.yaml"
        replacement_raw: dict[str, object] = {}
        if replacement_path.exists():
            replacement_raw = dict(
                yaml.safe_load(replacement_path.read_text(encoding="utf-8")) or {}
            )
        rows: list[dict[str, object]] = replacement_raw.setdefault("facts", [])  # type: ignore[assignment]
        rows.append(replacement.model_dump(mode="json", exclude_none=True))
        _atomic_write_yaml(replacement_path, replacement_raw)

        self._audit(
            actor=actor,
            action="supersede",
            fact_id=fact_id,
            before=target_fact.statement,
            after=replacement.statement,
        )
        self._reload()

    def get_audit_log(self, fact_id: str | None = None) -> list[AuditEntry]:
        log_path = self.universe_dir / "audit.log"
        if not log_path.exists():
            return []
        entries: list[AuditEntry] = []
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if fact_id is not None and row.get("fact_id") != fact_id:
                continue
            entries.append(
                AuditEntry(
                    ts=str(row.get("ts", "")),
                    actor=str(row.get("actor", "")),
                    action=str(row.get("action", "")),
                    fact_id=str(row.get("fact_id", "")),
                    before=row.get("before"),
                    after=row.get("after"),
                )
            )
        return entries

    # -- internals ------------------------------------------------------------

    def _audit(
        self,
        actor: str,
        action: str,
        fact_id: str,
        before: str | None = None,
        after: str | None = None,
    ) -> None:
        entry = {
            "ts": utc_now().isoformat(),
            "actor": actor,
            "action": action,
            "fact_id": fact_id,
        }
        if before is not None:
            entry["before"] = before
        if after is not None:
            entry["after"] = after
        self.universe_dir.mkdir(parents=True, exist_ok=True)
        with (self.universe_dir / "audit.log").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def build_ledger(universe_dir: Path) -> LedgerStore:
    return YamlLedgerStore(universe_dir)


def _episode_order(episodes: list[object], episode_id: str) -> int | None:
    """Monotonic order index for an episode_id based on its position in the
    release-sorted episode list (compared by (season, number), so "ep_010"
    and "s0ep010" are the same anchor). Unknown episodes return None."""
    try:
        target = _parse_episode_id(episode_id)
    except ValueError:
        return None
    for i, ep in enumerate(episodes):
        if getattr(ep, "order_key", None) == target:
            return i
    return None


def _find_fact(fact_id: str, episodes: Sequence[EpisodeFile]) -> Fact | None:
    """Find a fact by id across all episodes."""
    for ep in episodes:
        for fact in ep.facts:
            if fact.fact_id == fact_id:
                return fact
    return None


def _parse_episode_id(episode_id: str) -> tuple[int, int]:
    match = re.fullmatch(r"(?:s(\d+))?(?:ep[_-]?(\d+))", episode_id.strip().lower())
    if match is None or match.group(2) is None:
        raise ValueError(f"episode_id must look like 'ep_012' or 's2ep_101': {episode_id!r}")
    season = int(match.group(1)) if match.group(1) is not None else 0
    return season, int(match.group(2))


def _normalize_statement(statement: str) -> str:
    return re.sub(r"\s+", " ", statement.strip().lower()).rstrip(".")


def _same_slot_opposite_polarity(a: str, b: str, kind: FactKind) -> bool:
    """True when both statements hit the same semantic slot with opposite polarity."""
    a_hits = slot_polarities_for_kind(a, kind)
    b_hits = slot_polarities_for_kind(b, kind)
    shared = set(a_hits) & set(b_hits)
    return any(a_hits[slot] != b_hits[slot] for slot in shared)


def slot_polarities_for_kind(statement: str, kind: FactKind) -> dict[str, str]:
    """Map each slot the statement touches -> the polarity it expresses.

    Within one slot the FIRST matching polarity wins (keyword tables are
    ordered most-specific first: "còn sống" before "còn"). A negator within
    ~3 words before the keyword flips the polarity: on a two-pole slot the
    opposite pole ("not alive" ≡ dead); on single-pole slots the polarity
    becomes ``not_<polarity>``.
    """
    lowered = " " + statement.lower() + " "
    polarities: dict[str, str] = {}
    for slot, poles in _SLOTS.get(kind, {}).items():
        for polarity, keywords in poles.items():
            matched = [
                keyword
                for keyword in keywords
                if f" {keyword} " in lowered or lowered.strip().startswith(keyword + " ")
            ]
            if matched:
                negated = any(_negated(lowered, keyword) for keyword in matched)
                if negated and len(poles) == 2:
                    other = next(name for name in poles if name != polarity)
                    polarities[slot] = other
                elif negated:
                    polarities[slot] = f"not_{polarity}"
                else:
                    polarities[slot] = polarity
                break
    return polarities


def _negated(text: str, keyword: str) -> bool:
    """True when a negator sits within ~3 words before ``keyword``."""
    index = text.find(f" {keyword} ")
    if index == -1:
        index = 1 if text.strip().startswith(keyword) else -1
    if index == -1:
        return False
    window = text[max(0, index - 40) : index].strip()
    words = window.split()
    return any(word in _NEGATORS for word in words[-_NEGATION_WINDOW_WORDS:])


def _atomic_write_yaml(path: Path, payload: object) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(path)
