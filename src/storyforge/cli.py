"""StoryForge CLI — the only entrypoint that configures logging and wiring.

Commands:
    storyforge run      execute the full pipeline for one project
    storyforge resume   continue a partially-completed run (skips done stages)
    storyforge status   show the run manifest for a project

Design: the CLI is a thin shell. All logic lives in stages/providers; the CLI
only parses arguments, builds Settings, wires stage dependencies (what each
stage needs from upstream artifacts), and calls Pipeline.run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:
    from storyforge.kb.alias import AliasStore
    from storyforge.notify.webhook import WebhookDispatcher
    from storyforge.queue import JobSpec

import typer
from rich.console import Console
from rich.table import Table

from storyforge import __version__
from storyforge.core.config import Settings
from storyforge.core.contracts import StageContext
from storyforge.core.exceptions import StoryForgeError
from storyforge.core.logging import configure_logging, get_logger
from storyforge.core.types import RunManifest, StoryConfig, utc_now
from storyforge.kb.types import KnowledgeStore, SearchIntent, SearchQuery

app = typer.Typer(
    name="storyforge",
    help="Automated story-video production pipeline.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
logger = get_logger(__name__)

# M2-V3: alias review lives in its own sub-app (list/confirm/merge/reject).
aliases_app = typer.Typer(
    help="Alias review — human-in-the-loop over pending KB entities.",
    no_args_is_help=True,
)
app.add_typer(aliases_app, name="aliases")


def _load_settings(config: Path | None) -> Settings:
    return Settings.load(config)


def _load_story_config(path: Path) -> StoryConfig:
    import yaml

    if not path.exists():
        raise StoryForgeError(f"story config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        return StoryConfig.model_validate(data)
    except Exception as exc:
        raise StoryForgeError(f"invalid story config {path}: {exc}") from exc


def _execute_pipeline(
    settings: Settings,
    project: str,
    story_config_path: Path,
    urls: list[str],
    local_files: list[Path],
    force: bool,
    only: list[str] | None,
) -> None:
    """Run stages sequentially with per-stage error handling (T2-DEV1).

    Each stage is wrapped in try/except: a failure marks the stage FAILED in
    the manifest and stops the run (never leaves a stale PENDING). The alert
    checker runs in ``finally`` — on success AND on any failure. A run-scoped
    MetricsRecorder is flushed into the manifest after every stage.
    """
    from storyforge.core.artifacts import ArtifactStore
    from storyforge.core.metrics import (
        MetricsRecorder,
        bind_run_recorder,
        flush_into_manifest,
        reset_run_recorder,
    )
    from storyforge.providers.llm import set_current_stage
    from storyforge.stages.download import DownloadStage
    from storyforge.stages.imaging import ImagingStage
    from storyforge.stages.knowledge import KnowledgeStage
    from storyforge.stages.review import ReviewStage
    from storyforge.stages.story import StoryStage
    from storyforge.stages.transcribe import TranscribeStage
    from storyforge.stages.tts import TTSStage
    from storyforge.stages.video import VideoStage

    story_config = _load_story_config(story_config_path)
    store = ArtifactStore(settings.workspace_dir, project)
    manifest = store.load_manifest()
    ctx = StageContext(settings, store, manifest)
    recorder = MetricsRecorder()
    bind_run_recorder(recorder)

    try:
        # download
        try:
            set_current_stage("download")
            download = DownloadStage(urls=urls, local_files=local_files)
            sources = download.run(ctx, force=force)
            ctx.mark_done("download", sources=len(sources))
        except StoryForgeError as exc:
            _mark_failed(ctx, "download", exc)
            raise typer.Exit(code=1) from exc
        flush_into_manifest(recorder, manifest)
        store.save_manifest(manifest)

        # transcribe
        try:
            set_current_stage("transcribe")
            transcribe = TranscribeStage(sources=sources)
            transcripts = transcribe.run(ctx, force=force)
            ctx.mark_done("transcribe", transcripts=len(transcripts))
        except StoryForgeError as exc:
            _mark_failed(ctx, "transcribe", exc)
            raise typer.Exit(code=1) from exc
        flush_into_manifest(recorder, manifest)
        store.save_manifest(manifest)

        # knowledge
        try:
            set_current_stage("knowledge")
            knowledge = KnowledgeStage(
                transcripts=transcripts,
                universe=story_config.universe,
                grounding=story_config.grounding,
            )
            reports = knowledge.run(ctx, force=force)
            ctx.mark_done("knowledge", reports=len(reports))
        except StoryForgeError as exc:
            _mark_failed(ctx, "knowledge", exc)
            raise typer.Exit(code=1) from exc
        flush_into_manifest(recorder, manifest)
        store.save_manifest(manifest)

        # story
        try:
            set_current_stage("story")
            story_stage = StoryStage(config=story_config)
            story = story_stage.run(ctx, force=force)
            ctx.mark_done("story", scenes=len(story.scenes))
        except StoryForgeError as exc:
            _mark_failed(ctx, "story", exc)
            raise typer.Exit(code=1) from exc
        flush_into_manifest(recorder, manifest)
        store.save_manifest(manifest)

        # review
        try:
            set_current_stage("review")
            review = ReviewStage(story=story).run(ctx, force=force)
            ctx.mark_done("review", conflicts=review.summary.n_conflict)
        except StoryForgeError as exc:
            _mark_failed(ctx, "review", exc)
            raise typer.Exit(code=1) from exc
        flush_into_manifest(recorder, manifest)
        store.save_manifest(manifest)

        # tts
        try:
            set_current_stage("tts")
            tts_stage = TTSStage(story=story)
            clips = tts_stage.run(ctx, force=force)
            ctx.mark_done("tts", clips=len(clips))
        except StoryForgeError as exc:
            _mark_failed(ctx, "tts", exc)
            raise typer.Exit(code=1) from exc
        flush_into_manifest(recorder, manifest)
        store.save_manifest(manifest)

        # imaging
        try:
            imaging = ImagingStage(story=story)
            illustrations = imaging.run(ctx, force=force)
            ctx.mark_done("imaging", images=len(illustrations))
        except StoryForgeError as exc:
            _mark_failed(ctx, "imaging", exc)
            raise typer.Exit(code=1) from exc
        flush_into_manifest(recorder, manifest)
        store.save_manifest(manifest)

        # recap (T1-DEV2): prepend "Previously On" clip for episodes ≥ 2.
        equip_recap = getattr(story_config, "recap", True) is not False
        recap_segment = None
        if equip_recap:
            try:
                from storyforge.ledger.loader import load_universe
                from storyforge.recap import execute_recap

                universe_dir = settings.knowledge.ledgers_dir / story_config.universe
                if universe_dir.exists():
                    ledger = load_universe(universe_dir)
                    image_dir = store.dir("06_images")
                    scene_images = sorted(image_dir.glob("*.png")) if image_dir.exists() else []
                    episode_number = len(ledger.episodes) + 1 if ledger.episodes else 2
                    recap_segment = execute_recap(
                        settings, store, ledger, episode_number, scenes=scene_images
                    )
            except Exception:
                logger.warning("recap generation failed — continuing")

        # video
        try:
            video = VideoStage(
                clips=clips,
                illustrations=illustrations,
                music_mood=story_config.music_mood,
                recap=recap_segment,
            )
            result = video.run(ctx, force=force)
            ctx.mark_done("video", seconds=result.duration_seconds)
        except StoryForgeError as exc:
            _mark_failed(ctx, "video", exc)
            raise typer.Exit(code=1) from exc
        flush_into_manifest(recorder, manifest)
        store.save_manifest(manifest)

        console.print(f"[green]✓[/green] Video: {result.video_path}")
    finally:
        flush_into_manifest(recorder, manifest)
        store.save_manifest(manifest)
        reset_run_recorder()
        # Alerts fire on success AND on failure (T2-DEV1 AC1).
        _check_alert(settings, project, manifest)


def _mark_failed(ctx: StageContext, stage: str, exc: StoryForgeError) -> None:
    """Mark a stage FAILED on the manifest, save, and log."""
    from storyforge.core.types import StageStatus

    ctx.manifest.mark(stage, StageStatus.FAILED, error=str(exc))
    record = ctx.manifest.stages.get(stage)
    if record is not None:
        record.error = str(exc)
    ctx.store.save_manifest(ctx.manifest)
    console.print(f"[red]✗[/red] stage '{stage}' failed: {exc}")


# --- M3-W6: alert on repeated stage failure -----------------------------------


def _check_alert(settings: Settings, project: str, manifest: RunManifest) -> None:
    """Append an alert line when the same stage failed in the previous run too.

    Reads the per-project failure history (``<workspace>/<project>/.failures.json``);
    after 2 consecutive failures of the same stage, a line is appended to
    ``<data>/alerts.md`` (M3-W6 §9.2). The counter resets on success.
    """
    from storyforge.core.types import StageStatus

    failed = {
        stage for stage, record in manifest.stages.items() if record.status is StageStatus.FAILED
    }
    history_path = Path(settings.workspace_dir) / project / ".failures.json"
    history: dict[str, int] = {}
    if history_path.exists():
        import json

        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            history = {}

    for stage in failed:
        history[stage] = history.get(stage, 0) + 1
    # A fully-successful run resets the counters.
    if not failed:
        history = {}

    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        __import__("json").dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    alerts: list[str] = []
    for stage, count in history.items():
        if count >= 2:
            alerts.append(
                f"[{utc_now().isoformat()}] project {project} stage {stage} fail x{count}"
            )
    if alerts:
        alerts_dir = Path(settings.workspace_dir).parent
        alerts_dir.mkdir(parents=True, exist_ok=True)
        with (alerts_dir / "alerts.md").open("a", encoding="utf-8") as fh:
            fh.write("\n".join(alerts) + "\n")
        logger.warning("pipeline alert", project=project, alerts=alerts)


@app.command()
def run(
    project: Annotated[str, typer.Option(help="Project id (workspace subdirectory).")],
    source_config: Annotated[
        Path, typer.Option(help="Path to story_config.yaml.", exists=True, readable=True)
    ],
    url: Annotated[
        list[str] | None, typer.Option(help="YouTube URL to ingest. Repeatable.")
    ] = None,
    local_file: Annotated[
        list[Path] | None,
        typer.Option(help="Local audio file. Repeatable.", exists=True, readable=True),
    ] = None,
    config: Annotated[Path | None, typer.Option(help="settings.yaml override.")] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Regenerate even if artifacts exist.")
    ] = False,
    only: Annotated[list[str] | None, typer.Option(help="Run only these stages.")] = None,
) -> None:
    """Execute the full pipeline for one project."""
    settings = _load_settings(config)
    configure_logging(settings)
    try:
        _execute_pipeline(
            settings,
            project=project,
            story_config_path=source_config,
            urls=url or [],
            local_files=local_file or [],
            force=force,
            only=only,
        )
    except StoryForgeError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(code=1) from exc


@app.command()
def status(
    project: Annotated[str, typer.Option(help="Project id to inspect.")],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Show the run manifest for a project."""
    from storyforge.core.artifacts import ArtifactStore

    settings = _load_settings(config)
    store = ArtifactStore(settings.workspace_dir, project)
    manifest = store.load_manifest()

    table = Table(title=f"Project: {project}")
    table.add_column("stage")
    table.add_column("status")
    table.add_column("metrics")
    for stage in sorted(manifest.stages):
        record = manifest.stages[stage]
        metrics = ", ".join(f"{k}={v}" for k, v in record.metrics.items())
        table.add_row(stage, record.status.value, metrics)
    console.print(table)


@app.command()
def kb_health(
    universe: Annotated[str, typer.Option(help="Universe id to probe.")],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Report knowledge-base health for a universe (Dev 1 DoD)."""
    from storyforge.providers.knowledge import build_universe_store

    settings = _load_settings(config)
    try:
        store = build_universe_store(settings, universe)
        healthy = store.health()
    except StoryForgeError as exc:
        console.print(f"[red]✗ unhealthy: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    if healthy:
        console.print(
            f"[green]✓[/green] universe '{universe}' healthy " f"(store={settings.knowledge.store})"
        )
    else:
        console.print(
            f"[red]✗[/red] universe '{universe}' unhealthy " f"(store={settings.knowledge.store})"
        )
        raise typer.Exit(code=1)


@app.command(name="eval")
def eval_cmd(
    universe: Annotated[str, typer.Option(help="Universe id to evaluate.")],
    golden: Annotated[Path, typer.Option(help="Path to the golden queries YAML.")] = Path(
        "tests/golden/kb_queries.yaml"
    ),
    reranker: Annotated[
        bool, typer.Option("--reranker", help="Run every search with use_reranker=True.")
    ] = False,
    compare_reranker: Annotated[
        bool,
        typer.Option(
            "--compare-reranker",
            help="Run golden set twice (off/on) and write baseline_M2_kb.md.",
        ),
    ] = False,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Run the golden set against a universe and print baseline metrics."""
    from storyforge.providers.knowledge import build_universe_store

    settings = _load_settings(config)
    configure_logging(settings)
    queries = _load_golden(golden)

    if compare_reranker:
        settings.knowledge.use_reranker = False
        off = _run_golden(build_universe_store(settings, universe), queries, False)
        settings.knowledge.use_reranker = True
        on = _run_golden(build_universe_store(settings, universe), queries, True)
        _print_golden_table(f"Golden set eval — reranker OFF — universe: {universe}", off[0])
        _print_golden_table(f"Golden set eval — reranker ON — universe: {universe}", on[0])
        _print_reranker_comparison(off[1], on[1])
        _write_baseline_kb(golden, universe, off[1], on[1])
        return

    settings.knowledge.use_reranker = settings.knowledge.use_reranker or reranker
    store = build_universe_store(settings, universe)
    rows, aggregates = _run_golden(store, queries, reranker)
    _print_golden_table(f"Golden set eval — universe: {universe}", rows)
    _print_golden_summary(aggregates)
    _write_baseline(golden, universe, aggregates)


def _run_golden(
    store: KnowledgeStore, queries: list[dict[str, object]], use_reranker: bool
) -> tuple[list[tuple[str, str, str, str, int]], GoldenAggregates]:
    """Run every golden query; returns (table rows, aggregate metrics)."""
    rows: list[tuple[str, str, str, str, int]] = []
    j1: list[int] = []
    j2_hits = 0
    j2_total = 0
    j3: list[int] = []
    for query in queries:
        metric, value = _eval_query(store, query, use_reranker=use_reranker)
        job = str(query["job"])
        if job == "J1":
            j1.append(value)
        elif job == "J2":
            j2_total += 1
            j2_hits += value
        elif job == "J3":
            j3.append(value)
        rows.append((str(query["id"]), job, str(query["text"]), metric, value))
    return rows, GoldenAggregates(j1=j1, j2_hits=j2_hits, j2_total=j2_total, j3=j3)


def _print_golden_table(title: str, rows: list[tuple[str, str, str, str, int]]) -> None:
    table = Table(title=title)
    table.add_column("id")
    table.add_column("job")
    table.add_column("query")
    table.add_column("metric")
    table.add_column("value")
    for row in rows:
        table.add_row(*[str(cell) for cell in row])
    console.print(table)


def _print_golden_summary(aggregates: GoldenAggregates) -> None:
    summary = Table(title="Baseline (M1)")
    summary.add_column("metric")
    summary.add_column("value")
    summary.add_row("J1 source-coverage@8 (mean)", f"{_mean(aggregates.j1):.2f}")
    summary.add_row("J2 entity hit-rate", f"{aggregates.j2_hits}/{aggregates.j2_total}")
    summary.add_row("J3 passages@4 (mean)", f"{_mean(aggregates.j3):.2f}")
    console.print(summary)


def _print_reranker_comparison(off: GoldenAggregates, on: GoldenAggregates) -> None:
    j1_delta = _mean(on.j1) - _mean(off.j1)
    j3_delta = _mean(on.j3) - _mean(off.j3)
    table = Table(title="Reranker comparison (M2-V1)")
    table.add_column("metric")
    table.add_column("off")
    table.add_column("on")
    table.add_column("delta")
    table.add_row(
        "J1 source-coverage@8 (mean)",
        f"{_mean(off.j1):.2f}",
        f"{_mean(on.j1):.2f}",
        f"{j1_delta:+.2f}",
    )
    table.add_row(
        "J2 entity hit-rate",
        f"{off.j2_hits}/{off.j2_total}",
        f"{on.j2_hits}/{on.j2_total}",
        "—",
    )
    table.add_row(
        "J3 passages@4 (mean)", f"{_mean(off.j3):.2f}", f"{_mean(on.j3):.2f}", f"{j3_delta:+.2f}"
    )
    console.print(table)


def _write_baseline_kb(
    golden: Path, universe: str, off: GoldenAggregates, on: GoldenAggregates
) -> None:
    """M2-Q2 deliverable: baseline_M2_kb.md with reranker on/off side by side."""
    lines = [
        "# Golden set baseline — reranker on/off (M2)",
        "",
        f"- universe: {universe}",
        f"- J1 source-coverage@8 (mean): {_mean(off.j1):.2f} off / {_mean(on.j1):.2f} on",
        f"- J2 entity hit-rate: {off.j2_hits}/{off.j2_total} off / {on.j2_hits}/{on.j2_total} on",
        f"- J3 passages@4 (mean): {_mean(off.j3):.2f} off / {_mean(on.j3):.2f} on",
        "",
    ]
    out = golden.parent / "baseline_M2_kb.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"baseline written to {out}")


@app.command()
def ingest(
    universe: Annotated[str, typer.Option(help="Universe id to ingest into.")],
    drain: Annotated[
        bool, typer.Option("--drain", help="Sweep pending ingest markers and backfill.")
    ] = False,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Backfill pending ingest markers across workspaces (with --drain)."""
    settings = _load_settings(config)
    configure_logging(settings)
    if not drain:
        console.print("[yellow]Pass --drain to sweep pending ingest markers.[/yellow]")
        return
    from storyforge.providers.knowledge import build_universe_store

    store = build_universe_store(settings, universe)
    drained = _drain_pending(settings, store, universe)
    console.print(f"[green]✓[/green] Drained {drained} pending source(s)")


def _load_golden(path: Path) -> list[dict[str, object]]:
    import yaml

    if not path.exists():
        raise StoryForgeError(f"golden file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    queries = data.get("queries")
    if not isinstance(queries, list):
        raise StoryForgeError("golden file must contain a 'queries' list")
    return [item for item in queries if isinstance(item, dict)]


def _eval_query(
    store: KnowledgeStore, query: dict[str, object], *, use_reranker: bool = False
) -> tuple[str, int]:
    job = str(query.get("job", ""))
    text = str(query.get("text", ""))
    if job == "J1":
        hits = store.search(
            SearchQuery(
                text=text,
                intent=SearchIntent.THEME,
                group_by_source=True,
                top_k=8,
                use_reranker=use_reranker,
            )
        )
        return "source-coverage@8", len({h.source_id for h in hits})
    if job == "J2":
        dossier = store.query_entities(text)
        return "entity_hit", 1 if dossier is not None else 0
    hits = store.search(
        SearchQuery(text=text, intent=SearchIntent.SCENE, top_k=4, use_reranker=use_reranker)
    )
    return "passages@4", len(hits)


def _mean(values: list[int]) -> float:
    return sum(values) / len(values) if values else 0.0


@dataclass
class GoldenAggregates:
    """Per-job aggregate metrics from one golden-set run."""

    j1: list[int]  # source-coverage@8 per J1 query
    j2_hits: int
    j2_total: int
    j3: list[int]  # passages@4 per J3 query


def _write_baseline(golden: Path, universe: str, aggregates: GoldenAggregates) -> None:
    lines = [
        "# Golden set baseline (M1)",
        "",
        f"- universe: {universe}",
        f"- J1 source-coverage@8 (mean): {_mean(aggregates.j1):.2f}",
        f"- J2 entity hit-rate: {aggregates.j2_hits}/{aggregates.j2_total}",
        f"- J3 passages@4 (mean): {_mean(aggregates.j3):.2f}",
        "",
    ]
    out = golden.parent / "baseline_M1.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"baseline written to {out}")


def _drain_pending(settings: Settings, store: KnowledgeStore, universe: str) -> int:
    from storyforge.core.artifacts import ArtifactStore
    from storyforge.core.types import Transcript

    root = Path(settings.workspace_dir)
    if not root.exists():
        return 0
    drained = 0
    for project_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        marker = project_dir / "03_knowledge" / "pending_ingest.jsonl"
        if not marker.exists():
            continue
        rows = ArtifactStore.read_jsonl(marker)
        pending: list[dict[str, object]] = []
        for row in rows:
            if str(row.get("universe", "")) != universe:
                pending.append(row)
                continue
            source_id = str(row.get("source_id", ""))
            transcript_path = project_dir / "02_transcripts" / f"{source_id}.json"
            if not transcript_path.exists():
                pending.append(row)
                continue
            transcript = Transcript.model_validate_json(transcript_path.read_text(encoding="utf-8"))
            try:
                store.ingest(transcript)
                drained += 1
            except StoryForgeError:
                pending.append(row)
        if pending:
            ArtifactStore.write_jsonl(marker, pending)
        else:
            marker.unlink()
    return drained


# --- M2-V3: alias review (list pending with context, confirm/merge/reject) ----


def _universe_alias_store(settings: Settings, universe: str) -> AliasStore:
    from storyforge.kb.alias import AliasStore

    return AliasStore(settings.knowledge.kb_data_dir / universe / "aliases.yaml")


def _alias_context_passages(
    settings: Settings, universe: str, name: str, limit: int
) -> list[tuple[str, str]]:
    """Best-effort: top passages mentioning ``name`` for review context."""
    try:
        from storyforge.providers.knowledge import build_universe_store

        store = build_universe_store(settings, universe)
        if not store.health():
            return []
        hits = store.search(SearchQuery(text=name, intent=SearchIntent.ENTITY, top_k=limit))
        return [(f"{h.source_id} {h.start_ts:.0f}s", h.text) for h in hits]
    except StoryForgeError:
        return []


@aliases_app.command("list")
def aliases_list(
    universe: Annotated[str, typer.Option(help="Universe id.")],
    all_status: Annotated[bool, typer.Option("--all", help="Show confirmed too.")] = False,
    context: Annotated[int, typer.Option(help="Context passages per entry (0 = none).")] = 3,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """List entities in the alias table, pending by default."""
    settings = _load_settings(config)
    entries = _universe_alias_store(settings, universe).all_entries()
    if not all_status:
        entries = [e for e in entries if e.status == "pending"]
    if not entries:
        console.print("[green]✓[/green] no pending aliases — table is clean")
        return

    table = Table(title=f"Aliases — universe: {universe}")
    table.add_column("canonical")
    table.add_column("type")
    table.add_column("status")
    table.add_column("added_by")
    table.add_column("aliases")
    for entry in entries:
        table.add_row(
            entry.canonical, entry.type, entry.status, entry.added_by, ", ".join(entry.aliases)
        )
    console.print(table)

    if context > 0:
        for entry in entries:
            passages = _alias_context_passages(settings, universe, entry.canonical, context)
            console.print(f"[bold]{entry.canonical}[/bold]")
            if not passages:
                console.print("  (no passages — kb down or entity not in corpus)")
            for where, text in passages:
                console.print(f"  · [{where}] {text[:140]}")


@aliases_app.command("confirm")
def aliases_confirm(
    universe: Annotated[str, typer.Option(help="Universe id.")],
    name: Annotated[str, typer.Argument(help="Canonical name to confirm.")],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Promote a pending entity to confirmed."""
    settings = _load_settings(config)
    store = _universe_alias_store(settings, universe)
    if not store.confirm(name):
        console.print(f"[red]✗[/red] no pending entry matches '{name}'")
        raise typer.Exit(code=1)
    store.save()
    store.audit(actor="human", action="confirm", name=name)
    console.print(f"[green]✓[/green] confirmed '{name}' (audit appended)")


@aliases_app.command("merge")
def aliases_merge(
    universe: Annotated[str, typer.Option(help="Universe id.")],
    alias: Annotated[str, typer.Argument(help="Alias variant to fold in.")],
    into: Annotated[str, typer.Argument(help="Canonical entry receiving the alias.")],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Fold an alias variant into a canonical entry (fixes false splits)."""
    settings = _load_settings(config)
    store = _universe_alias_store(settings, universe)
    if not store.merge_alias(alias, into):
        console.print(f"[red]✗[/red] cannot merge: '{into}' unknown or '{alias}' already there")
        raise typer.Exit(code=1)
    store.save()
    store.audit(actor="human", action="merge", name=alias, into=into)
    console.print(f"[green]✓[/green] '{alias}' → '{into}' (audit appended)")


@aliases_app.command("reject")
def aliases_reject(
    universe: Annotated[str, typer.Option(help="Universe id.")],
    name: Annotated[str, typer.Argument(help="Entry to remove (false extraction).")],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Remove a pending entry that is not a real entity."""
    settings = _load_settings(config)
    store = _universe_alias_store(settings, universe)
    if not store.remove(name):
        console.print(f"[red]✗[/red] no entry matches '{name}'")
        raise typer.Exit(code=1)
    store.save()
    store.audit(actor="human", action="reject", name=name)
    console.print(f"[green]✓[/green] rejected '{name}' (audit appended)")


@aliases_app.command("rename")
def aliases_rename(
    universe: Annotated[str, typer.Option(help="Universe id.")],
    old: Annotated[str, typer.Argument(help="Current canonical name to rename.")],
    to: Annotated[str, typer.Option(help="New canonical name.")],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Rename a canonical entity (the old name becomes an alias) — T6-DEV1."""
    settings = _load_settings(config)
    store = _universe_alias_store(settings, universe)
    if not store.rename(old, to):
        console.print(f"[red]✗[/red] cannot rename: '{old}' unknown or invalid target")
        raise typer.Exit(code=1)
    store.save()
    store.audit(actor="human", action="rename", name=old, into=to)
    console.print(f"[green]✓[/green] renamed '{old}' → '{to}' (audit appended)")


# --- M2-V4: per-stage cost report ---------------------------------------------


@app.command()
def cost(
    project: Annotated[str, typer.Option(help="Project id (workspace subdirectory).")],
    tier: Annotated[
        str | None,
        typer.Option(help="Filter rows to a tier: standard | premium."),
    ] = None,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Aggregate manifest metrics into a per-stage, per-tier cost report."""
    from storyforge.core.artifacts import ArtifactStore
    from storyforge.core.cost import build_cost_report

    settings = _load_settings(config)
    store = ArtifactStore(settings.workspace_dir, project)
    manifest = store.load_manifest()
    report = build_cost_report(manifest, settings)

    table = Table(title=f"Cost report — project: {project}")
    table.add_column("stage")
    table.add_column("tier")
    table.add_column("cost_usd")
    table.add_column("metrics")
    for row in report.stages:
        if tier and row.tier != tier:
            continue
        metrics = ", ".join(f"{k}={v}" for k, v in row.metrics.items()) or "—"
        table.add_row(row.stage, row.tier, f"{row.cost_usd:.4f}", metrics)
    console.print(table)

    totals = Table(title="Totals by tier")
    totals.add_column("tier")
    totals.add_column("cost_usd")
    for t in ("standard", "premium"):
        if tier and t != tier:
            continue
        totals.add_row(t, f"{report.by_tier.get(t, 0.0):.4f}")
    totals.add_row("[bold]total[/bold]", f"[bold]{report.total_cost_usd:.4f}[/bold]")
    console.print(totals)

    if report.total_cost_usd == 0.0:
        console.print(
            "[yellow]note:[/yellow] no *_usd / token metrics recorded yet — "
            "providers emit cost via ctx.mark_done(..., cost_usd=...) "
            "or the metrics recorder"
        )

    out = store.root / "cost_report.json"
    store.write_model(out, report)
    console.print(f"report written to {out}")


# --- M2-W4: prompt-eval harness -------------------------------------------------


@app.command(name="eval-story")
def eval_story_cmd(
    project: Annotated[str, typer.Option(help="Project id (workspace subdirectory).")],
    judge_model: Annotated[str | None, typer.Option(help="Override judge model.")] = None,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Score every scene with the judge model (rubric 6 chiều) — M2-W4."""
    from storyforge.core.artifacts import ArtifactStore
    from storyforge.eval_story import eval_story

    settings = _load_settings(config)
    configure_logging(settings)
    store = ArtifactStore(settings.workspace_dir, project)
    evals = eval_story(settings, store, judge_model=judge_model)

    table = Table(title=f"Story eval — project: {project}")
    table.add_column("scene")
    table.add_column("total")
    for dimension in ("grounding", "consistency", "pacing", "tts_ready", "visual", "hook"):
        table.add_column(dimension[:4])
    for evaluation in evals:
        by_dim = {s.dimension: f"{s.score:.1f}" for s in evaluation.scores}
        table.add_row(
            evaluation.scene_id,
            f"{evaluation.total:.1f}",
            *[
                by_dim.get(d, "—")
                for d in ("grounding", "consistency", "pacing", "tts_ready", "visual", "hook")
            ],
        )
    console.print(table)
    console.print(f"[green]✓[/green] {len(evals)} scene(s) scored; results in evals/story/")


# --- M2-W6: A/B visual style tooling -------------------------------------------


@app.command(name="ab-style")
def ab_style_cmd(
    project: Annotated[str, typer.Option(help="Project id (workspace subdirectory).")],
    scenes: Annotated[str, typer.Option(help="Comma-separated scene indexes to render.")] = "1,4,9",
    styles: Annotated[
        str, typer.Option(help="Comma-separated art styles.")
    ] = "watercolor,anime,cinematic",
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Render selected scenes across styles into 06_images/ab/ for visual A/B (M2-W6)."""
    from storyforge.core.artifacts import ArtifactStore
    from storyforge.core.types import Story
    from storyforge.providers.imaging import build_image_generator

    settings = _load_settings(config)
    configure_logging(settings)
    store = ArtifactStore(settings.workspace_dir, project)
    story = store.read_model(store.story_path(), Story)

    scene_indexes = [int(s) for s in scenes.split(",") if s.strip().isdigit()]
    style_list = [s.strip() for s in styles.split(",") if s.strip()]

    generator = build_image_generator(settings)
    appearances = {c.name: c.appearance for c in story.config.characters}
    ab_dir = store.dir("06_images") / "ab"

    rendered: list[tuple[str, str, Path]] = []
    for index in scene_indexes:
        if index < 1 or index > len(story.scenes):
            console.print(f"[yellow]skip[/yellow] scene index {index} out of range")
            continue
        scene = story.scenes[index - 1]
        for style in style_list:
            from storyforge.stages.imaging import ImagingStage

            prompt = ImagingStage._compose_prompt(
                scene.beat.image_hint or scene.image_prompt, appearances, style
            )
            out_path = ab_dir / f"{style}_{scene.scene_id}.png"
            generator.generate_from_prompt(prompt, str(out_path))
            rendered.append((scene.scene_id, style, out_path))

    table = Table(title=f"AB images — project: {project}")
    table.add_column("scene")
    table.add_column("style")
    table.add_column("path")
    for scene_id, style, path in rendered:
        table.add_row(scene_id, style, str(path))
    console.print(table)
    console.print(f"[green]✓[/green] {len(rendered)} image(s) in 06_images/ab/")


# --- M4-A1: A/B hook -----------------------------------------------------------


@app.command(name="hook-ab")
def hook_ab_cmd(
    project: Annotated[str, typer.Option(help="Project id (workspace subdirectory).")],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Generate 2 alternative hooks, judge both, write 04_story/hook_ab/ (A1)."""
    from storyforge.core.artifacts import ArtifactStore
    from storyforge.m4tools import hook_ab

    settings = _load_settings(config)
    configure_logging(settings)
    store = ArtifactStore(settings.workspace_dir, project)
    choice_path = hook_ab(settings, store)
    console.print(f"[green]✓[/green] hook A/B written to {choice_path.parent}")
    console.print(f"      choice: {choice_path.read_text(encoding='utf-8')}")


# --- M4-A3: auto thumbnail ------------------------------------------------------


@app.command()
def thumbnail(
    project: Annotated[str, typer.Option(help="Project id (workspace subdirectory).")],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Best-effort thumbnail: title overlay on the first scene (A3)."""
    from storyforge.core.artifacts import ArtifactStore
    from storyforge.m4tools import auto_thumbnail

    settings = _load_settings(config)
    store = ArtifactStore(settings.workspace_dir, project)
    out = auto_thumbnail(settings, store)
    if out is None:
        console.print("[yellow]thumbnail skipped (missing story/image)[/yellow]")
    else:
        console.print(f"[green]✓[/green] thumbnail written to {out}")


# --- M4-B5: universe bootstrap from corpus -------------------------------------


@app.command()
def bootstrap(
    universe: Annotated[str, typer.Option(help="Universe id to bootstrap from KB.")],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Draft a StoryConfig from the ingested KB (producer reviews, never runs)."""
    from storyforge.bootstrap import build_bootstrap_draft, write_draft
    from storyforge.kb.alias import AliasStore
    from storyforge.providers.knowledge import build_universe_store

    settings = _load_settings(config)
    configure_logging(settings)
    store = build_universe_store(settings, universe)
    alias = AliasStore(settings.knowledge.kb_data_dir / universe / "aliases.yaml")
    draft = build_bootstrap_draft(store, alias, universe, settings=settings)
    out = write_draft(Path(settings.knowledge.kb_data_dir) / universe, draft)

    table = Table(title=f"Bootstrap draft — universe: {universe}")
    table.add_column("entity")
    table.add_column("type")
    table.add_column("appearance")
    for character in draft.characters:
        table.add_row(character.name, "person", character.appearance or "(fill in)")
    console.print(table)
    if draft.skipped_entities:
        console.print("[yellow]skipped:[/yellow] " + ", ".join(draft.skipped_entities[:20]))
    console.print(f"[green]✓[/green] draft written to {out} (review before use)")


# --- M3-W4 / T3-DEV2: character reference image ---------------------------------


character_ref_app = typer.Typer(
    help="Manage canonical character reference images for consistent generation.",
    no_args_is_help=True,
)
app.add_typer(character_ref_app, name="character-ref")


@character_ref_app.command("set")
def character_ref_set(
    universe: Annotated[str, typer.Option(help="Universe id.")],
    name: Annotated[str, typer.Argument(help="Character name.")],
    image: Annotated[Path, typer.Option(help="Path to the reference image PNG.", exists=True, readable=True)],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Set a canonical reference image for a character (T3-DEV2)."""
    from storyforge.stages.imaging import _slugify

    settings = _load_settings(config)
    slug = _slugify(name)
    ref_dir = settings.knowledge.kb_data_dir / universe / "characters"
    ref_dir.mkdir(parents=True, exist_ok=True)
    out = ref_dir / f"{slug}.png"
    import shutil

    shutil.copy2(image, out)
    console.print(f"[green]✓[/green] reference image set for '{name}' → {out}")


@character_ref_app.command("generate")
def character_ref_generate(
    universe: Annotated[str, typer.Option(help="Universe id.")],
    name: Annotated[str, typer.Argument(help="Character name.")],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Generate a character-sheet image for a new ref (front, neutral bg)."""
    from storyforge.providers.imaging import build_image_generator
    from storyforge.stages.imaging import _slugify

    settings = _load_settings(config)
    configure_logging(settings)
    generator = build_image_generator(settings)
    # Find the character sheet in the story config to get appearance.
    from storyforge.core.types import Story

    story_path = Path(settings.workspace_dir) / universe / "04_story" / "story.json"
    if story_path.exists():
        story = Story.model_validate_json(story_path.read_text(encoding="utf-8"))
        appearance = ""
        for c in story.config.characters:
            if c.name == name:
                appearance = c.appearance
                break
    else:
        appearance = name
    prompt = f"character sheet, front view, neutral background, {appearance}"
    slug = _slugify(name)
    ref_dir = settings.knowledge.kb_data_dir / universe / "characters"
    ref_dir.mkdir(parents=True, exist_ok=True)
    out_path = str(ref_dir / f"{slug}.png")
    generator.generate_from_prompt(prompt, out_path)
    console.print(f"[green]✓[/green] reference image generated for '{name}' → {out_path}")


# --- M4-A5: music library manager ------------------------------------------------


@app.command()
def music(
    list_moods: Annotated[
        bool, typer.Option("--list", help="List moods from config/music_moods.yaml.")
    ] = False,
    add: Annotated[
        Path | None, typer.Option(help="Music file to add to the library.")
    ] = None,
    mood: Annotated[str | None, typer.Option(help="Mood name (with --add).")] = None,
    license: Annotated[
        str | None, typer.Option(help="CC0 license URL (required with --add).")
    ] = None,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Manage the CC0 music library (T3-DEV1).

    ``--list`` reads ``config/music_moods.yaml``; ``--add <file> --mood <mood>
    --license <url>`` copies the file into ``assets/music_cc0/`` and appends
    the mood (a license URL is mandatory — no file without license).
    """
    from storyforge.music import (
        LICENSES_PATH,
        MOODS_PATH,
        MusicLibraryError,
        add_mood,
        load_moods,
    )

    if add is not None:
        if not mood:
            console.print("[red]✗[/red] --mood is required with --add")
            raise typer.Exit(code=1)
        if not license:
            console.print("[red]✗[/red] --license is required with --add")
            raise typer.Exit(code=1)
        try:
            entry = add_mood(add, mood, license)
        except MusicLibraryError as exc:
            console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(code=1) from exc
        dur = f"{entry.duration_seconds:.1f}s" if entry.duration_seconds else "n/a"
        console.print(
            f"[green]✓[/green] added '{mood}' -> {entry.file} (duration {dur})"
        )
        return

    moods = load_moods()
    if not moods:
        console.print(f"[yellow]no moods in {MOODS_PATH} yet[/yellow]")
        return

    table = Table(title="CC0 music library")
    table.add_column("mood")
    table.add_column("file")
    table.add_column("license")
    table.add_column("duration")
    for m in moods:
        dur = f"{m.duration_seconds:.1f}s" if m.duration_seconds else "n/a"
        table.add_row(m.mood, m.file, m.license or "—", dur)
    console.print(table)

    if LICENSES_PATH.exists():
        console.print(f"license info: {LICENSES_PATH}")
    console.print(
        "[green]use:[/green] set StoryConfig.music_mood=<mood> in your story config"
    )


# --- M4-A4: YouTube upload (draft mode) ---------------------------------------


@app.command()
def publish(
    project: Annotated[str, typer.Option(help="Project id (workspace subdirectory).")],
    draft: Annotated[
        bool,
        typer.Option("--draft", help="Upload as private (default)."),
    ] = True,
    publish_now: Annotated[
        bool,
        typer.Option(
            "--publish",
            help="Upload as public. Requires an explicit flag (AC4).",
        ),
    ] = False,
    setup_oauth: Annotated[
        bool,
        typer.Option("--setup-oauth", help="Print the OAuth consent URL (AC2)."),
    ] = False,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Upload the final video to YouTube (private draft by default) — A4.

    ``--draft`` (default) = ``privacyStatus: private``; ``--publish`` is the
    only way to go public and requires credentials + upload_enabled=true.
    """
    from storyforge.core.artifacts import ArtifactStore
    from storyforge.publish.receipt import (
        PublishReceipt,
        load_receipt,
        save_receipt,
        should_upload,
    )
    from storyforge.publish.youtube import (
        YouTubeUploader,
        YouTubeUploadError,
        build_oauth_url,
    )

    settings = _load_settings(config)
    configure_logging(settings)

    if setup_oauth:
        console.print(
            "Open this URL in a browser, authorize, then run:\n"
            f"  {build_oauth_url(settings.publish.youtube_client_id)}\n"
            "Save the refresh token to SF__PUBLISH__YOUTUBE_REFRESH_TOKEN."
        )
        return

    if publish_now:
        if not settings.publish.upload_enabled:
            console.print(
                "[red]✗[/red] --publish requires "
                "SF__PUBLISH__UPLOAD_ENABLED=true (AC4 explicit gate)."
            )
            raise typer.Exit(code=1)
        privacy: str = "public"
    else:
        privacy = "private"

    store = ArtifactStore(settings.workspace_dir, project)
    video_path = store.video_path()
    if not video_path.exists():
        console.print(f"[red]✗[/red] no final video yet: {video_path}")
        raise typer.Exit(code=1)

    # Idempotency (AC3): skip when the same file hash was already uploaded.
    receipt_path = store.dir("07_video") / "publish.json"
    if not should_upload(receipt_path, video_path, privacy):
        receipt = load_receipt(receipt_path)
        assert receipt is not None
        console.print(f"[green]✓[/green] already uploaded: {receipt.url} ({receipt.privacy})")
        return

    # Build metadata from the stored story config.
    from storyforge.core.types import Story
    from storyforge.publish.metadata import build_metadata

    story = store.read_model(store.story_path(), Story)
    meta = build_metadata(
        story.config, episode=1, total=1, template=settings.publish.metadata_template
    )
    meta.privacy = privacy  # type: ignore[assignment]

    if settings.publish.upload_enabled is False and privacy == "private":
        # Allow dry-run without credentials for private drafts.
        console.print(
            "[yellow]upload disabled (SF__PUBLISH__UPLOAD_ENABLED unset) — "
            "writing receipt only (dry-run).[/yellow]"
        )
        file_hash = PublishReceipt.hash_file(video_path)
        url = "https://youtu.be/dry-run"
        receipt = PublishReceipt(
            project=project, video_id="dry-run", privacy=privacy,
            file_hash=file_hash, url=url,
        )
        save_receipt(receipt_path, receipt)
        console.print(f"[green]✓[/green] dry-run receipt written to {receipt_path}")
        return

    uploader = YouTubeUploader(
        token_path=settings.publish.token_path,
        client_id=settings.publish.youtube_client_id,
        client_secret=settings.publish.youtube_client_secret.get_secret_value(),
        refresh_token=settings.publish.youtube_refresh_token.get_secret_value(),
    )
    thumbnail_candidate = store.dir("06_images") / "thumbnail.png"
    thumbnail_path: Path | None = thumbnail_candidate if thumbnail_candidate.exists() else None
    try:
        video_id = uploader.upload(video_path, thumbnail_path, meta)
    except YouTubeUploadError as exc:
        console.print(f"[red]✗[/red] upload failed: {exc}")
        raise typer.Exit(code=1) from exc

    file_hash = PublishReceipt.hash_file(video_path)
    url = f"https://youtu.be/{video_id}"
    receipt = PublishReceipt(
        project=project, video_id=video_id, privacy=privacy,
        file_hash=file_hash, url=url,
    )
    save_receipt(receipt_path, receipt)
    console.print(f"[green]✓[/green] uploaded ({privacy}): {url}")
    console.print(f"      receipt: {receipt_path}")


# --- M5-V4: webhooks + usage export -------------------------------------------


def _webhook_dispatcher(settings: Settings) -> WebhookDispatcher:
    from storyforge.notify.webhook import WebhookDispatcher, WebhookStore

    return WebhookDispatcher(WebhookStore(Path(settings.workspace_dir).parent / "webhooks"))


webhooks_app = typer.Typer(
    help="Webhook targets — add/list/remove/flush (M5-V4).",
    no_args_is_help=True,
)
app.add_typer(webhooks_app, name="webhooks")


@webhooks_app.command("add")
def webhooks_add(
    universe: Annotated[str, typer.Option(help="Universe id.")],
    url: Annotated[str, typer.Option(help="Webhook endpoint URL.")],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Register a webhook endpoint for a universe."""
    from storyforge.notify.webhook import WebhookTarget

    settings = _load_settings(config)
    dispatcher = _webhook_dispatcher(settings)
    dispatcher.store.add_target(WebhookTarget(universe_id=universe, url=url))
    console.print(f"[green]✓[/green] webhook added for '{universe}': {url}")


@webhooks_app.command("list")
def webhooks_list(
    universe: Annotated[str | None, typer.Option(help="Universe id (optional).")] = None,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """List registered webhook endpoints."""
    settings = _load_settings(config)
    dispatcher = _webhook_dispatcher(settings)
    targets = dispatcher.store.list_targets(universe)
    if not targets:
        console.print("[yellow]no webhooks registered[/yellow]")
        return
    table = Table(title=f"Webhooks — {universe or 'all universes'}")
    table.add_column("universe")
    table.add_column("url")
    for t in targets:
        table.add_row(t.universe_id, t.url)
    console.print(table)


@webhooks_app.command("remove")
def webhooks_remove(
    universe: Annotated[str, typer.Option(help="Universe id.")],
    url: Annotated[str, typer.Option(help="Webhook URL to remove.")],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Remove a webhook endpoint."""
    settings = _load_settings(config)
    dispatcher = _webhook_dispatcher(settings)
    if not dispatcher.store.remove_target(universe, url):
        console.print(f"[red]✗[/red] no matching webhook for '{universe}': {url}")
        raise typer.Exit(code=1)
    console.print(f"[green]✓[/green] removed webhook: {url}")


@webhooks_app.command("flush")
def webhooks_flush(
    universe: Annotated[str | None, typer.Option(help="Universe id (optional).")] = None,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Send all due webhook deliveries."""
    settings = _load_settings(config)
    dispatcher = _webhook_dispatcher(settings)
    delivered = dispatcher.flush()
    console.print(f"[green]✓[/green] flushed {len(delivered)} delivery(ies)")


@app.command()
def usage_export(
    universe: Annotated[str | None, typer.Option(help="Universe id filter.")] = None,
    since: Annotated[str | None, typer.Option(help="ISO date (YYYY-MM-DD).")] = None,
    out: Annotated[Path | None, typer.Option(help="Output path (default stdout).")] = None,
    fmt: Annotated[
        str, typer.Option(help="Output format: csv | jsonl.")
    ] = "csv",
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Export per-stage usage from project manifests (M5-V4)."""
    from datetime import datetime

    from storyforge.notify.usage import (
        collect_usage,
        export_csv,
        export_jsonl,
    )

    settings = _load_settings(config)
    configure_logging(settings)

    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            console.print(f"[red]✗[/red] invalid --since date: {since} (use YYYY-MM-DD)")
            raise typer.Exit(code=1) from None

    rows = collect_usage(Path(settings.workspace_dir), universe=universe, since=since_dt)
    if not rows:
        console.print("[yellow]no usage rows found[/yellow]")
        return

    if out is None:
        # Print to console.
        table = Table(title="Usage export")
        table.add_column("project")
        table.add_column("stage")
        table.add_column("cost_usd")
        table.add_column("api_calls")
        for row in rows:
            table.add_row(
                str(row["project"]),
                str(row["stage"]),
                f"{float(row['cost_usd']):.4f}",
                str(row["api_calls"]),
            )
        console.print(table)
        return

    if fmt == "jsonl":
        export_jsonl(rows, out)
    else:
        export_csv(rows, out)
    console.print(f"[green]✓[/green] {len(rows)} row(s) written to {out}")


# --- M7-V3: analytics ingestion ------------------------------------------------


analytics_app = typer.Typer(
    help="Analytics — pull video stats/retention (M7-V3).",
    no_args_is_help=True,
)
app.add_typer(analytics_app, name="analytics")


@analytics_app.command("pull")
def analytics_pull(
    universe: Annotated[str, typer.Option(help="Universe id.")],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Pull stats + retention for every published video of a universe."""
    from storyforge.analytics.ingest import AnalyticsIngestor
    from storyforge.publish.youtube import YouTubeUploader

    settings = _load_settings(config)
    configure_logging(settings)
    # Reuse the M4 OAuth token cache (access token refreshed on demand).
    uploader = YouTubeUploader(
        token_path=settings.publish.token_path,
        client_id=settings.publish.youtube_client_id,
        client_secret=settings.publish.youtube_client_secret.get_secret_value(),
        refresh_token=settings.publish.youtube_refresh_token.get_secret_value(),
    )
    access_token = uploader._access_token()
    ingestor = AnalyticsIngestor(
        warehouse_dir=settings.analytics.warehouse_dir,
        access_token=access_token,
        cache_ttl_hours=settings.analytics.retention_cache_ttl_hours,
    )
    ingested = ingestor.ingest_all(universe, Path(settings.workspace_dir))
    console.print(
        f"[green]✓[/green] ingested {ingested} video(s) for universe '{universe}'"
    )


@analytics_app.command("scene-retention")
def analytics_scene_retention(
    universe: Annotated[str, typer.Option(help="Universe id.")],
    project: Annotated[str, typer.Option(help="Project id (workspace subdirectory).")],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Map a video's retention curve onto the project's scenes."""
    from storyforge.analytics.ingest import (
        AnalyticsIngestor,
        map_retention_to_scenes,
    )
    from storyforge.core.artifacts import ArtifactStore
    from storyforge.core.types import NarrationClip
    from storyforge.publish.receipt import load_receipt

    settings = _load_settings(config)
    store = ArtifactStore(settings.workspace_dir, project)
    receipt = load_receipt(store.dir("07_video") / "publish.json")
    if receipt is None or receipt.video_id in ("", "dry-run"):
        console.print("[yellow]no published video for this project[/yellow]")
        return

    ingestor = AnalyticsIngestor(warehouse_dir=settings.analytics.warehouse_dir)
    curve = ingestor.load_retention(receipt.video_id)
    if curve is None:
        console.print("[yellow]no retention data pulled yet — run analytics pull[/yellow]")
        return

    clips = [
        NarrationClip.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted(store.dir("05_tts").glob("*.json"))
    ]
    scenes = map_retention_to_scenes(curve, clips)
    table = Table(title=f"Scene retention — {project}")
    table.add_column("scene")
    table.add_column("avg_view_pct")
    for s in scenes:
        table.add_row(s.scene_id, f"{s.avg_view_pct:.2%}")
    console.print(table)


# --- M4-A6: multi-worker queue -------------------------------------------------


def _make_job_runner(
    settings: Settings,
) -> Callable[[JobSpec], tuple[bool, str | None]]:
    """Return a ``run_one`` callback that executes the full pipeline."""

    def run_one(job: JobSpec) -> tuple[bool, str | None]:
        from storyforge.core.exceptions import StoryForgeError

        try:
            _execute_pipeline(
                settings,
                project=job.project,
                story_config_path=Path(job.source_config),
                urls=job.urls,
                local_files=[Path(f) for f in job.local_files],
                force=False,
                only=None,
            )
            return True, None
        except StoryForgeError as exc:
            return False, str(exc)

    return run_one


@app.command()
def worker(
    config: Annotated[Path | None, typer.Option()] = None,
    worker_id: Annotated[str | None, typer.Option(help="Worker id.")] = None,
    loop: Annotated[
        bool, typer.Option("--loop", help="Keep polling instead of one-shot.")
    ] = False,
    interval: Annotated[
        int, typer.Option(help="Poll interval in --loop mode (seconds).")
    ] = 30,
) -> None:
    """Claim and run one queue job (or poll forever with --loop) — M4-A6."""
    from storyforge.queue import run_worker

    settings = _load_settings(config)
    if worker_id:
        settings.queue.worker_id = worker_id
    configure_logging(settings)
    run_one = _make_job_runner(settings)
    processed = run_worker(settings, run_one, loop=loop, interval_seconds=interval)
    console.print(f"[green]✓[/green] worker processed {processed} job(s)")


queue_app = typer.Typer(
    help="Queue management — list, inspect, cancel (M4-A6).",
    no_args_is_help=True,
)
app.add_typer(queue_app, name="queue")


@queue_app.command("status")
def queue_status(
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Show queue bucket counts."""
    from storyforge.queue import QueueManager

    settings = _load_settings(config)
    manager = QueueManager(settings)
    counts = manager.status()

    table = Table(title=f"Queue — {manager.queue_dir}")
    table.add_column("bucket")
    table.add_column("count")
    for bucket in ("queued", "processing", "done", "failed"):
        table.add_row(bucket, str(counts[bucket]))
    console.print(table)

    # Print first 3 queued job names for convenience.
    queued = manager.queued()
    if queued:
        console.print()
        console.print("[bold]queued jobs:[/bold]")
        for path in queued[:3]:
            console.print(f"  {path.stem}")
        if len(queued) > 3:
            console.print(f"  … and {len(queued) - 3} more")


@queue_app.command("cancel")
def queue_cancel(
    job_id: Annotated[str, typer.Argument(help="Job id to cancel.")],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Cancel a queued (not processing) job."""
    from storyforge.queue import QueueManager

    settings = _load_settings(config)
    manager = QueueManager(settings)
    if not manager.cancel(job_id):
        console.print(f"[red]✗[/red] no queued job '{job_id}'")
        raise typer.Exit(code=1)
    console.print(f"[green]✓[/green] cancelled job '{job_id}'")


@app.callback()
def main(
    version: Annotated[bool, typer.Option("--version", help="Show version.")] = False,
) -> None:
    if version:
        console.print(f"storyforge {__version__}")
        raise typer.Exit()


if __name__ == "__main__":
    app()
