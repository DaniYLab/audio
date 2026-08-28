# StoryForge

Automated story-video production pipeline:

```
YouTube audio ─▶ Transcribe ─▶ Knowledge Base ─▶ Story Writer ─▶ TTS ─▶ Images ─▶ Video
```

Each stage is an independent module behind a stable contract, exchanging data
only through JSON artifacts on disk — runs are resumable, providers are
swappable via config, and nothing is ever half-written (atomic writes).

## Repository layout

```
├── src/storyforge/
│   ├── core/           # config, contracts, pipeline, artifacts, types, logging, retry
│   ├── stages/         # 7 pipeline stages (download, transcribe, knowledge, story, tts, imaging, video)
│   ├── providers/      # external services (stt, llm, knowledge store, tts, imaging)
│   └── cli.py          # entrypoint (typer)
├── prompts/            # LLM prompt templates (never inline prompts in code)
├── config/             # example story config + settings overrides
├── tests/              # unit tests (no network, no heavy models)
├── docker/             # Dockerfile + compose (ffmpeg baked in)
├── docs/               # CONVENTIONS.md, ARCHITECTURE.md
├── .env.example        # all environment variables, documented
├── Makefile            # install / lint / typecheck / test / run
└── pyproject.toml      # package + ruff + mypy (strict) + pytest config
```

## Quick start

```bash
# 1. Install (Python >= 3.11, ffmpeg on PATH)
make install-dev

# 2. Configure
cp .env.example .env          # fill in API keys (at minimum SF__LLM__API_KEY)

# 3. Run — ingest a local audio file (no YouTube ToS exposure)
storyforge run \
  --project demo \
  --source-config config/story_config.example.yaml \
  --local-file path/to/podcast.mp3

# 4. Inspect
storyforge status --project demo
```

Workspace artifacts land in `data/workspace/<project>/` — see
`docs/ARCHITECTURE.md` for the full directory contract.

## YouTube sources

Downloading YouTube audio violates YouTube's ToS unless you hold the rights.
The download stage refuses to run unless you set
`SF__DOWNLOAD__ACKNOWLEDGE_TOS_RISK=true`, confirming you have the rights to
the sources. Prefer `--local-file` or content you own/that is CC-licensed.

## Development rules

See **docs/CONVENTIONS.md** — it is binding. Highlights:

- `make lint && make typecheck && make test` must pass before every commit.
- mypy runs in **strict** mode; no `Any` in public signatures.
- Heavy imports (whisperx, chromadb, edge-tts, yt-dlp) are lazy, inside the
  function that needs them.
- Secrets only via `SF__*` env vars; never in code, YAML, logs, or commits.
- Stages are idempotent and communicate only through the ArtifactStore.

## Roadmap

- [x] Stage contracts + resumable manifest
- [x] WhisperX / AssemblyAI transcription providers
- [x] Chroma + pluggable embeddings knowledge store
- [x] Outline-first story writer (OpenAI-compatible LLM)
- [x] Edge-TTS / ElevenLabs narration, per-scene clips
- [x] fal.ai / OpenAI image generation
- [x] FFmpeg assembly (Ken Burns, burned subtitles)
- [ ] LightRAG knowledge graph backend
- [ ] Character reference image / LoRA consistency
- [ ] Reviewer pass (consistency checking) + fact ledger
- [ ] Cost tracking per run
