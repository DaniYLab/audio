# Architecture

## Pipeline overview

```
┌────────────┐    ┌────────────┐    ┌────────────┐    ┌────────────┐
│  download  │───▶│ transcribe │───▶│ knowledge  │───▶│   story    │
│  (yt-dlp / │    │ (WhisperX/ │    │ (chunk +   │    │ (outline → │
│   file)    │    │  AssemblyAI)│   │  embed +   │    │  scenes)   │
└────────────┘    └────────────┘    │  index)    │    └─────┬──────┘
                                    └────────────┘          │
┌────────────┐    ┌────────────┐    ┌────────────┐          ▼
│   video    │◀───│  imaging   │◀───│    tts     │◀─────────┘ (story)
│ (ffmpeg)   │    │ (fal/oai)  │    │ (edge/11L) │
└────────────┘    └────────────┘    └────────────┘
```

## Core design decisions

### 1. Artifacts-on-disk, not message passing

Stages exchange pydantic models serialized as JSON in a per-project
workspace. Why:

- **Resumability**: any stage can be re-run after a crash by reloading
  upstream artifacts; the run manifest (`manifest.json`) tracks
  pending/running/done/failed/skipped per stage.
- **Inspectability**: every intermediate (transcript, chunks, story, SRT) is
  human-readable — debugging a bad story means reading `04_story/story.json`.
- **Provider isolation**: regenerating images doesn't touch transcripts.

Layout:

```
data/workspace/<project>/
├── manifest.json              # RunManifest — resume backbone
├── 01_download/<id>.m4a
├── 02_transcripts/<id>.json   # Transcript
├── 03_knowledge/chunks.jsonl  # KnowledgeChunk[]
├── 04_story/config.json       # resolved StoryConfig (provenance)
│            story.json        # Story (outline + scenes)
├── 05_tts/<scene_id>.mp3      # one clip per scene
├── 06_images/<scene_id>.png
├── 07_video/final.mp4
└── logs/                      # ffmpeg logs, subtitles.srt, segments
```

### 2. Per-scene TTS drives A/V sync

Narration is synthesized **per scene**, and each clip's duration is probed
with ffprobe. The video stage sets each illustration's display time to its
clip's duration — sync is exact by construction, never estimated.

### 3. Outline-first story generation

Long stories in one LLM call drift. The writer generates a beat outline
first, then expands each beat into (narration, image_prompt) with the
character bible inlined — appearance text is reused verbatim so image prompts
stay consistent across scenes.

### 4. Provider protocols, config-selected

`Transcriber`, `TextToSpeech`, `ImageGenerator`, `KnowledgeStore` are narrow
protocols; `build_*` factories pick implementations from settings. Swapping
Edge-TTS for ElevenLabs is a one-line env change.

### 5. ToS gate on YouTube

The download stage hard-fails unless `SF__DOWNLOAD__ACKNOWLEDGE_TOS_RISK=true`.
The `--local-file` path exists so the pipeline is fully usable without
YouTube.

## Failure & retry model

- Transient provider errors (429/5xx/timeout) → `ExternalServiceError(retryable=True)`
  → exponential backoff w/ jitter (max 4 attempts, 30s cap).
- Deterministic errors (auth, bad request) → fail fast.
- Stage failure → `StageFailedError`, manifest marked `failed`; the run
  aborts. Re-running resumes from the last done stage (done stages are
  skipped unless `--force`).

## Extension points (roadmap)

| Extension | Where it plugs in |
|---|---|
| LightRAG / Qdrant backend | `providers/knowledge.py` factory branch |
| Reviewer pass + fact ledger | new stage between `story` and `tts` |
| Character reference images | `ImagingStage._compose_prompt` + provider `reference_image` kwarg |
| Music bed / SFX | `VideoStage._run_ffmpeg` mix inputs |
| Cost tracking | `StageContext.mark_done(**metrics)` already carries per-stage metrics |
| Queue-based batch runs | `Pipeline.run` is single-project; wrap in a worker loop |
