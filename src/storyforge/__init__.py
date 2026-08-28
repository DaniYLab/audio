"""StoryForge — automated story-video production pipeline.

Stages: download -> transcribe -> knowledge -> story -> tts -> imaging -> video.
Each stage is an independent module behind a stable contract (see
``storyforge.core.contracts``), communicating only through artifacts on disk
(see ``storyforge.core.artifacts``) so the pipeline is resumable and any
provider can be swapped without touching the rest of the system.
"""

__version__ = "0.1.0"
