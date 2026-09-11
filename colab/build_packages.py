"""Build the two Colab packages: storyforge.zip (source) + luu_lac_bundle.zip (data)."""

import os
import zipfile
from pathlib import Path

ROOT = Path(r"D:\Dat\POC\audio")
OUT = ROOT / "colab"

CODE_EXCLUDE_DIRS = {
    ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".zcode",
    "data", "repo_ainovel", "__pycache__", "colab", "dist", "build",
}
CODE_EXCLUDE_SUFFIXES = {".pyc"}


def add_tree(
    zf: zipfile.ZipFile, base: Path, root_prefix: str = "", exclude: set[str] | None = None
) -> int:
    exclude = CODE_EXCLUDE_DIRS if exclude is None else exclude
    count = 0
    for path in sorted(base.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(ROOT)
        if set(rel.parts) & exclude:
            continue
        if path.suffix in CODE_EXCLUDE_SUFFIXES:
            continue
        zf.write(path, root_prefix + str(rel).replace(os.sep, "/"))
        count += 1
    return count


OUT.mkdir(exist_ok=True)

with zipfile.ZipFile(OUT / "storyforge.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    n = add_tree(zf, ROOT)
print(f"storyforge.zip: {n} files, {(OUT / 'storyforge.zip').stat().st_size / 1e6:.1f} MB")

bundle_paths = [
    ROOT / "data/kb/luu-lac",
    ROOT / "data/workspace/luu_lac_s01e01/02_transcripts",
    ROOT / "data/workspace/luu_lac_s01e01/03_knowledge",
    ROOT / "data/workspace/luu_lac_s01e01/04_story",
    ROOT / "data/workspace/luu_lac_s01e01/06_images",
    ROOT / "data/workspace/luu_lac_s01e01/manifest.json",
]
with zipfile.ZipFile(OUT / "luu_lac_bundle.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    n = 0
    for p in bundle_paths:
        if not p.exists():
            print("  MISSING (skip):", p)
            continue
        if p.is_file():
            zf.write(p, str(p.relative_to(ROOT)).replace(os.sep, "/"))
            n += 1
        else:
            n += add_tree(zf, p, exclude=set())
print(f"luu_lac_bundle.zip: {n} files, {(OUT / 'luu_lac_bundle.zip').stat().st_size / 1e6:.1f} MB")
