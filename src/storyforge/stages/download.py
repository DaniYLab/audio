"""Stage 1 — download source audio from YouTube (or accept a local file).

ToS guard: downloading YouTube content without authorization violates
YouTube's Terms of Service. This stage refuses to run unless the operator has
explicitly set ``SF__DOWNLOAD__ACKNOWLEDGE_TOS_RISK=true``. Prefer sources you
own, CC-licensed content, or the ``--local-file`` ingestion path.
"""

from __future__ import annotations

from pathlib import Path

from storyforge.core.contracts import Stage, StageContext
from storyforge.core.exceptions import DownloadError
from storyforge.core.logging import get_logger
from storyforge.core.types import License, SourceRef

logger = get_logger(__name__)

_LICENSES: tuple[License, ...] = ("cc0", "cc_by", "owned", "permission", "unknown")


class DownloadStage(Stage):
    name = "download"

    def __init__(
        self,
        urls: list[str] | None = None,
        local_files: list[Path] | None = None,
        license: str = "unknown",
    ) -> None:
        self.urls = urls or []
        self.local_files = local_files or []
        self.license = license

    def run(self, ctx: StageContext, *, force: bool = False) -> list[SourceRef]:
        refs: list[SourceRef] = []
        if self.urls:
            refs.extend(self._download_remote(ctx))
        refs.extend(self._register_local(ctx))
        if not refs:
            raise DownloadError("no sources: provide --url or --local-file")
        for ref in refs:
            # M3 §8.3: the run-level license is stamped on every source; an
            # explicit per-source value set later wins (never overwrite).
            if ref.license != "unknown" or self.license == "unknown":
                continue
            if self.license not in _LICENSES:
                raise DownloadError(
                    f"invalid license: {self.license!r} "
                    f"(expected one of {', '.join(_LICENSES)})"
                )
            ref.license = self.license
        return refs

    def _download_remote(self, ctx: StageContext) -> list[SourceRef]:
        if not ctx.settings.download.acknowledge_tos_risk:
            raise DownloadError(
                "YouTube downloading is gated: set SF__DOWNLOAD__ACKNOWLEDGE_TOS_RISK=true "
                "to confirm you have the rights to download these sources, or use --local-file."
            )

        # yt-dlp is imported lazily so environments without it can still run
        # later stages (resume) — it is an optional heavy dependency.
        try:
            import yt_dlp  # type: ignore[import-untyped]
        except ImportError as exc:
            raise DownloadError("yt-dlp is not installed (pip install yt-dlp)") from exc

        refs: list[SourceRef] = []
        for url in self.urls:
            out_dir = ctx.store.dir("01_download")
            options = {
                "format": "bestaudio/best",
                "extractaudio": True,
                "audioformat": ctx.settings.download.audio_format,
                "audio_quality": ctx.settings.download.audio_quality,
                "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
                "ratelimit": ctx.settings.download.rate_limit,
                "quiet": True,
                "noprogress": True,
            }
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=True)
            except Exception as exc:  # yt-dlp raises many ad-hoc exception types
                raise DownloadError(
                    f"download failed for {url}", details={"error": str(exc)}
                ) from exc

            source_id = str(info["id"])
            ext = ctx.settings.download.audio_format
            refs.append(
                SourceRef(
                    id=source_id,
                    url=url,
                    local_path=ctx.store.audio_path(source_id, ext),
                    title=info.get("title"),
                    channel=info.get("channel"),
                    duration_seconds=info.get("duration"),
                )
            )
            logger.info("downloaded", source=source_id, title=info.get("title"))
        return refs

    def _register_local(self, ctx: StageContext) -> list[SourceRef]:
        refs: list[SourceRef] = []
        for path in self.local_files:
            if not path.exists():
                raise DownloadError(f"local file not found: {path}")
            target = ctx.store.dir("01_download") / f"{path.stem}{path.suffix}"
            if not target.exists():
                target.write_bytes(path.read_bytes())
            refs.append(SourceRef(id=path.stem, local_path=target, title=path.stem))
        return refs
