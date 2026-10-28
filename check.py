"""Report saved articles whose sitemap lastmod is newer than the local copy."""

from __future__ import annotations

import argparse
import json

from paths import posts_dir
from sync import load_sitemaps


def check_updates() -> int:
    entries, _tags = load_sitemaps()
    by_slug = {entry.slug: entry for entry in entries}
    updated: list[tuple[str, str, str]] = []
    for path in sorted(posts_dir().glob("*.json")):
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"warning: {path.name} is not valid JSON")
            continue
        if not isinstance(saved, dict):
            continue
        slug = str(saved.get("slug") or path.stem)
        entry = by_slug.get(slug)
        if entry is None or not entry.lastmod:
            continue
        local = str(saved.get("lastmod") or "")
        if local != entry.lastmod:
            updated.append((entry.lastmod, slug, local))
    updated.sort(reverse=True)
    if not updated:
        print("Saved posts match the sitemap")
        return 0
    noun = "post has" if len(updated) == 1 else "posts have"
    print(f"{len(updated)} saved {noun} changed on the site")
    for remote, slug, local in updated:
        print(slug)
        print(f"  local {local or 'unknown'}")
        print(f"  sitemap {remote}")
        print(f"  python sync.py --slug {slug}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare saved posts with sitemap lastmod and list the ones that changed")
    parser.parse_args(argv)
    return check_updates()


if __name__ == "__main__":
    raise SystemExit(main())
