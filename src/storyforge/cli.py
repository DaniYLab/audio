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
from typing import TYPE_CHECKING, Annotated, Literal, cast

if TYPE_CHECKING:
    from storyforge.kb.alias import AliasStore
    from storyforge.notify.webhook import WebhookDispatcher
    from storyforge.queue import JobSpec

import typer
from rich.console import Console
from rich.table import Table

from storyforge import __version__
from storyforge.core.artifacts import ArtifactStore
from storyforge.core.config import Settings
from storyforge.core.contracts import StageContext
from storyforge.core.exceptions import StoryForgeError
from storyforge.core.logging import configure_logging, get_logger
from storyforge.core.types import (
    Illustration,
    NarrationClip,
    RunManifest,
    SourceRef,
    Story,
    StoryConfig,
    Transcript,
    utc_now,
)
from storyforge.kb.types import KnowledgeStore, SearchIntent, SearchQuery

_PIPELINE_STAGES = (
    "download",
    "transcribe",
    "knowledge",
    "story",
    "review",
    "tts",
    "imaging",
    "video",
)
# Upstream stages whose in-memory outputs a stage consumes. ``--only`` only
# works if these run in the same invocation, so the CLI closes the set over
# this dependency map up front instead of failing mid-run.
_STAGE_DEPS: dict[str, set[str]] = {
    "transcribe": {"download"},
    "knowledge": {"download", "transcribe"},
    "review": {"story"},
    "tts": {"story"},
    "imaging": {"story"},
    "video": {"tts", "imaging"},
}


def _validate_only(only: list[str]) -> None:
    unknown = sorted(set(only) - set(_PIPELINE_STAGES))
    if unknown:
        raise typer.BadParameter(
            f"unknown stage(s): {', '.join(unknown)}. " f"Valid: {', '.join(_PIPELINE_STAGES)}"
        )
    for stage in sorted(only):
        missing = _STAGE_DEPS.get(stage, set()) - set(only)
        if missing:
            raise typer.BadParameter(
                f"--only {stage} also needs: {', '.join(sorted(missing))} "
                "(upstream outputs are passed in-memory)"
            )


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
    license: str = "unknown",
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
    only_set = set(only) if only else None

    def _wants(name: str) -> bool:
        return only_set is None or name in only_set

    sources: list[SourceRef] = []
    transcripts: list[Transcript] = []
    story: Story | None = None
    clips: list[NarrationClip] = []
    illustrations: list[Illustration] = []

    try:
        # download
        if _wants("download"):
            try:
                set_current_stage("download")
                download = DownloadStage(urls=urls, local_files=local_files, license=license)
                sources = download.run(ctx, force=force)
                ctx.mark_done("download", sources=len(sources))
            except StoryForgeError as exc:
                _mark_failed(ctx, "download", exc)
                raise typer.Exit(code=1) from exc
            flush_into_manifest(recorder, manifest)
            store.save_manifest(manifest)
        else:
            ctx.mark_skipped("download")

        # transcribe
        if _wants("transcribe"):
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
        else:
            ctx.mark_skipped("transcribe")

        # knowledge
        if _wants("knowledge"):
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
        else:
            ctx.mark_skipped("knowledge")

        # story
        if _wants("story"):
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
            _accumulate_style_stats(settings, store, story_config.universe)
        else:
            ctx.mark_skipped("story")

        # review
        if _wants("review"):
            assert story is not None  # _validate_only guarantees the story stage ran
            try:
                set_current_stage("review")
                review = ReviewStage(story=story).run(ctx, force=force)
                ctx.mark_done("review", conflicts=review.summary.n_conflict)
            except StoryForgeError as exc:
                _mark_failed(ctx, "review", exc)
                raise typer.Exit(code=1) from exc
            flush_into_manifest(recorder, manifest)
            store.save_manifest(manifest)
        else:
            ctx.mark_skipped("review")

        # tts
        if _wants("tts"):
            assert story is not None
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
        else:
            ctx.mark_skipped("tts")

        # imaging
        if _wants("imaging"):
            assert story is not None
            try:
                imaging = ImagingStage(story=story)
                illustrations = imaging.run(ctx, force=force)
                ctx.mark_done("imaging", images=len(illustrations))
            except StoryForgeError as exc:
                _mark_failed(ctx, "imaging", exc)
                raise typer.Exit(code=1) from exc
            flush_into_manifest(recorder, manifest)
            store.save_manifest(manifest)

            # M4-A3: auto thumbnail (best-effort — publish reuses thumbnail.png).
            try:
                from storyforge.m4tools import auto_thumbnail

                auto_thumbnail(settings, store)
            except Exception:
                logger.warning("auto thumbnail skipped (best-effort)")
        else:
            ctx.mark_skipped("imaging")

        # Video chain (animation + recap + music + assembly) — all gated by
        # --only video; recap/mood are no-ops when the data isn't there.
        if _wants("video"):
            # M6-W1: optional animation stage — runs only when an API provider
            # is configured (fal_kling/veo); kenburns default stays internal.
            animated: dict[str, Path] = {}
            if settings.animation.provider in ("fal_kling", "veo", "svd_local", "wan_local"):
                try:
                    from storyforge.stages.animation import AnimationStage

                    anim_stage = AnimationStage(
                        clips=clips,
                        illustrations={i.scene_id: i for i in illustrations},
                    )
                    anim_result = anim_stage.run(ctx, force=force)
                    animated = {scene_id: clip.clip_path for scene_id, clip in anim_result.items()}
                    api_animated = sum(
                        1 for c in anim_result.values() if c.provider != "kenburns_fallback"
                    )
                    ctx.mark_done("animation", animated=api_animated)
                    flush_into_manifest(recorder, manifest)
                    store.save_manifest(manifest)
                except Exception:
                    logger.warning("animation stage failed — continuing without animated clips")

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
                        # M6-V2: recap sees only canon established up to the
                        # last shipped episode — never facts from the future.
                        as_of = ledger.episodes[-1].episode_id if ledger.episodes else None
                        recap_segment = execute_recap(
                            settings,
                            store,
                            ledger,
                            episode_number,
                            scenes=scene_images,
                            as_of_episode=as_of,
                        )
                except Exception:
                    logger.warning("recap generation failed — continuing")

            # M6-W2: auto-classify music mood when not set explicitly.
            if story_config.music_mood is None and story is not None:
                try:
                    from storyforge.music import load_moods
                    from storyforge.musicmood import classify_mood

                    moods = [m.mood for m in load_moods()]
                    auto_mood = classify_mood(story, ctx.settings, available_moods=moods)
                    if auto_mood:
                        story_config.music_mood = auto_mood
                        logger.info("music mood auto-classified", mood=auto_mood)
                except Exception:
                    logger.warning("music mood auto-classification failed (no music)")

            try:
                video = VideoStage(
                    clips=clips,
                    illustrations=illustrations,
                    music_mood=story_config.music_mood,
                    recap=recap_segment,
                    animated=animated or None,
                )
                result = video.run(ctx, force=force)
                ctx.mark_done("video", seconds=result.duration_seconds)
            except StoryForgeError as exc:
                _mark_failed(ctx, "video", exc)
                raise typer.Exit(code=1) from exc
            flush_into_manifest(recorder, manifest)
            store.save_manifest(manifest)

            console.print(f"[green]✓[/green] Video: {result.video_path}")
        else:
            ctx.mark_skipped("video")
    finally:
        flush_into_manifest(recorder, manifest)
        store.save_manifest(manifest)
        reset_run_recorder()
        # Alerts fire on success AND on failure (T2-DEV1 AC1).
        alerts = _check_alert(settings, project, manifest)
        # M5-V4: auto-enqueue webhook events (run_end/run_fail/alert).
        _enqueue_run_webhooks(
            settings,
            project,
            manifest,
            universe=story_config.universe,
            alerts=alerts,
        )


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


def _check_alert(settings: Settings, project: str, manifest: RunManifest) -> list[str]:
    """Append an alert line when the same stage failed in the previous run too.

    Reads the per-project failure history (``<workspace>/<project>/.failures.json``);
    after 2 consecutive failures of the same stage, a line is appended to
    ``<data>/alerts.md`` (M3-W6 §9.2). The counter resets on success.
    Returns the alert lines (for webhook fan-out).
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
    return alerts


def _enqueue_run_webhooks(
    settings: Settings,
    project: str,
    manifest: RunManifest,
    universe: str,
    alerts: list[str],
) -> None:
    """M5-V4 / M5-W2: enqueue webhook deliveries for run_end/run_fail/alert.

    Best-effort — no targets registered for the universe is a no-op, and a
    dispatcher failure never breaks the pipeline.
    """
    from storyforge.core.types import StageStatus
    from storyforge.notify.webhook import WebhookDispatcher, WebhookStore

    if not universe:
        return
    try:
        dispatcher = WebhookDispatcher(
            WebhookStore(Path(settings.workspace_dir).parent / "webhooks")
        )
        failed = [
            stage
            for stage, record in sorted(manifest.stages.items())
            if record.status is StageStatus.FAILED
        ]
        if failed:
            error = next(
                (str(r.error) for r in manifest.stages.values() if r.error),
                "stage failed",
            )
            dispatcher.enqueue(
                universe,
                "run_fail",
                {"project": project, "stages": failed, "error": error},
            )
        else:
            dispatcher.enqueue(universe, "run_end", {"project": project})
        for alert in alerts:
            dispatcher.enqueue(universe, "alert", {"project": project, "message": alert})
    except Exception as exc:
        logger.warning("webhook enqueue failed (pipeline continues)", error=str(exc))


def _accumulate_style_stats(settings: Settings, store: object, universe: str) -> None:
    """M4-B1: write this episode's style stats into the universe's cross-episode
    accumulation directory (``data/kb/<universe>/style_stats/``).

    Best-effort: missing stats file / universe dir / ledger are harmless no-ops.
    ``store`` is duck-typed to accept ``ArtifactStore`` or a test double.
    """
    from pathlib import Path

    from storyforge.ledger.loader import load_universe
    from storyforge.stylestat import StyleStats, accumulate_style_stats

    root = getattr(store, "root", None)
    if root is None:
        return
    stats_path = Path(root) / "04_story" / "style_stats.json"
    if not stats_path.exists():
        return
    try:
        stats = StyleStats.model_validate_json(stats_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return

    universe_dir = Path(settings.knowledge.kb_data_dir) / universe
    if not universe_dir.exists():
        return

    # Episode number: ledger episode count + 1 (or 1 when no ledger exists).
    try:
        ledger = load_universe(universe_dir)
        number = len(ledger.episodes) + 1
    except Exception:
        number = 1
    episode_id = f"ep_{number:03d}"
    accumulate_style_stats(universe_dir, episode_id, stats)
    logger.info("style stats accumulated", universe=universe, episode=episode_id)


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
    license: Annotated[
        str,
        typer.Option(
            "--license",
            help="Content license of the ingested sources " "(cc0|cc_by|owned|permission|unknown).",
        ),
    ] = "unknown",
) -> None:
    """Execute the full pipeline for one project."""
    if only:
        _validate_only(only)
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
            license=license,
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
    image: Annotated[
        Path, typer.Option(help="Path to the reference image PNG.", exists=True, readable=True)
    ],
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
    add: Annotated[Path | None, typer.Option(help="Music file to add to the library.")] = None,
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
        console.print(f"[green]✓[/green] added '{mood}' -> {entry.file} (duration {dur})")
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
    console.print("[green]use:[/green] set StoryConfig.music_mood=<mood> in your story config")


# --- M3-V4: disk lifecycle ----------------------------------------------------


@app.command()
def clean(
    project: Annotated[str, typer.Option(help="Project id (workspace subdirectory).")],
    keep_final: Annotated[
        bool,
        typer.Option(
            "--keep-final",
            help="Keep final.mp4 and delete 05_tts/06_images (batch-runner mode).",
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preview what would be deleted.")
    ] = False,
    older_than: Annotated[
        str | None, typer.Option(help="Only delete files older than this (7d/48h/30m).")
    ] = None,
    tts_cache: Annotated[
        bool, typer.Option("--tts-cache", help="Clean the TTS audio cache instead.")
    ] = False,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Reclaim disk: delete render intermediates of a project (M3-V4 §5)."""
    from storyforge.cleanup import (
        CleanError,
        apply_report,
        clean_project,
        clean_tts_cache,
        parse_age,
    )

    settings = _load_settings(config)
    age: float | None = None
    if older_than:
        try:
            age = parse_age(older_than)
        except CleanError as exc:
            console.print(f"[red]✗ {exc}[/red]")
            raise typer.Exit(code=1) from exc

    if tts_cache:
        report = clean_tts_cache(settings.tts.cache_dir, dry_run=dry_run)
    else:
        from storyforge.core.artifacts import ArtifactStore

        store = ArtifactStore(settings.workspace_dir, project)
        report = clean_project(store, keep_final=keep_final, dry_run=dry_run, older_than=age)
    apply_report(report)

    table = Table(title=f"Clean {'(dry-run)' if dry_run else ''} — {report.project}")
    table.add_column("files")
    table.add_column("freed bytes")
    table.add_column("skipped")
    table.add_row(str(report.file_count), f"{report.freed_bytes:,}", str(len(report.skipped_files)))
    console.print(table)
    for note in report.skipped_files[:10]:
        console.print(f"  [dim]skip: {note}[/dim]")
    console.print(
        f"[green]✓[/green] {report.file_count} file(s) — {report.freed_bytes:,} bytes freed"
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
    channel: Annotated[
        str | None,
        typer.Option(
            "--channel",
            help="Multi-channel: comma-separated credentials_ref values from "
            "data/channels.yaml (M7-W4).",
        ),
    ] = None,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Upload the final video to YouTube (private draft by default) — A4.

    ``--draft`` (default) = ``privacyStatus: private``; ``--publish`` is the
    only way to go public and requires credentials + upload_enabled=true.
    With ``--channel <ref>[,<ref>]`` the video goes to every registry channel
    whose credentials_ref matches (shorts are vertical-cut first).
    """
    from storyforge.core.artifacts import ArtifactStore
    from storyforge.publish.channels import load_channel_registry
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

    from storyforge.core.types import Story
    from storyforge.publish.metadata import build_metadata

    story = store.read_model(store.story_path(), Story)
    meta = build_metadata(
        story.config, episode=1, total=1, template=settings.publish.metadata_template
    )
    meta.privacy = privacy  # type: ignore[assignment]

    # -- M7-W4: multi-channel dispatch -------------------------------------
    if channel:
        refs = [ref.strip() for ref in channel.split(",") if ref.strip()]
        registry = load_channel_registry()
        specs = [c for c in registry.channels if c.credentials_ref in refs]
        if not specs:
            console.print(
                f"[red]✗[/red] no registry channel matches {', '.join(refs)} — "
                "run `storyforge channels add` first"
            )
            raise typer.Exit(code=1)
        for spec in specs:
            _publish_channel(settings, store, meta, video_path, spec)
        return

    # -- single-channel (legacy) -------------------------------------------
    receipt_path = store.dir("07_video") / "publish.json"
    if not should_upload(receipt_path, video_path, privacy):
        receipt = load_receipt(receipt_path)
        assert receipt is not None
        console.print(f"[green]✓[/green] already uploaded: {receipt.url} ({receipt.privacy})")
        return

    if settings.publish.upload_enabled is False and privacy == "private":
        # Allow dry-run without credentials for private drafts.
        console.print(
            "[yellow]upload disabled (SF__PUBLISH__UPLOAD_ENABLED unset) — "
            "writing receipt only (dry-run).[/yellow]"
        )
        file_hash = PublishReceipt.hash_file(video_path)
        url = "https://youtu.be/dry-run"
        receipt = PublishReceipt(
            project=project,
            video_id="dry-run",
            privacy=privacy,
            file_hash=file_hash,
            url=url,
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
        project=project,
        video_id=video_id,
        privacy=privacy,
        file_hash=file_hash,
        url=url,
    )
    save_receipt(receipt_path, receipt)
    console.print(f"[green]✓[/green] uploaded ({privacy}): {url}")
    console.print(f"      receipt: {receipt_path}")


def _publish_channel(
    settings: Settings,
    store: ArtifactStore,
    meta: object,
    video_path: Path,
    spec: object,
) -> None:
    """Upload the project video to one registry channel (M7-W4).

    - youtube → the final video as-is.
    - shorts → a vertical 9:16 cut of the final video, uploaded to YouTube.
    - tiktok → not supported yet (best-effort per M7 §2.2 — logged, skipped).

    Receipts are written per platform (``07_video/publish_<platform>.json``) so
    re-running only re-uploads the channels that changed.
    """
    from storyforge.publish.channels import ChannelSpec
    from storyforge.publish.cuts import VerticalCutConfig, build_vertical_cut
    from storyforge.publish.receipt import (
        PublishReceipt,
        save_receipt,
        should_upload,
    )
    from storyforge.publish.youtube import YouTubeUploadError

    spec = spec if isinstance(spec, ChannelSpec) else ChannelSpec.model_validate(spec)
    if spec.platform == "tiktok":
        console.print(
            f"[yellow]tiktok channel '{spec.credentials_ref}' skipped — "
            "TikTok Content API not wired yet (M7 stretch).[/yellow]"
        )
        return

    target = video_path
    if spec.platform == "shorts":
        cut_path = video_path.parent / f"final_vertical_{spec.credentials_ref}.mp4"
        if not cut_path.exists():
            build_vertical_cut(
                video_path,
                VerticalCutConfig(hook_full_frame=True),
                cut_path,
                ffmpeg=settings.video.ffmpeg_bin,
            )
        target = cut_path

    receipt_path = store.dir("07_video") / f"publish_{spec.platform}.json"
    privacy = spec.default_privacy or "private"
    if not should_upload(receipt_path, target, privacy):
        console.print(f"[green]✓[/green] {spec.platform} '{spec.credentials_ref}' already uploaded")
        return

    if settings.publish.upload_enabled is False and privacy == "private":
        file_hash = PublishReceipt.hash_file(target)
        save_receipt(
            receipt_path,
            PublishReceipt(
                project=store.root.name,
                video_id="dry-run",
                privacy=privacy,
                file_hash=file_hash,
                url="https://youtu.be/dry-run",
            ),
        )
        console.print(
            f"[yellow]dry-run receipt for {spec.platform} '{spec.credentials_ref}'[/yellow]"
        )
        return

    from storyforge.publish.youtube import YouTubeUploader

    uploader = YouTubeUploader(
        token_path=settings.publish.token_path,
        client_id=settings.publish.youtube_client_id,
        client_secret=settings.publish.youtube_client_secret.get_secret_value(),
        refresh_token=settings.publish.youtube_refresh_token.get_secret_value(),
    )
    thumbnail_candidate = store.dir("06_images") / "thumbnail.png"
    thumbnail_path: Path | None = thumbnail_candidate if thumbnail_candidate.exists() else None
    try:
        video_id = uploader.upload(target, thumbnail_path, meta)  # type: ignore[arg-type]
    except YouTubeUploadError as exc:
        console.print(f"[red]✗[/red] {spec.platform} upload failed: {exc}")
        raise typer.Exit(code=1) from exc

    file_hash = PublishReceipt.hash_file(target)
    save_receipt(
        receipt_path,
        PublishReceipt(
            project=store.root.name,
            video_id=video_id,
            privacy=privacy,
            file_hash=file_hash,
            url=f"https://youtu.be/{video_id}",
        ),
    )
    console.print(f"[green]✓[/green] {spec.platform} '{spec.credentials_ref}' uploaded: {video_id}")


# --- M7-W4: channel registry + vertical cuts ----------------------------------


channels_app = typer.Typer(
    help="Channel registry — multi-platform publish destinations (M7-W4).",
    no_args_is_help=True,
)
app.add_typer(channels_app, name="channels")


@channels_app.command("add")
def channels_add(
    platform: Annotated[
        str,
        typer.Option(help="Platform: youtube | tiktok | shorts."),
    ],
    cred_ref: Annotated[
        str, typer.Option(help="Credentials reference (key into the secret vault).")
    ],
    vertical: Annotated[
        bool,
        typer.Option("--vertical", help="Requires a vertical 9:16 cut."),
    ] = False,
    privacy: Annotated[
        str, typer.Option(help="Default privacy: private | unlisted | public.")
    ] = "private",
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Register a publishing channel (persisted to data/channels.yaml)."""
    from storyforge.publish.channels import (
        ChannelSpec,
        load_channel_registry,
        save_channel_registry,
    )

    platform = platform.lower()
    if platform not in ("youtube", "tiktok", "shorts"):
        console.print("[red]✗[/red] platform must be youtube | tiktok | shorts")
        raise typer.Exit(code=1)
    registry = load_channel_registry()
    added = registry.add(
        ChannelSpec(
            platform=cast(Literal["youtube", "tiktok", "shorts"], platform),
            credentials_ref=cred_ref,
            vertical=vertical,
            default_privacy=privacy,
        )
    )
    if not added:
        console.print(f"[yellow]channel already registered: {platform}/{cred_ref}[/yellow]")
        return
    save_channel_registry(registry)
    console.print(
        f"[green]✓[/green] channel added: {platform} ({cred_ref}, "
        f"vertical={vertical}, privacy={privacy})"
    )


@channels_app.command("list")
def channels_list(config: Annotated[Path | None, typer.Option()] = None) -> None:
    """List registered channels."""
    from storyforge.publish.channels import load_channel_registry

    registry = load_channel_registry()
    if not registry.channels:
        console.print("[yellow]no channels registered (data/channels.yaml)[/yellow]")
        return
    table = Table(title="Channel registry")
    table.add_column("platform")
    table.add_column("credentials_ref")
    table.add_column("vertical")
    table.add_column("privacy")
    for c in registry.channels:
        table.add_row(c.platform, c.credentials_ref, str(c.vertical), c.default_privacy)
    console.print(table)


@channels_app.command("remove")
def channels_remove(
    platform: Annotated[str, typer.Option(help="Platform: youtube | tiktok | shorts.")],
    cred_ref: Annotated[str, typer.Option(help="Credentials reference.")],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Remove a registered channel."""
    from storyforge.publish.channels import load_channel_registry, save_channel_registry

    registry = load_channel_registry()
    if not registry.remove(platform, cred_ref):
        console.print(f"[red]✗[/red] no channel {platform}/{cred_ref}")
        raise typer.Exit(code=1)
    save_channel_registry(registry)
    console.print(f"[green]✓[/green] removed {platform}/{cred_ref}")


@app.command()
def cut(
    project: Annotated[str, typer.Option(help="Project id (workspace subdirectory).")],
    out: Annotated[Path, typer.Option(help="Output path for the 9:16 video.")],
    hook_full_frame: Annotated[
        bool, typer.Option("--hook-full-frame", help="Keep the hook full-width.")
    ] = True,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Crop the final video to vertical 9:16 for Shorts/TikTok (M7-V2)."""
    from storyforge.core.artifacts import ArtifactStore
    from storyforge.publish.cuts import VerticalCutConfig, build_vertical_cut

    settings = _load_settings(config)
    configure_logging(settings)
    store = ArtifactStore(settings.workspace_dir, project)
    video_path = store.video_path()
    if not video_path.exists():
        console.print(f"[red]✗[/red] no final video yet: {video_path}")
        raise typer.Exit(code=1)
    try:
        build_vertical_cut(
            video_path,
            VerticalCutConfig(hook_full_frame=hook_full_frame),
            out,
            ffmpeg=settings.video.ffmpeg_bin,
        )
    except Exception as exc:
        console.print(f"[red]✗[/red] vertical cut failed: {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]✓[/green] vertical cut written to {out}")


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
    fmt: Annotated[str, typer.Option(help="Output format: csv | jsonl.")] = "csv",
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
    console.print(f"[green]✓[/green] ingested {ingested} video(s) for universe '{universe}'")


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


# --- M7-W3: agentic loop (proposals + approve) --------------------------------


@analytics_app.command("proposals")
def analytics_proposals(
    universe: Annotated[str, typer.Option(help="Universe id.")],
    threshold: Annotated[
        float | None, typer.Option(help="Retention threshold (0-1). Default from settings.")
    ] = None,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Generate retention proposals for low-performing scenes (M7-W3)."""
    from storyforge.analytics.agentic import generate_proposals

    settings = _load_settings(config)
    configure_logging(settings)
    proposals = generate_proposals(
        settings,
        settings.analytics.warehouse_dir,
        Path(settings.workspace_dir),
        universe,
        threshold=threshold,
    )
    if not proposals:
        console.print("[yellow]no proposals generated (no low-retention video found)[/yellow]")
        return
    table = Table(title=f"Retention proposals — universe: {universe}")
    table.add_column("project")
    table.add_column("scene")
    table.add_column("dimension")
    table.add_column("change_kind")
    table.add_column("expected_impact")
    for p in proposals:
        table.add_row(p.project, p.scene_id, p.dimension, p.change_kind, p.expected_impact)
    console.print(table)
    console.print(
        f"[green]✓[/green] {len(proposals)} proposal(s) — approve via "
        "`storyforge analytics approve --project X --id <created_at>`"
    )


@analytics_app.command("approve")
def analytics_approve(
    project: Annotated[str, typer.Option(help="Project id.")],
    id: Annotated[str, typer.Option(help="Proposal id (its created_at timestamp).")],
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Approve one proposal → appended to data/analytics/decisions/ (M7-W3)."""
    from storyforge.analytics.agentic import approve_proposal

    settings = _load_settings(config)
    approved = approve_proposal(settings.analytics.warehouse_dir, project, id)
    if approved is None:
        console.print(f"[red]✗[/red] no proposal with id '{id}' for project '{project}'")
        raise typer.Exit(code=1)
    console.print(f"[green]✓[/green] approved: {approved.scene_id} — {approved.change_kind}")


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
                license=job.license,
            )
            return True, None
        except StoryForgeError as exc:
            return False, str(exc)

    return run_one


@app.command()
def worker(
    config: Annotated[Path | None, typer.Option()] = None,
    worker_id: Annotated[str | None, typer.Option(help="Worker id.")] = None,
    loop: Annotated[bool, typer.Option("--loop", help="Keep polling instead of one-shot.")] = False,
    interval: Annotated[int, typer.Option(help="Poll interval in --loop mode (seconds).")] = 30,
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


@queue_app.command("add")
def queue_add(
    project: Annotated[str, typer.Option(help="Project id.")],
    source_config: Annotated[
        Path,
        typer.Option(
            help="Path to story_config.yaml.",
            exists=True,
            readable=True,
        ),
    ] = Path("config/story_config.example.yaml"),
    universe: Annotated[str | None, typer.Option(help="Universe id.")] = None,
    url: Annotated[list[str] | None, typer.Option(help="YouTube URL. Repeatable.")] = None,
    local_file: Annotated[
        list[Path] | None,
        typer.Option(
            help="Local audio file. Repeatable.",
            exists=True,
            readable=True,
        ),
    ] = None,
    scheduled_at: Annotated[
        str | None,
        typer.Option(help="ISO datetime (e.g. 2026-09-03T10:00:00)."),
    ] = None,
    license: Annotated[
        str,
        typer.Option(help="Content license (cc0|cc_by|owned|permission|unknown)."),
    ] = "unknown",
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Enqueue a pipeline job for a worker — M4-A6 (AC3)."""
    from storyforge.queue import QueueManager, new_job

    settings = _load_settings(config)
    manager = QueueManager(settings)
    job = new_job(
        project,
        source_config=str(source_config),
        universe=universe or "",
        urls=url or [],
        local_files=[str(p) for p in (local_file or [])],
        license=license,
    )
    if scheduled_at:
        from datetime import datetime

        try:
            dt = datetime.fromisoformat(scheduled_at)
        except ValueError as exc:
            console.print(f"[red]✗[/red] invalid ISO datetime: {exc}")
            raise typer.Exit(code=1) from exc
        job.scheduled_at = dt.timestamp()
    manager.enqueue(job)
    console.print(f"[green]✓[/green] job '{job.id}' enqueued for project '{project}'")


# --- M5-W1: REST API server ---------------------------------------------------


@app.command()
def api(
    host: Annotated[str, typer.Option(help="Bind host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port.")] = 8000,
    config: Annotated[Path | None, typer.Option(help="settings.yaml override.")] = None,
) -> None:
    """Run the REST API server (uvicorn) — M5-W1."""
    from storyforge.api.app import main as api_main

    settings = _load_settings(config)
    configure_logging(settings)
    console.print(f"[green]✓[/green] StoryForge API on http://{host}:{port} (docs: /docs)")
    api_main(host=host, port=port, config=config)


@app.callback(invoke_without_command=True)
def callback(
    version: Annotated[bool, typer.Option("--version", help="Show version.")] = False,
) -> None:
    if version:
        console.print(f"storyforge {__version__}")
        raise typer.Exit()


def main() -> None:
    """Console-script entrypoint (pyproject [project.scripts] storyforge)."""
    app()


if __name__ == "__main__":
    app()
