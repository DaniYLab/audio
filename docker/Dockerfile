# Stage 1: base image with ffmpeg (required by transcription & video assembly).
FROM python:3.11-slim-bookworm AS base

# ffmpeg + fonts for subtitle burn-in; git for yt-dlp version pinning in CI.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Stage 2: dependency layer (cached separately from source).
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -e ".[transcription,knowledge,tts]"

# Stage 3: source.
COPY src/ src/
COPY prompts/ prompts/
COPY config/ config/

RUN useradd --create-home storyforge && chown -R storyforge:storyforge /app
USER storyforge

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "storyforge.cli"]
