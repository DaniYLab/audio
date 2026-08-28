"""Image generation providers."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Protocol, runtime_checkable

from storyforge.core.config import Settings
from storyforge.core.exceptions import ExternalServiceError, ImageGenerationError
from storyforge.core.retry import retry_external
from storyforge.core.types import Illustration


@runtime_checkable
class PromptImageGenerator(Protocol):
    """Render a composed prompt to an image file."""

    def generate_from_prompt(self, prompt: str, out_path: str) -> Illustration: ...


def build_image_generator(settings: Settings) -> PromptImageGenerator:
    provider = settings.imaging.provider
    if provider == "fal":
        return FalImageGenerator(settings)
    if provider == "openai":
        return OpenAIImageGenerator(settings)
    raise ImageGenerationError(f"unknown imaging provider: {provider}")


class FalImageGenerator:
    """fal.ai hosted diffusion models (Flux family, Nano Banana, ...)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        if not settings.imaging.fal_key.get_secret_value():
            raise ImageGenerationError("SF__IMAGING__FAL_KEY is required")

    def generate_from_prompt(self, prompt: str, out_path: str) -> Illustration:
        import httpx

        url = f"https://fal.run/{self._settings.imaging.fal_model}"
        headers = {"Authorization": f"Key {self._settings.imaging.fal_key.get_secret_value()}"}
        payload = {
            "prompt": prompt,
            "image_size": {
                "width": self._settings.imaging.width,
                "height": self._settings.imaging.height,
            },
        }

        def _call() -> dict[str, object]:
            response = httpx.post(url, headers=headers, json=payload, timeout=180.0)
            if response.status_code in (429, 500, 502, 503, 504):
                raise ExternalServiceError(f"fal http {response.status_code}", retryable=True)
            if response.status_code != 200:
                raise ExternalServiceError(
                    f"fal http {response.status_code}: {response.text[:300]}",
                    retryable=False,
                )
            return dict(response.json())

        data = retry_external(_call)
        image_url = _extract_url(data)
        if not image_url:
            raise ImageGenerationError(f"fal response missing image url: {str(data)[:300]}")

        _download(image_url, out_path)
        return Illustration(
            scene_id="",  # filled by the imaging stage
            image_path=Path(out_path),
            prompt_hash=str(hash(prompt) & 0xFFFFFFFF),
        )


class OpenAIImageGenerator:
    """gpt-image-1 via the images API (reference-image capable)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        llm_key = settings.llm.api_key.get_secret_value()
        if not llm_key:
            raise ImageGenerationError("SF__LLM__API_KEY is required for openai imaging")

    def generate_from_prompt(self, prompt: str, out_path: str) -> Illustration:
        import httpx

        url = f"{self._settings.llm.base_url.rstrip('/')}/images/generations"
        headers = {"Authorization": f"Bearer {self._settings.llm.api_key.get_secret_value()}"}
        payload = {
            "model": self._settings.imaging.openai_model,
            "prompt": prompt,
            "size": f"{self._settings.imaging.width}x{self._settings.imaging.height}",
        }

        def _call() -> dict[str, object]:
            response = httpx.post(url, headers=headers, json=payload, timeout=180.0)
            if response.status_code in (429, 500, 502, 503, 504):
                raise ExternalServiceError(
                    f"openai images http {response.status_code}", retryable=True
                )
            if response.status_code != 200:
                raise ExternalServiceError(
                    f"openai images http {response.status_code}: {response.text[:300]}",
                    retryable=False,
                )
            return dict(response.json())

        data = retry_external(_call)
        items = data.get("data")
        if not isinstance(items, list) or not items:
            raise ImageGenerationError("openai images returned no data")
        first = items[0]
        if not isinstance(first, dict):
            raise ImageGenerationError("openai images returned malformed data")
        b64 = first.get("b64_json")
        if not isinstance(b64, str) or not b64:
            raise ImageGenerationError("openai images returned no b64_json")

        Path(out_path).write_bytes(base64.b64decode(b64))
        return Illustration(
            scene_id="",
            image_path=Path(out_path),
            prompt_hash=str(hash(prompt) & 0xFFFFFFFF),
        )


def _extract_url(data: dict[str, object]) -> str | None:
    images = data.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            url = first.get("url")
            if isinstance(url, str):
                return url
    image_url = data.get("image_url")
    return image_url if isinstance(image_url, str) else None


def _download(url: str, out_path: str) -> None:
    import httpx

    response = httpx.get(url, timeout=120.0, follow_redirects=True)
    response.raise_for_status()
    Path(out_path).write_bytes(response.content)
