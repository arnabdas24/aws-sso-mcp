"""Check that every relative link and anchor in the Markdown docs resolves.

Documentation rots quietly: a renamed file or a reworded heading leaves a link
that still looks fine in the diff and only breaks for the reader. This walks
every tracked Markdown file, resolves relative targets against the filesystem
and matches ``#fragments`` against the headings GitHub would generate.

External (``http``/``https``/``mailto``) links are deliberately **not** checked:
that needs the network, is slow and fails for reasons outside this repository.

Usage::

    python scripts/check_doc_links.py [root]
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

# Fenced blocks and inline spans are stripped before links are extracted, so a
# documented example such as `[label](path)` is never reported as broken.
_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_INLINE_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(\s*<?([^)>\s]+)>?\s*\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$", re.MULTILINE)
# GitHub keeps word characters and hyphens; everything else is dropped.
_NOT_SLUG = re.compile(r"[^\w\s-]", re.UNICODE)

_SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".mutmut-cache"})
_EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "tel:")


def _strip_code(text: str) -> str:
    return _INLINE_CODE.sub("", _FENCE.sub("", text))


def _slug(heading: str) -> str:
    """Convert heading text to a GitHub anchor slug.

    Markdown emphasis, inline code and links contribute their text only, so
    ``## `token_status` (**fast**)`` becomes ``token_status-fast``.
    """
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = text.replace("`", "").replace("*", "").replace("~", "")
    return _NOT_SLUG.sub("", text).strip().lower().replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    """Return every anchor GitHub would generate for *path*, duplicates included."""
    seen: Counter[str] = Counter()
    anchors: set[str] = set()
    for _, heading in _HEADING.findall(_FENCE.sub("", _read(path))):
        slug = _slug(heading)
        if not slug:
            continue
        # GitHub disambiguates repeated headings as slug, slug-1, slug-2, ...
        anchors.add(slug if not seen[slug] else f"{slug}-{seen[slug]}")
        seen[slug] += 1
    return anchors


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _markdown_files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*.md") if not _SKIP_DIRS.intersection(p.relative_to(root).parts)
    )


def _check_file(md: Path, root: Path, anchor_cache: dict[Path, set[str]]) -> tuple[list[str], int]:
    problems: list[str] = []
    checked = 0
    for target in _INLINE_LINK.findall(_strip_code(_read(md))):
        if target.startswith(_EXTERNAL_SCHEMES):
            continue
        checked += 1
        file_part, _, fragment = target.partition("#")
        resolved = (md.parent / file_part).resolve() if file_part else md.resolve()
        where = md.relative_to(root).as_posix()
        if not resolved.exists():
            problems.append(f"  {where} -> {target} (no such file)")
            continue
        if not fragment or resolved.suffix != ".md":
            continue
        if resolved not in anchor_cache:
            anchor_cache[resolved] = _anchors(resolved)
        if fragment not in anchor_cache[resolved]:
            problems.append(f"  {where} -> {target} (no such heading)")
    return problems, checked


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path.cwd()
    anchor_cache: dict[Path, set[str]] = {}
    problems: list[str] = []
    checked = 0

    files = _markdown_files(root)
    for md in files:
        found, count = _check_file(md, root, anchor_cache)
        problems.extend(found)
        checked += count

    if problems:
        print(f"FAIL: {len(problems)} broken link(s) in {len(files)} file(s):", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        return 1

    print(f"OK: {checked} relative link(s) across {len(files)} Markdown file(s) resolve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
