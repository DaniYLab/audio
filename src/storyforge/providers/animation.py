"""Animation provider — image-to-video (M6-W1).

``AnimationProvider`` protocol turns a still image into a short motion clip
that the video stage assembles. The built-in ``KenBurnsFallback`` uses the
existing FFmpeg zoompan filter (cost 0, always available); API providers
(FalKling, Veo) plug in behind the same interface.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from storyforge.core.config import Settings
from storyforge.core.exceptions import VideoAssemblyError
from storyforge.core.logging import get_logger
from storyforge.providers.video_encoders import encode_flags, resolve_encoder

logger = get_logger(__name__)


class MotionSpec(BaseModel):
    """Motion description for one animated scene (M6-W1 §2.1)."""

    kind: Literal["kenburns", "slow_push", "pan", "subtle_zoom", "particles"] = "kenburns"
    intensity: float = 0.3  # 0.0 = static, 1.0 = full
    prompt: str | None = None  # scene image_prompt, for text-guided providers (Wan)


class AnimatedClip(BaseModel):
    """Output of one animation call."""

    clip_path: Path
    duration_seconds: float
    provider: str  # "fal_kling" | "veo" | "kenburns_fallback"
    cost_usd: float = 0.0


@runtime_checkable
class AnimationProvider(Protocol):
    """Turn a still image into a motion clip."""

    def animate(
        self,
        image_path: str,
        duration_seconds: float,
        motion: MotionSpec,
        out_path: str,
    ) -> AnimatedClip: ...


class KenBurnsFallback:
    """FFmpeg zoompan — zero cost, always available (M6-W1 fallback)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def animate(
        self,
        image_path: str,
        duration_seconds: float,
        motion: MotionSpec,
        out_path: str,
    ) -> AnimatedClip:
        frames = int(duration_seconds * 30) + 1
        vf = (
            f"zoompan=z='1+{motion.intensity:.1f}*on/{frames}':"
            f"d={frames}:s={self._settings.imaging.width}x{self._settings.imaging.height}:fps=30"
        )
        cmd = [
            self._settings.video.ffmpeg_bin,
            "-y",
            "-loop",
            "1",
            "-i",
            image_path,
            "-vf",
            vf,
            "-t",
            str(duration_seconds),
            *encode_flags(
                resolve_encoder(
                    self._settings.video.encoder,
                    self._settings.video.ffmpeg_bin,
                    self._settings.workspace_dir,
                ),
                self._settings.video.crf,
                self._settings.video.preset,
            ),
            "-pix_fmt",
            "yuv420p",
            str(out_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=600)
        except subprocess.TimeoutExpired as exc:
            raise VideoAssemblyError("animation ffmpeg timed out") from exc
        if result.returncode != 0:
            raise VideoAssemblyError(
                "animation ffmpeg failed",
                details={"stderr": (result.stderr or "")[-2000:]},
            )
        return AnimatedClip(
            clip_path=Path(out_path),
            duration_seconds=duration_seconds,
            provider="kenburns_fallback",
            cost_usd=0.0,
        )


class SVDLocalProvider:
    """Stable Video Diffusion XT, local — image-to-video on the GPU (free).

    SVD conditions on the scene image itself, which is exactly what keeps the
    episode visually consistent: the clip animates from the already-approved
    illustration, no prompt drift. MotionSpec kinds map roughly onto
    ``motion_bucket_id`` (semantic direction is not supported by SVD).

    Output is a short ambient loop (svd_fps * svd_frames); VideoStage loops it
    to the narration length.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pipe: Any = None

    def _load(self) -> Any:
        if self._pipe is not None:
            return self._pipe
        import torch
        from diffusers import StableVideoDiffusionPipeline

        if not torch.cuda.is_available():
            raise VideoAssemblyError("SVD local provider requires a CUDA GPU")
        cfg = self._settings.animation
        logger.info("loading SVD", model=cfg.svd_model)
        pipe = StableVideoDiffusionPipeline.from_pretrained(  # type: ignore[no-untyped-call]
            cfg.svd_model,
            torch_dtype=torch.float16,
            variant="fp16",
        )
        pipe.enable_model_cpu_offload()
        self._pipe = pipe
        return pipe

    def animate(
        self,
        image_path: str,
        duration_seconds: float,
        motion: MotionSpec,
        out_path: str,
    ) -> AnimatedClip:
        import torch
        from diffusers.utils import (  # type: ignore[attr-defined]
            export_to_video,
            load_image,
        )

        cfg = self._settings.animation
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        image = load_image(str(image_path)).convert("RGB").resize((1024, 576))
        bucket = int(cfg.svd_motion_bucket * (0.5 + motion.intensity))
        generator = torch.Generator("cuda").manual_seed(cfg.svd_seed)
        torch.cuda.empty_cache()
        pipe = self._load()
        frames = pipe(
            image,
            height=576,
            width=1024,
            num_frames=cfg.svd_frames,
            num_inference_steps=cfg.svd_steps,
            motion_bucket_id=bucket,
            noise_aug_strength=cfg.svd_noise_aug,
            decode_chunk_size=8,
            generator=generator,
            min_guidance_scale=1.0,
            max_guidance_scale=3.0,
        ).frames[0]
        export_to_video(frames, str(out_path), fps=cfg.svd_fps)
        duration = cfg.svd_frames / cfg.svd_fps
        return AnimatedClip(
            clip_path=Path(out_path),
            duration_seconds=duration,
            provider="svd_local",
            cost_usd=0.0,
        )


class WanLocalProvider:
    """Wan 2.2 TI2V-5B, local — text+image-to-video on the GPU (free).

    A generation ahead of SVD: 704p, 81 frames @ 24 fps (~3.4s), and crucially
    it accepts the scene's text prompt, so the animation follows the beat's
    action instead of generic ambient motion.

    Memory recipe (learned on a 16 GB card, documented because each step is
    load-bearing): sequential CPU offload + gradient checkpointing makes the
    5B DiT + 11 GB UMT5 fit; ``output_type="latent"`` skips the pipeline's
    own decode (the 704p VAE decode OOMs while the DiT is still resident);
    the whole pipeline is then torn down so a fresh VAE can decode the
    latents on an empty GPU.
    """

    _NEGATIVE = (
        "static, blurry, distorted, morphing faces, deformed hands, watermark, "
        "text, logo, low quality, flickering, color shift"
    )

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pipe: Any = None
        self._embeds_cache: dict[str, Any] = {}

    def _dtype(self) -> Any:
        """bf16 on Ampere+ (A100/L4); float16 on Turing (T4) or via config."""
        import torch

        return torch.bfloat16 if self._settings.animation.wan_dtype == "bfloat16" else torch.float16

    def _encode_prompt(self, prompt: str) -> Any:
        """UMT5-xxl on CPU — offload-streaming an 11 GB text encoder through
        the GPU took ~30 min in practice; a straight CPU forward is a few
        minutes and uses no VRAM. The (constant) negative embeds are cached
        so later scenes only pay for the positive prompt."""
        cached = self._embeds_cache.get(prompt)
        if cached is not None:
            return cached
        import torch
        from transformers import T5TokenizerFast, UMT5EncoderModel

        cfg = self._settings.animation
        logger.info("encoding prompt (UMT5 on GPU)", chars=len(prompt))
        tokenizer = T5TokenizerFast.from_pretrained(cfg.wan_model, subfolder="tokenizer")
        inputs = tokenizer(
            [prompt],
            padding="max_length",
            max_length=512,
            truncation=True,
            add_special_tokens=True,
            return_tensors="pt",
        )
        # GPU forward is ~10 s once the 11 GB encoder is resident; a CPU
        # forward under RAM pressure measured ~25 min. The DiT is offloaded
        # to RAM at this point, so the encoder can use the whole GPU.
        encoder = UMT5EncoderModel.from_pretrained(
            cfg.wan_model, subfolder="text_encoder", torch_dtype=self._dtype()
        ).to("cuda")
        with torch.no_grad():
            hidden = encoder(
                input_ids=inputs["input_ids"].to("cuda"),
                attention_mask=inputs["attention_mask"].to("cuda"),
            ).last_hidden_state
        # Replicate the pipeline's own embeds path: hidden states at PADDED
        # positions are masked-attention noise — truncate to the real token
        # length and re-pad with ZEROS, or the DiT cross-attention degrades
        # the whole clip into a fade-to-black (observed on the 5070 Ti).
        seq_len = int(inputs["attention_mask"].gt(0).sum().item())
        del encoder
        torch.cuda.empty_cache()
        kept = hidden[0, :seq_len].cpu()
        del hidden
        result = torch.zeros(1, 512, kept.shape[-1], dtype=kept.dtype)
        result[0, :seq_len] = kept
        result = result.float()
        self._embeds_cache[prompt] = result
        return result

    def _denoise(self, image: Any, prompt: str) -> Any:
        import torch

        cfg = self._settings.animation
        pipe = self._load()
        kwargs: dict[str, Any] = {
            "prompt_embeds": self._encode_prompt(prompt).to("cuda", self._dtype()),
        }
        if cfg.wan_guidance > 1.0:
            kwargs["negative_prompt_embeds"] = self._encode_prompt(self._NEGATIVE).to(
                "cuda", self._dtype()
            )
        out = pipe(
            image=image,
            height=cfg.wan_height,
            width=cfg.wan_width,
            num_frames=cfg.wan_frames,
            num_inference_steps=cfg.wan_steps,
            guidance_scale=cfg.wan_guidance,
            generator=torch.Generator("cuda").manual_seed(cfg.wan_seed),
            output_type="latent",
            **kwargs,
        )
        return out.frames

    def _load(self) -> Any:
        if self._pipe is not None:
            return self._pipe
        import torch
        from diffusers import AutoencoderKLWan, UniPCMultistepScheduler, WanImageToVideoPipeline

        if not torch.cuda.is_available():
            raise VideoAssemblyError("Wan local provider requires a CUDA GPU")
        cfg = self._settings.animation
        logger.info("loading Wan2.2 TI2V", model=cfg.wan_model)
        vae = AutoencoderKLWan.from_pretrained(  # type: ignore[no-untyped-call]
            cfg.wan_model, subfolder="vae", torch_dtype=torch.float32
        )
        pipe = WanImageToVideoPipeline.from_pretrained(  # type: ignore[no-untyped-call]
            cfg.wan_model,
            vae=vae,
            torch_dtype=self._dtype(),
        )
        # Turbo recipe (quanhaol/yetter): flow_shift 5.0 on UniPC keeps quality
        # at 4 steps with CFG off (guidance 1.0); harmless on the base model too.
        pipe.scheduler = UniPCMultistepScheduler.from_config(  # type: ignore[no-untyped-call]
            pipe.scheduler.config, flow_shift=cfg.wan_flow_shift
        )
        pipe.enable_sequential_cpu_offload()
        pipe.transformer.enable_gradient_checkpointing()
        self._pipe = pipe
        return pipe

    def _decode(self, latents: Any) -> Any:
        import torch
        from diffusers import AutoencoderKLWan
        from diffusers.video_processor import VideoProcessor

        cfg = self._settings.animation
        latents = latents.cpu()
        self._pipe = None
        import gc

        gc.collect()
        torch.cuda.empty_cache()
        vae = AutoencoderKLWan.from_pretrained(  # type: ignore[no-untyped-call]
            cfg.wan_model, subfolder="vae", torch_dtype=torch.float32
        )
        vae.to("cuda")
        latents = latents.to("cuda", vae.dtype)
        z = vae.config.z_dim
        mean = torch.tensor(vae.config.latents_mean).view(1, z, 1, 1, 1).to("cuda", latents.dtype)
        std = 1.0 / torch.tensor(vae.config.latents_std).view(1, z, 1, 1, 1).to(
            "cuda", latents.dtype
        )
        latents = latents / std + mean
        with torch.no_grad():
            video = vae.decode(latents, return_dict=False)[0]
        del vae, latents
        gc.collect()
        torch.cuda.empty_cache()
        return VideoProcessor(vae_scale_factor=8).postprocess_video(video, output_type="np")[0]

    def animate(
        self,
        image_path: str,
        duration_seconds: float,
        motion: MotionSpec,
        out_path: str,
    ) -> AnimatedClip:
        import torch
        from diffusers.utils import export_to_video, load_image  # type: ignore[attr-defined]

        cfg = self._settings.animation
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        image = load_image(str(image_path)).convert("RGB").resize((cfg.wan_width, cfg.wan_height))
        prompt = motion.prompt or "cinematic ambient motion, subtle camera drift"
        torch.cuda.empty_cache()
        latents = self._denoise(image, prompt)
        frames = self._decode(latents)
        export_to_video(frames, str(out_path), fps=cfg.wan_fps)
        duration = cfg.wan_frames / cfg.wan_fps
        return AnimatedClip(
            clip_path=Path(out_path),
            duration_seconds=duration,
            provider="wan_local",
            cost_usd=0.0,
        )


def build_animation_provider(settings: Settings) -> AnimationProvider:
    """Factory: returns the configured provider, falling back to KenBurns."""
    engine = settings.animation.provider if hasattr(settings, "animation") else "kenburns"
    if engine == "svd_local":
        return SVDLocalProvider(settings)
    if engine == "wan_local":
        return WanLocalProvider(settings)
    if engine == "kenburns":
        return KenBurnsFallback(settings)
    # FalKling / Veo providers would go here (lazy import, cost model).
    return KenBurnsFallback(settings)
