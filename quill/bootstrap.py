"""``quill --init`` bootstrap: write the repo's ``quillfolio.toml`` (ticket #33).

A repo with no ``quillfolio.toml`` is refused by :func:`quill.config.load_config` with a pointer
here. :func:`init_config` writes the packaged default so the repo is runnable after two edits
(``runner.kind`` and ``build.command`` / ``build.test``, which are never guessed).

Personas are **not** copied in. They are a machine-level library shared by every repo
(:func:`quill.config.default_personas_root`), and a repo picks which ones it wants by naming them
in its phases — so a per-repo copy would be a fork of the library that silently stops receiving
improvements. :func:`seed_personas` populates that library from the packaged defaults the first
time, and refuses to touch it once it has content.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from quill.config import CONFIG_FILENAME, default_personas_root

#: Where the default config + personas live inside the installed package.
ASSETS_DIR = Path(__file__).parent / "_init_assets"


class InitError(RuntimeError):
    """``--init`` could not write the config (it already exists, or assets are missing)."""


def init_config(directory: str | Path) -> Path:
    """Write ``<directory>/quillfolio.toml`` from the shipped default; return its path.

    Raises:
        InitError: the file already exists (refuse to clobber edits), or the packaged asset is
            missing (a broken install).
    """
    target = Path(directory) / CONFIG_FILENAME
    if target.exists():
        raise InitError(
            f"{CONFIG_FILENAME} already exists in {directory} — refusing to overwrite. "
            "Delete it first if you want a fresh default."
        )
    source = ASSETS_DIR / CONFIG_FILENAME
    if not source.is_file():
        raise InitError(f"packaged init assets are missing at {ASSETS_DIR} — broken install?")

    shutil.copyfile(source, target)
    return target


def seed_personas(root: Path | None = None) -> tuple[Path, int]:
    """Populate an empty persona library from the packaged defaults.

    Returns ``(root, copied)``. A library that already holds any ``.md`` is left completely alone
    and reports ``0`` — overwriting it would clobber edits shared by every repo on the machine,
    which is a far worse failure than doing nothing.
    """
    root = root or default_personas_root()
    source = ASSETS_DIR / "personas"
    if not source.is_dir():
        raise InitError(f"packaged personas are missing at {source} — broken install?")

    root.mkdir(parents=True, exist_ok=True)
    if any(root.glob("*.md")):
        return root, 0

    copied = 0
    for persona in sorted(source.glob("*.md")):
        shutil.copyfile(persona, root / persona.name)
        copied += 1
    return root, copied
