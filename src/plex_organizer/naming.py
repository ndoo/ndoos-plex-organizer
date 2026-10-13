from __future__ import annotations

import re

_ILLEGAL = re.compile(r'[\\/*?"<>|\x00-\x1f]')
MAX_NAME_BYTES = 255


def sanitize(name: str) -> str:
    name = name.replace(":", " -")
    name = _ILLEGAL.sub("", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name.strip()


def _truncate(stem: str, suffix: str, limit: int = MAX_NAME_BYTES) -> str:
    budget = limit - len(suffix.encode())
    raw = stem.encode()
    if len(raw) > budget:
        stem = raw[:budget].decode(errors="ignore").rstrip()
    return stem + suffix


def with_year(title: str, year: int | None) -> str:
    title = re.sub(r"\s*\(\d{4}\s*[-\u2013]\s*(?:\d{2,4})?\)$", "", sanitize(title))
    if re.search(r"\(\d{4}\)$", title):
        return title
    return f"{title} ({year})" if year else title


def media_label(resolution: str | None, hdr: str | None) -> str:
    res = (resolution or "").lower()
    if res == "4k":
        label = "2160p"
    elif res.isdigit():
        label = f"{res}p"
    elif res:
        label = res.upper() if res == "sd" else res
    else:
        label = ""
    if hdr:
        label = f"{label} {hdr}".strip()
    return label


def movie_names(
    title: str,
    year: int | None,
    ext: str,
    *,
    edition: str | None = None,
    label: str | None = None,
    part: int | None = None,
    id_tag: str | None = None,
) -> tuple[str, str]:
    """Return (folder, filename) per Plex movie naming."""
    base = with_year(title, year)
    folder = base + (f" {{{id_tag}}}" if id_tag else "")
    stem = base
    if edition:
        stem += f" {{edition-{sanitize(edition)}}}"
    if label:
        stem += f" - {sanitize(label)}"
    if part:
        stem += f" - part{part}"
    return _truncate(folder, ""), _truncate(stem, ext)


def episode_names(
    show: str,
    show_year: int | None,
    season: int,
    episodes: list[tuple[int, str]],
    ext: str,
    *,
    label: str | None = None,
    part: int | None = None,
    id_tag: str | None = None,
) -> tuple[str, str, str]:
    """Return (show folder, season folder, filename) per Plex TV naming."""
    base = with_year(show, show_year)
    nums = sorted(n for n, _ in episodes)
    code = f"s{season:02d}e{nums[0]:02d}"
    if len(nums) > 1:
        code += f"-e{nums[-1]:02d}"
    titles = [sanitize(t) for _, t in sorted(episodes) if t]
    stem = f"{base} - {code}"
    if titles:
        stem += " - " + " & ".join(dict.fromkeys(titles))
    if label:
        stem += f" - {sanitize(label)}"
    if part:
        stem += f" - part{part}"
    show_folder = base + (f" {{{id_tag}}}" if id_tag else "")
    return _truncate(show_folder, ""), f"Season {season:02d}", _truncate(stem, ext)
