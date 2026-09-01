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

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:
    from storyforge.kb.alias import AliasStore

import typer
from rich.console import Console
from rich.table import Table

from storyforge import __version__
from storyforge.core.config import Settings
from storyforge.core.contracts import StageContext
from storyforge.core.exceptions import StoryForgeError
from storyforge.core.logging import configure_logging, get_logger
from storyforge.core.types import StoryConfig, utc_now
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
    """Run stages sequentially, reloading intermediates between stages.

    The orchestrator in core.pipeline tracks status; this function handles the
    data handoff by reading the previous stage's artifacts from the store, so
    each stage gets its inputs regardless of when they were produced.
    """
    from storyforge.core.artifacts import ArtifactStore
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

    review = ReviewStage(story=story).run(ctx, force=force)
    ctx.mark_done("review", conflicts=review.summary.n_conflict)
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

    _check_alert(settings, project, manifest)
    console.print(f"[green]✓[/green] Video: {result.video_path}")


# --- M3-W6: alert on repeated stage failure -----------------------------------


def _check_alert(settings: Settings, project: str, manifest) -> None:
    """Append an alert line when the same stage failed in the previous run too.

    Reads the per-project failure history from the workspace; after 2
    consecutive failures of the same stage, a line is appended to
    ``data/alerts.md`` (M3-W6 §9.2). The counter resets on success.
    """
    from storyforge.core.types import StageStatus

    failed = {
        stage
        for stage, record in manifest.stages.items()
        if record.status is StageStatus.FAILED
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
            alerts.append(f"[{utc_now().isoformat()}] project {project} stage {stage} fail ×{count}")
    if alerts:
        alerts_dir = Path("data")
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


# --- M2-V4: per-stage cost report ---------------------------------------------


@app.command()
def cost(
    project: Annotated[str, typer.Option(help="Project id (workspace subdirectory).")],
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
        metrics = ", ".join(f"{k}={v}" for k, v in row.metrics.items()) or "—"
        table.add_row(row.stage, row.tier, f"{row.cost_usd:.4f}", metrics)
    console.print(table)

    totals = Table(title="Totals by tier")
    totals.add_column("tier")
    totals.add_column("cost_usd")
    for tier in ("standard", "premium"):
        totals.add_row(tier, f"{report.by_tier.get(tier, 0.0):.4f}")
    totals.add_row("[bold]total[/bold]", f"[bold]{report.total_cost_usd:.4f}[/bold]")
    console.print(totals)

    if report.total_cost_usd == 0.0:
        console.print(
            "[yellow]note:[/yellow] no *_usd metrics recorded yet — "
            "stages emit cost via ctx.mark_done(..., <name>_usd=...)"
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


@app.callback()
def main(
    version: Annotated[bool, typer.Option("--version", help="Show version.")] = False,
) -> None:
    if version:
        console.print(f"storyforge {__version__}")
        raise typer.Exit()


if __name__ == "__main__":
    app()
