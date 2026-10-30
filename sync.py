"""Download each English article, parse it, and cache its images."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass

from lxml import etree
from tqdm import tqdm

import article
from article import FALLBACK_TOPICS, english_post_slug, is_safe_slug
from fetch import async_client, client, get, get_text
from images import download_pool, ensure_images
from paths import atomic_write_text, catalog_path, posts_dir, raw_dir

SITEMAP = "https://blog.cloudflare.com/sitemap.xml"
SITEMAP_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
TAG_PREFIX = "https://blog.cloudflare.com/tag/"


@dataclass(frozen=True)
class PostEntry:
    slug: str
    url: str
    lastmod: str


def sync_posts(
    *,
    limit: int = 0,
    slugs: list[str] | None = None,
    force: bool = False,
    updated: bool = False,
    concurrency: int = 4,
) -> int:
    slugs = slugs or []
    if concurrency < 1:
        print("concurrency must be at least 1")
        return 2
    entries, tag_slugs = load_sitemaps()
    to_fetch, already = _select(
        entries, limit=limit, slugs=slugs, force=force, updated=updated
    )
    print(
        f"sitemap posts {len(entries)}, tags {len(tag_slugs)}, "
        f"saved {len(already)}, to fetch {len(to_fetch)}, concurrency {concurrency}"
    )
    with download_pool(concurrency):
        failures = _backfill_images(already)
        failures.extend(asyncio.run(_fetch_all(to_fetch, concurrency)))
    catalog = _load_catalog()
    catalog["posts"] = [
        {"slug": entry.slug, "url": entry.url, "lastmod": entry.lastmod}
        for entry in sorted(entries, key=lambda item: item.lastmod, reverse=True)
    ]
    _refresh_taxonomy(catalog, tag_slugs)
    _save_catalog(catalog)
    saved = len(list(posts_dir().glob("*.json")))
    if failures:
        atomic_write_text(
            catalog_path().parent / "failures.json",
            json.dumps(failures, ensure_ascii=False, indent=2),
        )
        print(f"failed {len(failures)}")
        for item in failures:
            print(f"  {item['slug']}: {item['error']}")
        # A handful of timed-out posts should not discard the edition.
        # A run that saved little of the sitemap still fails.
        if not entries or saved < len(entries) * 0.95:
            return 1
        print(f"synced {len(entries)} listed, {saved} saved, {len(failures)} skipped")
        return 0
    print(f"synced {len(entries)} listed, {saved} saved")
    return 0


def load_catalog() -> dict:
    return _load_catalog()


def load_sitemaps() -> tuple[list[PostEntry], list[str]]:
    with client() as http:
        index = get(http, SITEMAP).text
        post_maps = [
            loc
            for loc in _locs(index, kind="sitemap")
            if "sitemap-posts" in loc
        ]
        tag_map = next((loc for loc in _locs(index, kind="sitemap") if loc.endswith("sitemap-tags.xml")), "")
        if not post_maps or not tag_map:
            raise RuntimeError("sitemap index is missing posts or tags")
        by_slug: dict[str, PostEntry] = {}
        tags: list[str] = []
        seen: set[str] = set()
        for loc in tqdm([*post_maps, tag_map], desc="sitemap", unit="file", disable=None):
            xml = get(http, loc).text
            if loc == tag_map:
                for url, _lastmod in _url_entries(xml):
                    if not url.startswith(TAG_PREFIX):
                        continue
                    slug = url[len(TAG_PREFIX):].strip("/")
                    if not is_safe_slug(slug) or slug in seen:
                        continue
                    seen.add(slug)
                    tags.append(slug)
                continue
            for url, lastmod in _url_entries(xml):
                slug = english_post_slug(url)
                if slug is None:
                    continue
                current = by_slug.get(slug)
                if current is None or lastmod > current.lastmod:
                    by_slug[slug] = PostEntry(slug, f"https://blog.cloudflare.com/{slug}", lastmod)
    return list(by_slug.values()), tags


def _locs(xml: str, *, kind: str) -> list[str]:
    root = etree.fromstring(xml.encode("utf-8"))
    parent = "sitemap" if kind == "sitemap" else "url"
    found = []
    for node in root.findall(f"s:{parent}", SITEMAP_NS):
        loc = node.findtext("s:loc", default="", namespaces=SITEMAP_NS).strip()
        if loc:
            found.append(loc)
    return found


def _url_entries(xml: str) -> list[tuple[str, str]]:
    root = etree.fromstring(xml.encode("utf-8"))
    entries = []
    for node in root.findall("s:url", SITEMAP_NS):
        loc = node.findtext("s:loc", default="", namespaces=SITEMAP_NS).strip()
        lastmod = node.findtext("s:lastmod", default="", namespaces=SITEMAP_NS).strip()
        if loc:
            entries.append((loc, lastmod))
    return entries


def _select(
    entries: list[PostEntry],
    *,
    limit: int,
    slugs: list[str],
    force: bool,
    updated: bool,
) -> tuple[list[PostEntry], list[PostEntry]]:
    """Return posts to download and posts already saved.

    Named slugs are always downloaded again. Without --limit, passing --slug
    refreshes only those articles. --updated also downloads a saved post when
    its sitemap lastmod differs. --force downloads every selected post.
    """
    by_slug = {entry.slug: entry for entry in entries}
    explicit: list[PostEntry] = []
    seen: set[str] = set()
    for raw in slugs:
        entry = _entry_for_slug(by_slug, raw)
        if entry is None or entry.slug in seen:
            continue
        explicit.append(entry)
        seen.add(entry.slug)
    if explicit and not (limit and limit > 0):
        return explicit, []

    chosen: list[PostEntry] = []
    ordered = sorted(entries, key=lambda item: item.lastmod, reverse=True)
    pool = ordered[:limit] if limit and limit > 0 else ordered
    for entry in pool:
        if entry.slug in seen:
            continue
        chosen.append(entry)
        seen.add(entry.slug)
    if force:
        return [*explicit, *chosen], []
    to_fetch = [entry for entry in chosen if _needs_fetch(entry, updated=updated)]
    already = [entry for entry in chosen if entry not in to_fetch]
    return [*explicit, *to_fetch], already


def _entry_for_slug(by_slug: dict[str, PostEntry], raw: str) -> PostEntry | None:
    slug = raw.strip().strip("/")
    if slug.startswith("https://"):
        parsed = english_post_slug(slug)
        slug = parsed or slug
    entry = by_slug.get(slug)
    if entry is None and is_safe_slug(slug):
        print(f"warning: {slug} is not in the sitemap; fetching it directly")
        return PostEntry(slug, f"https://blog.cloudflare.com/{slug}", "")
    if entry is None:
        print(f"warning: ignoring slug {slug}")
    return entry


def _unfetched_images(names: list[str], got: dict, absent: list[str]) -> list[str]:
    skipped = set(absent)
    return [name for name in dict.fromkeys(names) if name not in got and name not in skipped]


def _article_html(page: str) -> bool:
    lowered = page.lower()
    return "article-content" in lowered and "<h1" in lowered


def _needs_fetch(entry: PostEntry, *, updated: bool = False) -> bool:
    raw_path = raw_dir() / f"{entry.slug}.html"
    json_path = posts_dir() / f"{entry.slug}.json"
    if not raw_path.is_file() or not json_path.is_file():
        return True
    try:
        saved = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return True
    if not isinstance(saved, dict) or saved.get("slug") != entry.slug:
        return True
    if not updated or not entry.lastmod:
        return False
    return str(saved.get("lastmod") or "") != entry.lastmod


async def _fetch_all(entries: list[PostEntry], concurrency: int) -> list[dict[str, str]]:
    if not entries:
        return []
    semaphore = asyncio.Semaphore(concurrency)
    failures: list[dict[str, str]] = []

    with tqdm(total=len(entries), desc="posts", unit="post", disable=None) as bar:
        async with async_client(connections=concurrency) as http:
            async def one(entry: PostEntry) -> None:
                async with semaphore:
                    try:
                        try:
                            page = ""
                            post = None
                            for attempt in range(3):
                                try:
                                    page = await get_text(http, entry.url)
                                    if not _article_html(page):
                                        raise article.ParseError(
                                            f"{entry.slug}: missing article heading or body"
                                        )
                                    post = await asyncio.to_thread(
                                        article.parse_post,
                                        page,
                                        slug=entry.slug,
                                        url=entry.url,
                                        lastmod=entry.lastmod,
                                    )
                                except Exception as exc:
                                    if attempt == 2:
                                        raise
                                    tqdm.write(f"retry {entry.slug}: {exc}")
                                    await asyncio.sleep(1.2 * (attempt + 1))
                                    continue
                                break
                            if post is None:
                                raise article.ParseError(
                                    f"{entry.slug}: missing article heading or body"
                                )
                            atomic_write_text(raw_dir() / f"{entry.slug}.html", page)
                        except Exception as exc:
                            failures.append({"slug": entry.slug, "error": str(exc) or type(exc).__name__})
                            tqdm.write(f"failed {entry.slug}: {exc}")
                            return
                        atomic_write_text(
                            posts_dir() / f"{entry.slug}.json",
                            json.dumps(post, ensure_ascii=False, indent=2),
                        )
                        names = list(post.get("images") or [])
                        absent: list[str] = []
                        try:
                            got = await asyncio.to_thread(ensure_images, names, absent=absent)
                        except Exception as exc:
                            failures.append({"slug": entry.slug, "error": f"images: {exc}"})
                            tqdm.write(f"images failed {entry.slug}: {exc}")
                            return
                        missing = _unfetched_images(names, got, absent)
                        if missing:
                            failures.append({"slug": entry.slug, "error": f"{len(missing)} images failed"})
                            tqdm.write(f"images incomplete {entry.slug}: {len(missing)}/{len(names)}")
                    finally:
                        bar.set_postfix_str(entry.slug, refresh=False)
                        bar.update(1)

            await asyncio.gather(*(one(entry) for entry in entries))
    return failures


def _backfill_images(entries: list[PostEntry]) -> list[dict[str, str]]:
    """Download images for posts already on disk. Does not fetch their HTML."""
    if not entries:
        return []
    names: list[str] = []
    owner: dict[str, str] = {}
    for entry in entries:
        path = posts_dir() / f"{entry.slug}.json"
        if not path.is_file():
            continue
        try:
            post = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(post, dict):
            continue
        for name in post.get("images") or []:
            names.append(name)
            owner.setdefault(name, entry.slug)
    unique = list(dict.fromkeys(names))
    if not unique:
        return []
    print(f"images for {len(entries)} saved posts: {len(unique)} files")
    absent: list[str] = []
    try:
        with tqdm(total=len(unique), desc="images", unit="img", disable=None) as bar:
            got = ensure_images(unique, progress=bar, absent=absent)
    except Exception as exc:
        print(f"images failed: {exc}")
        return [{"slug": owner.get(name, name), "error": f"image {name}: {exc}"} for name in unique]
    missing = _unfetched_images(unique, got, absent)
    if missing:
        print(f"images failed {len(missing)}")
    else:
        print(f"images ready {len(unique)}")
    return [{"slug": owner.get(name, name), "error": f"image {name}"} for name in missing]


def _load_catalog() -> dict:
    path = catalog_path()
    if not path.is_file():
        return {"topics": [], "tags": [], "posts": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"topics": [], "tags": [], "posts": []}
    data.setdefault("topics", [])
    data.setdefault("tags", [])
    data.setdefault("posts", [])
    return data


def _save_catalog(catalog: dict) -> None:
    atomic_write_text(catalog_path(), json.dumps(catalog, ensure_ascii=False, indent=2))


def _refresh_taxonomy(catalog: dict, tag_slugs: list[str]) -> None:
    names = {item["slug"]: item["name"] for item in catalog.get("tags", []) if item.get("name")}
    topics = list(catalog.get("topics") or [])
    for path in sorted(raw_dir().glob("*.html"))[:8]:
        page = path.read_text(encoding="utf-8")
        parsed_topics = article.parse_topics(page)
        directory = article.parse_tag_directory(page)
        if parsed_topics:
            topics = parsed_topics
        for item in directory:
            names[item["slug"]] = item["name"]
        if topics and len(directory) >= 100:
            break
    if not topics:
        print("warning: topic menu was not found; using the known ten topics")
        topics = list(FALLBACK_TOPICS)
    for item in topics:
        names.setdefault(item["slug"], item["name"])
    slugs = list(dict.fromkeys([*tag_slugs, *names.keys()]))
    catalog["topics"] = topics
    catalog["tags"] = [{"slug": slug, "name": names.get(slug) or slug} for slug in sorted(slugs)]
    print(f"topics {len(topics)}: " + ", ".join(item["name"] for item in topics))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download English posts, parse them, and cache their images")
    parser.add_argument("--limit", type=int, default=0, help="download the N most recently updated posts; 0 means all that are not saved yet")
    parser.add_argument("--slug", action="append", default=[], help="re-download this post and its images; repeatable. With no --limit, only these posts are fetched")
    parser.add_argument("--force", action="store_true", help="re-download every post selected for this run")
    parser.add_argument(
        "--updated",
        action="store_true",
        help="also re-download saved posts whose sitemap lastmod differs",
    )
    parser.add_argument(
        "--concurrency",
        type=_positive_int,
        default=4,
        help="how many posts to download at once; image downloads share this limit",
    )
    args = parser.parse_args(argv)
    return sync_posts(
        limit=args.limit,
        slugs=args.slug,
        force=args.force,
        updated=args.updated,
        concurrency=args.concurrency,
    )


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("concurrency must be at least 1")
    return number


if __name__ == "__main__":
    raise SystemExit(main())
