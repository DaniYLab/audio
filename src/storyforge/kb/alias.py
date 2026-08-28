"""Alias table per universe — product data, not an accessory (design v4 §5).

``data/kb/<universe>/aliases.yaml`` holds canonical entities, their alias
variants, provenance, and review status. New aliases discovered by ingest land
as ``pending`` and are NEVER auto-merged. The same table normalizes fact
ledger subjects (FACT_LEDGER_DESIGN.md §5) — one table, two worlds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from storyforge.core.exceptions import KnowledgeBaseError
from storyforge.core.logging import get_logger

logger = get_logger(__name__)


class AliasEntry:
    """One canonical entity with its alias variants."""

    def __init__(
        self,
        canonical: str,
        aliases: list[str],
        type_: str = "other",
        status: str = "confirmed",
        added_by: str = "human",
    ) -> None:
        self.canonical = canonical
        self.aliases = aliases
        self.type = type_
        self.status = status  # confirmed | pending
        self.added_by = added_by  # human | llm | ledger

    def matches(self, name: str) -> bool:
        lowered = name.strip().lower()
        return lowered == self.canonical.lower() or lowered in {a.lower() for a in self.aliases}


class AliasStore:
    """YAML-backed alias table for one universe (≤ ~200 entities by design)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: list[AliasEntry] | None = None

    # -- read ---------------------------------------------------------------

    def _load(self) -> list[AliasEntry]:
        if self._entries is not None:
            return self._entries
        if not self.path.exists():
            self._entries = []
            return self._entries
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or []
        except yaml.YAMLError as exc:
            raise KnowledgeBaseError(
                f"corrupted alias table: {self.path}", details={"error": str(exc)}
            ) from exc
        entries: list[AliasEntry] = []
        if isinstance(raw, list):
            for row in raw:
                if not isinstance(row, dict) or "canonical" not in row:
                    continue
                entries.append(
                    AliasEntry(
                        canonical=str(row["canonical"]),
                        aliases=[str(a) for a in row.get("aliases", [])],
                        type_=str(row.get("type", "other")),
                        status=str(row.get("status", "confirmed")),
                        added_by=str(row.get("added_by", "human")),
                    )
                )
        self._entries = entries
        return entries

    def resolve(self, name: str) -> str | None:
        """Return the canonical name for ``name`` (case-insensitive), else None."""
        for entry in self._load():
            if entry.matches(name):
                return entry.canonical
        return None

    def entry_for(self, name: str) -> AliasEntry | None:
        for entry in self._load():
            if entry.matches(name):
                return entry
        return None

    def all_entries(self) -> list[AliasEntry]:
        return list(self._load())

    # -- write ---------------------------------------------------------------

    def add_pending(self, name: str, type_: str, provenance: str = "llm") -> bool:
        """Register an unseen entity as pending. Returns True if newly added.

        An entity counts as seen when any existing entry's canonical name or
        alias matches — case-insensitively, since ASR drops diacritics.
        """
        entries = self._load()
        if any(entry.matches(name) for entry in entries):
            return False
        entries.append(
            AliasEntry(
                canonical=name,
                aliases=[],
                type_=type_,
                status="pending",
                added_by=provenance,
            )
        )
        self._entries = entries
        return True

    def save(self) -> None:
        rows: list[dict[str, Any]] = [
            {
                "canonical": e.canonical,
                "aliases": e.aliases,
                "type": e.type,
                "status": e.status,
                "added_by": e.added_by,
            }
            for e in self._load()
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            yaml.safe_dump(rows, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    # -- bootstrap report (design v4 §5.3) ------------------------------------

    def unmapped_report(self, mentions: dict[str, int], top: int = 50) -> list[dict[str, Any]]:
        """Top-N frequently mentioned tokens not mapped to any entry."""
        unmapped = {
            name: count
            for name, count in mentions.items()
            if not any(entry.matches(name) for entry in self._load())
        }
        ranked = sorted(unmapped.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
        return [{"name": n, "mentions": c} for n, c in ranked]
