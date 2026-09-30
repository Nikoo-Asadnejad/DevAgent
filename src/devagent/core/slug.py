"""ASCII-only branch naming (non-Latin titles are dropped, never transliterated)."""

import re

_WORD = re.compile(r"[A-Za-z0-9]+")


def make_slug(title: str, max_len: int = 40) -> str:
    words = [w.lower() for w in _WORD.findall(title)]
    slug = ""
    for word in words:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > max_len:
            break
        slug = candidate
    return slug


def branch_name(prefix: str, task_id: str, title: str, run_id: str) -> str:
    if not run_id:
        raise ValueError("run_id is required so every run gets a unique branch")
    safe_id = "-".join(w.lower() for w in _WORD.findall(task_id))
    parts = [safe_id, make_slug(title), run_id]
    return prefix + "-".join(p for p in parts if p)
