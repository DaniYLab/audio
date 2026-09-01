"""LLM Arbiter escalation (M4-B4) — port of the ainovel-cli Arbiter pattern.

Rule-based ``find_conflicts`` is decisive most of the time. When it is NOT
(same slot, different statement, ambiguous negation) the ledger is in an
uncertain state and the arbiter is asked — exactly ONE LLM call per episode,
using the writer model. Every decision is written append-only to
``meta/conflict_verdicts.jsonl`` so any verdict can be replayed and audited.

Failure policy (AC3): if the LLM call fails/times out, fall back to the
conservative rule-based default: CONFLICT.

Gate (AC4): ``SF__LEDGER__ARBITER_ENABLED`` defaults to false; enable only
after a rule-based baseline exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from storyforge.core.config import Settings
from storyforge.core.logging import get_logger
from storyforge.core.types import utc_now
from storyforge.kb.types import ConflictReport, ConflictVerdict, Fact
from storyforge.ledger.store import LedgerStore

logger = get_logger(__name__)

ArbiterCall = Callable[[Fact, list[Fact]], "ArbiterVerdict"]


class ArbiterVerdict:
    """{verdict: conflict|no_conflict, reason} returned by the arbiter LLM."""

    def __init__(self, verdict: str, reason: str) -> None:
        self.verdict = verdict  # "conflict" | "no_conflict"
        self.reason = reason


class ArbiterLedgerStore:
    """Wrapper around a ledger store that escalates uncertain conflicts.

    Implements ``LedgerStore``; all mutations/reads delegate to the inner
    store. Only ``find_conflicts`` is intercepted.
    """

    def __init__(
        self,
        inner: LedgerStore,
        *,
        enabled: bool,
        arbiter: ArbiterCall | None,
        verdicts_path: Path,
    ) -> None:
        self._inner = inner
        self._enabled = enabled
        self._arbiter = arbiter
        self._verdicts_path = verdicts_path

    # -- delegation -----------------------------------------------------------

    def record_episode(self, episode_id: str, facts: list[Fact]) -> None:
        self._inner.record_episode(episode_id, facts)

    def query(
        self,
        subject: str | None = None,
        kind: object | None = None,
        include_superseded: bool = False,
    ) -> list[Fact]:
        return self._inner.query(subject, kind, include_superseded)  # type: ignore[arg-type]

    def supersede(self, fact_id: str, replacement: Fact, *, actor: str) -> None:
        self._inner.supersede(fact_id, replacement, actor=actor)

    def get_audit_log(self, fact_id: str | None = None) -> list[object]:
        return list(self._inner.get_audit_log(fact_id))  # type: ignore[arg-type]

    # -- interception -----------------------------------------------------------

    def find_conflicts(self, candidate: Fact) -> ConflictReport:
        base = self._inner.find_conflicts(candidate)
        if not self._enabled or base.verdict is not ConflictVerdict.NO_CONFLICT:
            return base

        uncertain = self._inner.find_uncertain(candidate)  # type: ignore[attr-defined]
        if not uncertain:
            return base

        verdict = self._ask(candidate, uncertain)
        self._append(candidate, uncertain, verdict)

        if verdict.verdict == "conflict":
            return ConflictReport(
                candidate=candidate,
                conflicts=uncertain,
                verdict=ConflictVerdict.CONFLICT,
                reason=verdict.reason,
            )
        return ConflictReport(
            candidate=candidate,
            verdict=ConflictVerdict.NO_CONFLICT,
            reason=verdict.reason,
        )

    # -- internals ---------------------------------------------------------------

    def _ask(self, candidate: Fact, uncertain: list[Fact]) -> ArbiterVerdict:
        if self._arbiter is None:
            logger.warning("arbiter enabled but no arbiter callable — conservative CONFLICT")
            return ArbiterVerdict("conflict", "arbiter unavailable — conservative fallback")
        try:
            return self._arbiter(candidate, uncertain)
        except Exception as exc:
            logger.warning("arbiter LLM failed — conservative CONFLICT", error=str(exc))
            return ArbiterVerdict("conflict", "arbiter failed — conservative fallback")

    def _append(self, candidate: Fact, uncertain: list[Fact], verdict: ArbiterVerdict) -> None:
        row = {
            "episode": candidate.episode_id,
            "candidate": {
                "fact_id": candidate.fact_id,
                "statement": candidate.statement,
                "kind": candidate.kind.value,
                "subject": candidate.subject,
            },
            "existing": [f.fact_id for f in uncertain],
            "verdict": verdict.verdict,
            "reason": verdict.reason,
            "created_at": utc_now().isoformat(),
        }
        self._verdicts_path.parent.mkdir(parents=True, exist_ok=True)
        with self._verdicts_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def llm_arbiter(settings: Settings) -> ArbiterCall:
    """Default arbiter: writer model + prompts/arbiter_conflict.txt, 1 call.

    Parsed with the shared JSON extractor (M2 §3.2.1) — LLM responses are
    wrapped in ```json fences more often than not.
    """

    def arbitrate(candidate: Fact, uncertain: list[Fact]) -> ArbiterVerdict:
        from storyforge.providers.llm import (
            LLMClient,
            extract_json,
            fill_prompt,
            load_prompt,
        )

        model = settings.ledger.arbiter_model or settings.llm.writer_model
        client = LLMClient(settings, model)
        template = load_prompt("arbiter_conflict")
        existing = "\n".join(f"- ({f.fact_id}) {f.statement}" for f in uncertain)
        response = client.chat(
            system=fill_prompt(template, {"language": "vi"}),
            user=fill_prompt(
                template,
                {
                    "candidate": candidate.statement,
                    "subject": candidate.subject,
                    "kind": candidate.kind.value,
                    "existing": existing,
                },
            ),
        )
        data = extract_json(response)
        verdict = str(data.get("verdict", "conflict")).lower()
        reason = str(data.get("reason", ""))
        if verdict not in ("conflict", "no_conflict"):
            raise ValueError(f"arbiter returned unknown verdict: {verdict}")
        return ArbiterVerdict(verdict, reason)

    return arbitrate


def build_arbiter_ledger(
    settings: Settings,
    universe_dir: Path,
    inner: LedgerStore,
    arbiter: ArbiterCall | None = None,
) -> LedgerStore:
    """Wrap a ledger with the arbiter when enabled; otherwise pass through."""
    if not settings.ledger.arbiter_enabled:
        return inner
    verdicts_path = universe_dir / "meta" / "conflict_verdicts.jsonl"
    return ArbiterLedgerStore(
        inner,
        enabled=True,
        arbiter=arbiter or llm_arbiter(settings),
        verdicts_path=verdicts_path,
    )
