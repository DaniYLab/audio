# Engineering Conventions (binding)

These rules keep the codebase uniform as it grows. Deviations require an
explicit note in the PR explaining why.

## 1. Layout & imports

- `src/` layout; the package is `storyforge`. Import as
  `from storyforge.core.types import Story`.
- **Heavy dependencies are always imported lazily** — inside the function or
  method that uses them, never at module top level. This includes:
  `yt_dlp`, `whisperx`, `chromadb`, `sentence_transformers`, `edge_tts`,
  `assemblyai`, `httpx` (acceptable at top level), `anyio`.
  Rationale: `storyforge status` must start in milliseconds, and a missing
  optional extra must only fail the stage that needs it.
- No circular imports: `stages/*` may import from `core/*` and
  `providers/*` (lazily); `providers/*` may import from `core/*` only.

## 2. Typing

- mypy **strict** — every function fully annotated; no `Any` in public
  signatures. Internal `Any` requires a `# type: ignore[...]` with code.
- Modern syntax: `list[str]`, `X | None`, `match` where it reads better.
- Domain objects are pydantic models (`core/types.py`) — pure data, no I/O,
  no provider fields. If a provider returns something exotic, convert at the
  provider boundary.

## 3. Configuration

- All runtime configuration flows through `core/config.py::Settings`
  (pydantic-settings). Env prefix `SF_`, nesting via `__`:
  `SF__TTS__EDGE_VOICE`.
- Precedence: env > `.env` > `config/settings.yaml` > code defaults.
- **Secrets**: only ever in env/`.env` (gitignored). Typed as `SecretStr`.
  Never log a secret, never put one in an exception message, never serialize
  settings wholesale into artifacts or logs.
- Every new setting must be added to `.env.example` with a comment.

## 4. Errors

- All exceptions derive from `StoryForgeError`. Catch the family, not
  individual classes, at boundaries.
- Raise the specific subclass (`DownloadError`, `TTSError`, ...) with a short
  message and machine-readable `details={...}`.
- `ExternalServiceError(retryable=True)` marks transient HTTP failures
  (429/5xx, timeouts); anything else fails fast. Retry policy lives only in
  `core/retry.py` — never scatter `time.sleep` loops.
- Stages never return `None` to signal errors and never swallow exceptions
  silently. Best-effort paths (e.g. diarization) may catch + log + continue,
  and must say so in a comment.

## 5. Stages & artifacts

- One stage = one module in `stages/`, one class inheriting `Stage`, with
  `name` set to its manifest key.
- Stages are **idempotent**: if the output artifact exists and `force=False`,
  reload and return it. Crash-safety comes from atomic writes in
  `ArtifactStore` — never `path.write_text` an artifact directly.
- Stages read only upstream artifacts and write only their own directory.
  Never mutate another stage's output.
- File naming inside stage directories is owned by `ArtifactStore` helpers;
  stages ask the store for paths.

## 6. External providers

- One provider = one module in `providers/`, exposed via a `build_*(settings)`
  factory. Adding a provider means adding a class + one branch in the
  factory — nothing else changes.
- Provider selection is config-driven (`SF__*_ENGINE/PROVIDER`); never
  hardcode a provider in stage code.
- HTTP calls go through `httpx` with explicit timeouts and
  `core.retry.retry_external` for transient failures.

## 7. Prompts

- Prompt templates live in `prompts/*.txt` with `{placeholder}` substitution
  via `providers/llm.fill_prompt`. **Never inline prompt text in code.**
- Prompts must pin the output format so parsing is deterministic
  (see `_parse_beats` / `_parse_scene_response` for the contract).

## 8. Logging

- `get_logger(__name__)` everywhere; only `cli.py` calls
  `configure_logging`. Structured key=value pairs; log stage progress as
  `logger.info("stage done", stage=..., <metric>=...)`.
- No `print()` outside the CLI presentation layer (typer/rich).

## 9. Tests

- `tests/` mirror nothing special — name them `test_<area>.py`.
- Unit tests never touch the network, GPUs, or paid APIs. Provider code is
  tested by parsing/fixture tests; integration tests are marked
  `@pytest.mark.integration` and skipped by default in CI.
- New pure logic (chunking, parsing, SRT generation) ships with tests in the
  same PR.

## 10. Subprocess / FFmpeg

- Always `subprocess.run(..., check=False, capture_output=True)` then inspect
  the return code; write stderr to the stage's log dir for post-mortem.
- Timeouts are mandatory for subprocess calls.

## 11. Git hygiene

- Conventional commits: `feat(story): ...`, `fix(tts): ...`,
  `chore(deps): ...`.
- `make lint && make typecheck && test` green before every commit.
- Never commit: `.env`, `data/`, model weights, generated media.
