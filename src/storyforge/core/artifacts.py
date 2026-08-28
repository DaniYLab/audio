"""Artifact store: the only way stages exchange data.

Layout per project inside the workspace::

    data/workspace/<project>/
        manifest.json          run manifest (resume backbone)
        01_download/<id>.m4a  downloaded audio
        02_transcripts/<id>.json
        03_knowledge/chunks.jsonl
        04_story/config.json   resolved StoryConfig
                     story.json
        05_tts/<scene_id>.mp3
        06_images/<scene_id>.png
        07_video/final.mp4
        logs/ffmpeg.log

Rules:
- Stages read upstream artifacts and write their own; they never mutate
  another stage's outputs.
- All JSON is UTF-8, pydantic-serialized domain models (core.types).
- Atomic writes (tmp file + os.replace) so a crash never leaves a torn
  artifact that a later resume would trust.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from storyforge.core.exceptions import WorkspaceError
from storyforge.core.types import RunManifest

ModelT = TypeVar("ModelT", bound=BaseModel)

DIR_DOWNLOAD = "01_download"
DIR_TRANSCRIPTS = "02_transcripts"
DIR_KNOWLEDGE = "03_knowledge"
DIR_STORY = "04_story"
DIR_TTS = "05_tts"
DIR_IMAGES = "06_images"
DIR_VIDEO = "07_video"
DIR_LOGS = "logs"

_MANIFEST_NAME = "manifest.json"


class ArtifactStore:
    """Filesystem-backed artifact store for one project run."""

    def __init__(self, workspace: Path, project: str) -> None:
        self.root = workspace / project
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            for sub in (
                DIR_DOWNLOAD,
                DIR_TRANSCRIPTS,
                DIR_KNOWLEDGE,
                DIR_STORY,
                DIR_TTS,
                DIR_IMAGES,
                DIR_VIDEO,
                DIR_LOGS,
            ):
                (self.root / sub).mkdir(exist_ok=True)
        except OSError as exc:
            raise WorkspaceError(
                f"cannot create workspace at {self.root}", details={"error": str(exc)}
            ) from exc

    # -- paths -----------------------------------------------------------

    def dir(self, stage_dir: str) -> Path:
        path = self.root / stage_dir
        path.mkdir(parents=True, exist_ok=True)
        return path

    def audio_path(self, source_id: str, ext: str) -> Path:
        return self.dir(DIR_DOWNLOAD) / f"{source_id}.{ext}"

    def transcript_path(self, source_id: str) -> Path:
        return self.dir(DIR_TRANSCRIPTS) / f"{source_id}.json"

    def story_path(self) -> Path:
        return self.dir(DIR_STORY) / "story.json"

    def video_path(self) -> Path:
        return self.dir(DIR_VIDEO) / "final.mp4"

    # -- manifest ----------------------------------------------------------

    def load_manifest(self) -> RunManifest:
        path = self.root / _MANIFEST_NAME
        if not path.exists():
            return RunManifest(project=self.root.name)
        return RunManifest.model_validate_json(path.read_text(encoding="utf-8"))

    def save_manifest(self, manifest: RunManifest) -> None:
        self._atomic_write(self.root / _MANIFEST_NAME, manifest.model_dump_json(indent=2))

    # -- generic model I/O ---------------------------------------------------

    def write_model(self, path: Path, model: BaseModel) -> None:
        self._atomic_write(path, model.model_dump_json(indent=2))

    def read_model(self, path: Path, model_cls: type[ModelT]) -> ModelT:
        if not path.exists():
            raise WorkspaceError(f"artifact not found: {path}")
        try:
            return model_cls.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise WorkspaceError(
                f"corrupted artifact: {path}", details={"error": str(exc)}
            ) from exc

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def read_jsonl(path: Path) -> list[dict[str, object]]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
        text = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
        ArtifactStore._atomic_write(path, text + "\n" if text else "")
