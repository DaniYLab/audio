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

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from storyforge import __version__
from storyforge.core.config import Settings
from storyforge.core.contracts import StageContext
from storyforge.core.exceptions import StoryForgeError
from storyforge.core.logging import configure_logging, get_logger
from storyforge.core.types import StoryConfig
from storyforge.kb.types import KnowledgeStore, SearchIntent, SearchQuery

app = typer.Typer(
    name="storyforge",
    help="Automated story-video production pipeline.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
logger = get_logger(__name__)


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
    """Run stages sequentially, reloading intermediates between stages.

    The orchestrator in core.pipeline tracks status; this function handles the
    data handoff by reading the previous stage's artifacts from the store, so
    each stage gets its inputs regardless of when they were produced.
    """
    from storyforge.core.artifacts import ArtifactStore
    from storyforge.stages.download import DownloadStage
    from storyforge.stages.imaging import ImagingStage
    from storyforge.stages.knowledge import KnowledgeStage
    from storyforge.stages.story import StoryStage
    from storyforge.stages.transcribe import TranscribeStage
    from storyforge.stages.tts import TTSStage
    from storyforge.stages.video import VideoStage

    story_config = _load_story_config(story_config_path)
    store = ArtifactStore(settings.workspace_dir, project)
    manifest = store.load_manifest()
    ctx = StageContext(settings, store, manifest)

    download = DownloadStage(urls=urls, local_files=local_files)
    sources = download.run(ctx, force=force)
    ctx.mark_done("download", sources=len(sources))
    store.save_manifest(manifest)

    transcribe = TranscribeStage(sources=sources)
    transcripts = transcribe.run(ctx, force=force)
    ctx.mark_done("transcribe", transcripts=len(transcripts))
    store.save_manifest(manifest)

    knowledge = KnowledgeStage(
        transcripts=transcripts,
        universe=story_config.universe,
        grounding=story_config.grounding,
    )
    reports = knowledge.run(ctx, force=force)
    ctx.mark_done("knowledge", reports=len(reports))
    store.save_manifest(manifest)

    story_stage = StoryStage(config=story_config)
    story = story_stage.run(ctx, force=force)
    ctx.mark_done("story", scenes=len(story.scenes))
    store.save_manifest(manifest)

    tts_stage = TTSStage(story=story)
    clips = tts_stage.run(ctx, force=force)
    ctx.mark_done("tts", clips=len(clips))
    store.save_manifest(manifest)

    imaging = ImagingStage(story=story)
    illustrations = imaging.run(ctx, force=force)
    ctx.mark_done("imaging", images=len(illustrations))
    store.save_manifest(manifest)

    video = VideoStage(clips=clips, illustrations=illustrations)
    result = video.run(ctx, force=force)
    ctx.mark_done("video", seconds=result.duration_seconds)
    store.save_manifest(manifest)

    console.print(f"[green]✓[/green] Video: {result.video_path}")


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
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Run the golden set against a universe and print baseline metrics."""
    from storyforge.providers.knowledge import build_universe_store

    settings = _load_settings(config)
    configure_logging(settings)
    store = build_universe_store(settings, universe)
    queries = _load_golden(golden)

    table = Table(title=f"Golden set eval — universe: {universe}")
    table.add_column("id")
    table.add_column("job")
    table.add_column("query")
    table.add_column("metric")
    table.add_column("value")

    j1: list[int] = []
    j2_hits = 0
    j2_total = 0
    j3: list[int] = []
    for query in queries:
        metric, value = _eval_query(store, query)
        job = str(query["job"])
        if job == "J1":
            j1.append(value)
        elif job == "J2":
            j2_total += 1
            j2_hits += value
        elif job == "J3":
            j3.append(value)
        table.add_row(str(query["id"]), job, str(query["text"]), metric, str(value))

    console.print(table)

    summary = Table(title="Baseline (M1)")
    summary.add_column("metric")
    summary.add_column("value")
    summary.add_row("J1 source-coverage@8 (mean)", f"{_mean(j1):.2f}")
    summary.add_row("J2 entity hit-rate", f"{j2_hits}/{j2_total}")
    summary.add_row("J3 passages@4 (mean)", f"{_mean(j3):.2f}")
    console.print(summary)

    _write_baseline(golden, universe, j1, j2_hits, j2_total, j3)


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


def _eval_query(store: KnowledgeStore, query: dict[str, object]) -> tuple[str, int]:
    job = str(query.get("job", ""))
    text = str(query.get("text", ""))
    if job == "J1":
        hits = store.search(
            SearchQuery(text=text, intent=SearchIntent.THEME, group_by_source=True, top_k=8)
        )
        return "source-coverage@8", len({h.source_id for h in hits})
    if job == "J2":
        dossier = store.query_entities(text)
        return "entity_hit", 1 if dossier is not None else 0
    hits = store.search(SearchQuery(text=text, intent=SearchIntent.SCENE, top_k=4))
    return "passages@4", len(hits)


def _mean(values: list[int]) -> float:
    return sum(values) / len(values) if values else 0.0


def _write_baseline(
    golden: Path,
    universe: str,
    j1: list[int],
    j2_hits: int,
    j2_total: int,
    j3: list[int],
) -> None:
    lines = [
        "# Golden set baseline (M1)",
        "",
        f"- universe: {universe}",
        f"- J1 source-coverage@8 (mean): {_mean(j1):.2f}",
        f"- J2 entity hit-rate: {j2_hits}/{j2_total}",
        f"- J3 passages@4 (mean): {_mean(j3):.2f}",
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


@app.callback()
def main(
    version: Annotated[bool, typer.Option("--version", help="Show version.")] = False,
) -> None:
    if version:
        console.print(f"storyforge {__version__}")
        raise typer.Exit()


if __name__ == "__main__":
    app()
