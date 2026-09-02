"""Per-run metrics recorder (T1-DEV1, M2-8).

Stages and providers record cost/token metrics into the current run's
recorder via a contextvar; the pipeline flushes the recorder into the
manifest after each stage. ``core/cost.py`` then aggregates those metrics
against ``config/prices.yaml``.

Usage::

    from storyforge.core.metrics import current_run_recorder

    rec = current_run_recorder()
    if rec is not None:
        rec.record("story", llm_input_tokens=123, llm_output_tokens=45,
                   api_calls=1, cost_usd=0.0012)
"""

from __future__ import annotations

from contextvars import ContextVar

_current_recorder: ContextVar[MetricsRecorder | None] = ContextVar(
    "storyforge_current_run_recorder", default=None
)


class MetricsRecorder:
    """Accumulates metrics per stage for one pipeline run.

    Not thread-safe by design: one run = one worker thread (M4-A6 queue
    runs jobs sequentially within a worker).
    """

    def __init__(self) -> None:
        self._metrics: dict[str, dict[str, object]] = {}

    def record(self, stage: str, **metrics: float | int | str) -> None:
        """Merge ``metrics`` into the stage bucket."""
        bucket = self._metrics.setdefault(stage, {})
        for key, value in metrics.items():
            existing = bucket.get(key)
            if isinstance(existing, int | float) and isinstance(value, int | float):
                bucket[key] = existing + value
            else:
                bucket[key] = value

    def snapshot(self, stage: str) -> dict[str, object]:
        """Return a copy of the stage bucket (empty dict when absent)."""
        return dict(self._metrics.get(stage, {}))

    def flush(self) -> dict[str, dict[str, object]]:
        """Return all buckets and reset (called once per stage boundary)."""
        result, self._metrics = self._metrics, {}
        return result


def current_run_recorder() -> MetricsRecorder | None:
    """The recorder bound to the current run, or None outside a run."""
    return _current_recorder.get()


def bind_run_recorder(recorder: MetricsRecorder) -> None:
    """Bind a recorder to the current execution context (pipeline entry)."""
    _current_recorder.set(recorder)


def reset_run_recorder() -> None:
    """Clear the current run recorder (pipeline exit)."""
    _current_recorder.set(None)


def flush_into_manifest(recorder: MetricsRecorder, manifest: object) -> None:
    """Merge the recorder's buckets into the run manifest's stage records.

    Called by the pipeline after each stage so that even a failed run keeps
    the metrics recorded up to the failure point. ``manifest`` is duck-typed
    (has ``stages: dict[str, StageRecord]``) to avoid a hard core.types
    import cycle.
    """
    for stage, metrics in recorder.flush().items():
        record = getattr(manifest, "stages", {}).get(stage)
        if record is not None:
            record.metrics.update(metrics)


__all__ = [
    "MetricsRecorder",
    "bind_run_recorder",
    "current_run_recorder",
    "flush_into_manifest",
    "reset_run_recorder",
]
