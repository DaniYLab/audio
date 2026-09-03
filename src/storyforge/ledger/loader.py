"""Load a whole universe ledger into memory (M3-V1).

Scale: a few hundred facts per universe — Python filtering over an
in-memory list is milliseconds; no index engine needed (design §2.2).

Episode ordering (design §4): release order, not dates. ``s2/ep001``
sorts after ``s1/ep010``; the canonical sort key is
``(season, episode_number)`` parsed from the directory/file names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from storyforge.core.exceptions import LedgerCorruptedError
from storyforge.core.logging import get_logger
from storyforge.kb.types import Fact

logger = get_logger(__name__)

_SEASON_DIR_RE = re.compile(r"^s(\d+)$")
_EPISODE_FILE_RE = re.compile(r"^ep(\d+)\.ya?ml$")


@dataclass
class EpisodeFile:
    """One on-disk episode ledger file and its position in release order."""

    path: Path
    season: int
    episode_number: int
    facts: list[Fact] = field(default_factory=list)

    @property
    def episode_id(self) -> str:
        return f"s{self.season}ep{self.episode_number:03d}"

    @property
    def order_key(self) -> tuple[int, int]:
        return (self.season, self.episode_number)


@dataclass
class UniverseLedger:
    """All parseable episode files of one universe, in release order."""

    root: Path
    episodes: list[EpisodeFile] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # corrupt file names

    @property
    def all_facts(self) -> list[Fact]:
        facts: list[Fact] = []
        for episode in self.episodes:
            facts.extend(episode.facts)
        return facts


def episode_key(episode_id: str) -> tuple[int, int] | None:
    """Parse an episode anchor (``ep_012`` / ``s2ep_101``) into its order key.

    Returns None when the id is not a parseable anchor (facts without one are
    excluded from bi-temporal views).
    """
    match = re.fullmatch(r"(?:s(\d+))?(?:ep[_-]?(\d+))", episode_id.strip().lower())
    if match is None or match.group(2) is None:
        return None
    season = int(match.group(1)) if match.group(1) is not None else 0
    return (season, int(match.group(2)))


def facts_as_of(universe: UniverseLedger, as_of_episode: str) -> list[Fact]:
    """Facts still valid at ``as_of_episode`` (M6-V2 bi-temporal view).

    A fact is valid at N when it was established at or before N AND not yet
    superseded by a replacement established at or before N. Facts are returned
    in release order (recap/writer consume the tail as "most recent").
    """
    anchor = episode_key(as_of_episode)
    if anchor is None:
        # Unknown anchor behaves like "current canon": superseded facts drop.
        return [f for f in universe.all_facts if f.superseded_by is None]

    by_id = {f.fact_id: f for f in universe.all_facts}
    out: list[Fact] = []
    for episode in universe.episodes:
        if episode.order_key > anchor:
            break  # nothing established after the anchor is visible
        for fact in episode.facts:
            if fact.superseded_by is None:
                out.append(fact)
                continue
            replacement = by_id.get(fact.superseded_by)
            if replacement is None:
                continue  # superseded by an unknown fact -> not canon
            rep_key = episode_key(replacement.episode_id) if replacement.episode_id else None
            if rep_key is None or rep_key <= anchor:
                continue  # replacement established at or before the anchor
            out.append(fact)  # replacement lands after the anchor → old fact still valid
    return out


def load_universe(root: Path) -> UniverseLedger:
    """Parse every ``s<N>/ep<M>.yaml`` under ``root``.

    One corrupt file fails THAT episode (skipped + warned, recorded in
    ``skipped``) — never the universe (design §1.4).
    """
    universe = UniverseLedger(root=root)
    if not root.exists():
        return universe

    for season_dir in sorted(root.iterdir()):
        match = _SEASON_DIR_RE.match(season_dir.name)
        if not season_dir.is_dir() or match is None:
            continue
        season = int(match.group(1))
        for file in sorted(season_dir.iterdir()):
            ep_match = _EPISODE_FILE_RE.match(file.name)
            if not file.is_file() or ep_match is None:
                continue
            episode = EpisodeFile(path=file, season=season, episode_number=int(ep_match.group(1)))
            try:
                episode.facts = _load_facts(file)
            except LedgerCorruptedError as exc:
                logger.warning("ledger file skipped", file=str(file), error=str(exc))
                universe.skipped.append(file.name)
                continue
            universe.episodes.append(episode)

    universe.episodes.sort(key=lambda e: e.order_key)
    return universe


def _load_facts(path: Path) -> list[Fact]:
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise LedgerCorruptedError(
            f"yaml error in {path.name}", details={"error": str(exc)}
        ) from exc

    if not isinstance(raw, dict):
        raise LedgerCorruptedError(f"{path.name} must contain a mapping")
    rows = raw.get("facts", [])
    if not isinstance(rows, list):
        raise LedgerCorruptedError(f"{path.name} 'facts' must be a list")

    facts: list[Fact] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise LedgerCorruptedError(f"{path.name} fact #{i} must be a mapping")
        try:
            facts.append(Fact.model_validate(row))
        except ValueError as exc:
            raise LedgerCorruptedError(
                f"{path.name} fact #{i} invalid", details={"error": str(exc)}
            ) from exc
    return facts
