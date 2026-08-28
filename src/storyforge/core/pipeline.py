"""Pipeline orchestrator.

Runs the seven stages in order for one project. Responsibilities:
- persist the manifest before/after each stage (crash-safe resume);
- skip stages whose outputs already exist unless ``force=True``;
- wrap failures in ``StageFailedError`` with the stage name attached;
- never retry a whole stage here — transient failures are retried inside
  provider calls (core.retry); stage-level retries are a CLI decision.
"""

from __future__ import annotations

from pathlib import Path

from storyforge.core.artifacts import ArtifactStore
from storyforge.core.config import Settings
from storyforge.core.contracts import Stage, StageContext
from storyforge.core.exceptions import StageFailedError, StoryForgeError
from storyforge.core.logging import get_logger
from storyforge.core.types import RunManifest, StageStatus

logger = get_logger(__name__)


class Pipeline:
    def __init__(self, settings: Settings, stages: list[Stage]) -> None:
        self.settings = settings
        self.stages = stages

    def run(
        self,
        project: str,
        *,
        force: bool = False,
        only: list[str] | None = None,
    ) -> RunManifest:
        store = ArtifactStore(self.settings.workspace_dir, project)
        manifest = store.load_manifest()
        ctx = StageContext(self.settings, store, manifest)

        selected = self.stages if not only else [s for s in self.stages if s.name in only]
        unknown = set(only or []) - {s.name for s in self.stages}
        if unknown:
            raise StoryForgeError(f"unknown stage(s): {sorted(unknown)}")

        for stage in selected:
            if manifest.stages.get(stage.name, None) is not None and not force:
                record = manifest.stages[stage.name]
                if record.status is StageStatus.DONE:
                    ctx.mark_skipped(stage.name)
                    store.save_manifest(manifest)
                    logger.info("stage skipped (already done)", stage=stage.name)
                    continue

            ctx.mark_running(stage.name)
            store.save_manifest(manifest)
            logger.info("stage start", stage=stage.name)

            try:
                output = stage.run(ctx, force=force)
            except StoryForgeError as exc:
                manifest.mark(stage.name, StageStatus.FAILED, error=str(exc))
                store.save_manifest(manifest)
                logger.error("stage failed", stage=stage.name, error=str(exc))
                raise StageFailedError(stage.name, exc) from exc

            ctx.mark_done(stage.name)
            store.save_manifest(manifest)
            logger.info("stage done", stage=stage.name, output=type(output).__name__)

        return manifest


def workspace_root(settings: Settings) -> Path:
    return Path(settings.workspace_dir)
