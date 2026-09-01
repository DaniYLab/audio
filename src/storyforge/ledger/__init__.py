"""Fact ledger — the established facts of the written story world.

KB = world of the SOURCES (podcasts). Ledger = world of the STORY as
written across episodes. The two stores are never mixed; the only shared
key is the alias table (FACT_LEDGER_DESIGN.md §5).

Layout (M3 §1.1):

    data/ledgers/<universe>/
    ├── s0/ep_001.yaml     # facts established in that episode
    ├── s1/ep_101.yaml
    └── audit.log          # JSONL append-only record of every mutation
"""

from __future__ import annotations

from storyforge.ledger.loader import UniverseLedger, load_universe
from storyforge.ledger.store import AuditEntry, YamlLedgerStore, build_ledger

__all__ = [
    "AuditEntry",
    "UniverseLedger",
    "YamlLedgerStore",
    "build_ledger",
    "load_universe",
]
