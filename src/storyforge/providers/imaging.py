"""Image generation providers."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from storyforge.core.config import Settings
from storyforge.core.exceptions import ExternalServiceError, ImageGenerationError
from storyforge.core.retry import retry_external
from storyforge.core.types import Illustration


@runtime_checkable
class PromptImageGenerator(Protocol):
    """Render a composed prompt to an image file.

    ``reference_image`` (M3-W4) optionally anchors a character's appearance so
    generations stay consistent across episodes.
    """

    def generate_from_prompt(
        self, prompt: str, out_path: str, reference_image: Path | None = None
    ) -> Illustration: ...


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

    def generate_from_prompt(
        self, prompt: str, out_path: str, reference_image: Path | None = None
    ) -> Illustration:
        import base64

        import httpx

        settings = self._settings
        # T3-DEV2: a character reference image switches to the reference-capable
        # model (flux-pro/kontext) and sends the image data alongside the prompt.
        model = settings.imaging.fal_ref_model if reference_image is not None else settings.imaging.fal_model
        url = f"https://fal.run/{model}"
        headers = {"Authorization": f"Key {settings.imaging.fal_key.get_secret_value()}"}
        payload: dict[str, object] = {
            "prompt": prompt,
            "image_size": {
                "width": settings.imaging.width,
                "height": settings.imaging.height,
            },
        }
        if reference_image is not None:
            ref_b64 = base64.b64encode(reference_image.read_bytes()).decode("utf-8")
            payload["reference_image"] = {"url": f"data:image/png;base64,{ref_b64}"}
            payload["reference_image_url"] = f"data:image/png;base64,{ref_b64}"

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

    def generate_from_prompt(
        self, prompt: str, out_path: str, reference_image: Path | None = None
    ) -> Illustration:
        import httpx

        url = f"{self._settings.llm.base_url.rstrip('/')}/images/generations"
        headers = {"Authorization": f"Bearer {self._settings.llm.api_key.get_secret_value()}"}
        payload = {
            "model": self._settings.imaging.openai_model,
            "prompt": prompt,
            "size": f"{self._settings.imaging.width}x{self._settings.imaging.height}",
        }
        # M3-W4: reference images are passed through when the endpoint supports
        # them; gpt-image-1 uses the images API which accepts a base64 ref.
        if reference_image is not None:
            import base64

            ref_b64 = base64.b64encode(reference_image.read_bytes()).decode("utf-8")
            payload["reference_image"] = f"data:image/png;base64,{ref_b64}"

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
        b64_raw = first.get("b64_json")
        if not isinstance(b64_raw, str) or not b64_raw:
            raise ImageGenerationError("openai images returned no b64_json")
        b64: str = b64_raw

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
