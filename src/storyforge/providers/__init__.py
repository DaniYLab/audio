"""External providers (STT, LLM, knowledge store, TTS, imaging).

Each module exposes a ``build_*`` factory that selects the implementation
from settings. Stage code imports these factories lazily inside ``run`` so
unused heavy dependencies are never loaded.
"""
