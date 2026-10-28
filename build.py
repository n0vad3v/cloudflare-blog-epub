"""Turn cached posts into an EPUB: chronological chapters, then tag indexes."""

from __future__ import annotations

import argparse
import json
import posixpath
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree, html as lhtml
from tqdm import tqdm

from article import (
    FALLBACK_TOPICS,
    ILLEGAL_XML,
    media_filename,
    media_url,
    post_slug_from_href,
    tag_slug_from_href,
)
from epub import pack, validate_epub, write_container
from images import KINDLE_EDGE, KINDLE_QUALITY, cached_images, prepare_kindle_images
from paths import (
    atomic_write_bytes,
    build_dir,
    images_dir,
    kindle_images_dir,
    posts_dir,
    root,
)
from sync import load_catalog

XHTML = "http://www.w3.org/1999/xhtml"
OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
EPUB_NS = "http://www.idpf.org/2007/ops"
XML_NS = "http://www.w3.org/XML/1998/namespace"
BOOK_ID = "urn:uuid:8f3e2c1a-6b47-4d9e-a1c0-cfb10e000001"

BOOK_CSS = (
    """
body { font-family: Georgia, Palatino, serif; line-height: 1.6; margin: 5%; }
h1, h2, h3, h4 { font-family: Helvetica, Arial, sans-serif; line-height: 1.25; font-weight: 600; }
h1 { font-size: 1.7em; }
h2 { font-size: 1.3em; }
.meta, figcaption { color: #555555; font-size: 0.9em; }
.summary { font-size: 1.05em; }
.tags a { margin-right: 0.75em; }
a { color: #1a5fb4; }
pre { font-family: Consolas, "DejaVu Sans Mono", monospace; font-size: 0.85em; white-space: pre-wrap; background-color: #f4f4f4; padding: 0.8em 1em; }
code { font-family: Consolas, "DejaVu Sans Mono", monospace; }
img { max-width: 100%; height: auto; }
body.cover { margin: 0 0 2em; }
img.cover { width: 100%; height: auto; }
body.cover p { margin: 0.8em 6%; }
figure { margin: 1em 0; }
ul.listing { padding-left: 1.1em; }
ul.listing li { margin: 0.55em 0; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border: 1px solid #cccccc; padding: 0.3em 0.5em; vertical-align: top; }
blockquote { margin: 1em 0; padding-left: 1em; border-left: 3px solid #cccccc; }
""".strip()
    + "\n"
)


def build_book(
    *,
    limit: int = 0,
    output: Path = Path("cloudflare-blog.epub"),
    images_profile: str = "original",
    image_edge: int = KINDLE_EDGE,
    image_quality: int = KINDLE_QUALITY,
) -> int:
    catalog = load_catalog()
    posts = _load_posts()
    if limit and limit > 0:
        posts = posts[:limit]
    if not posts:
        print("No posts saved yet. Run python sync.py first.")
        return 1

    topics = catalog.get("topics") or list(FALLBACK_TOPICS)
    topic_slugs = [item["slug"] for item in topics]
    names = {item["slug"]: item["name"] for item in catalog.get("tags", [])}
    for topic in topics:
        names.setdefault(topic["slug"], topic["name"])
    for post in posts:
        for tag in post.get("tags") or []:
            names.setdefault(tag["slug"], tag["name"])

    catalog_slugs = {item["slug"] for item in catalog.get("posts", [])}
    included_slugs = {post["slug"] for post in posts}
    complete = (
        (not limit or limit <= 0)
        and bool(catalog_slugs)
        and catalog_slugs <= included_slugs
    )
    used_slugs = {
        tag["slug"]
        for post in posts
        for tag in post.get("tags") or []
        if tag.get("slug")
    }
    if complete:
        page_slugs = list(dict.fromkeys([*names.keys(), *used_slugs, *topic_slugs]))
    else:
        page_slugs = list(dict.fromkeys([*used_slugs, *topic_slugs]))
    tags = [{"slug": slug, "name": names.get(slug) or slug} for slug in page_slugs]
    tags.sort(key=lambda item: (item["name"].casefold(), item["slug"]))

    post_hrefs = {
        post["slug"]: f"posts/{_year(post)}/{post['slug']}.xhtml" for post in posts
    }
    tag_hrefs = {tag["slug"]: f"tags/{tag['slug']}.xhtml" for tag in tags}
    grouped = {
        slug: [post for post in posts if _has_tag(post, slug)] for slug in tag_hrefs
    }
    topic_set = set(topic_slugs)
    uncategorized = [
        post for post in posts if not any(_has_tag(post, slug) for slug in topic_set)
    ]

    image_names = [name for post in posts for name in post.get("images") or []]
    images = cached_images(image_names)
    missing_images = sorted({name for name in image_names if name not in images})
    if missing_images:
        print(
            f"warning: {len(missing_images)} images are not on disk and will be skipped"
        )
    image_root = images_dir()
    if images_profile == "kindle":
        images = prepare_kindle_images(images, edge=image_edge, quality=image_quality)
        image_root = kindle_images_dir(image_edge, image_quality)
        failed = sorted(
            {
                name
                for name in image_names
                if name not in images and name not in missing_images
            }
        )
        if failed:
            print(
                f"warning: {len(failed)} images failed to compress and will be skipped"
            )

    book = build_dir()
    if book.exists():
        shutil.rmtree(book)
    oebps = book / "OEBPS"
    oebps.mkdir(parents=True)
    write_container(book)
    (oebps / "css").mkdir()
    (oebps / "css" / "book.css").write_text(BOOK_CSS, encoding="utf-8")
    for info in _track(images.values(), "copy images", "img"):
        source = image_root / info["file"]
        target = oebps / "images" / info["file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    context = {
        "post_slugs": included_slugs,
        "post_hrefs": post_hrefs,
        "tag_hrefs": tag_hrefs,
        "images": images,
    }
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_cover_image(oebps / "images" / "cover.jpg", posts)
    _write_cover(oebps / "cover.xhtml", posts, catalog, complete, generated)
    _write_topics_page(oebps / "topics.xhtml", topics, grouped, uncategorized, context)
    _write_tag_index(oebps / "tags.xhtml", tags, grouped, context)
    for tag in _track(tags, "tag pages", "page"):
        _write_tag_page(
            oebps / tag_hrefs[tag["slug"]], tag, grouped[tag["slug"]], context
        )
    for post in _track(posts, "chapters", "post"):
        _write_post(oebps / post_hrefs[post["slug"]], post, context)

    other_tags = [tag for tag in tags if tag["slug"] not in topic_set]
    manifest: list[tuple[str, str, str, str]] = [
        ("cover", "cover.xhtml", "application/xhtml+xml", ""),
        ("topics", "topics.xhtml", "application/xhtml+xml", ""),
        ("all-tags", "tags.xhtml", "application/xhtml+xml", ""),
        ("nav", "nav.xhtml", "application/xhtml+xml", "nav"),
        ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
        ("css", "css/book.css", "text/css", ""),
        ("cover-image", "images/cover.jpg", "image/jpeg", "cover-image"),
    ]
    for post in posts:
        manifest.append(
            (
                f"post-{post['slug']}",
                post_hrefs[post["slug"]],
                "application/xhtml+xml",
                "",
            )
        )
    spine = ["cover", *[f"post-{post['slug']}" for post in posts], "topics"]
    for topic in topics:
        if topic["slug"] in tag_hrefs:
            spine.append(f"tag-{topic['slug']}")
    spine.append("all-tags")
    for tag in other_tags:
        spine.append(f"tag-{tag['slug']}")
    for tag in tags:
        manifest.append(
            (f"tag-{tag['slug']}", tag_hrefs[tag["slug"]], "application/xhtml+xml", "")
        )
    for index, info in enumerate(images.values()):
        manifest.append(
            (f"img-{index}", f"images/{info['file']}", info["media_type"], "")
        )

    _dedupe_ids(manifest)
    _write_nav(oebps / "nav.xhtml", topics, grouped, tag_hrefs)
    _write_ncx(oebps / "toc.ncx", topics, grouped, tag_hrefs)
    _write_opf(oebps / "content.opf", manifest, spine, generated)

    destination = output if output.is_absolute() else root() / output
    pack(book, destination)
    errors = validate_epub(destination)
    print(
        f"wrote {destination} ({destination.stat().st_size} bytes, {len(posts)} posts, {len(tags)} tags)"
    )
    if errors:
        for error in errors[:30]:
            print(f"invalid: {error}")
        if len(errors) > 30:
            print(f"invalid: {len(errors) - 30} more")
        return 1
    return _epubcheck(destination)


def _track(items, desc: str, unit: str):
    items = list(items)
    if not items:
        return []
    return tqdm(items, desc=desc, unit=unit, disable=None)


def _load_posts() -> list[dict]:
    posts = []
    paths = sorted(posts_dir().glob("*.json"))
    for path in _track(paths, "read posts", "post"):
        try:
            post = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            tqdm.write(f"warning: skipping unreadable {path.name}")
            continue
        slug = post.get("slug") or ""
        if not slug:
            continue
        posts.append(post)
    posts.sort(key=lambda post: post["slug"])
    posts.sort(
        key=lambda post: post.get("published_at") or post.get("published") or "",
        reverse=True,
    )
    return posts


def _has_tag(post: dict, slug: str) -> bool:
    return any(tag.get("slug") == slug for tag in post.get("tags") or [])


def _year(post: dict) -> str:
    published = post.get("published") or ""
    if len(published) >= 4 and published[:4].isdigit():
        return published[:4]
    return "undated"


def _write_cover_image(path: Path, posts: list[dict]) -> None:
    from PIL import Image, ImageDraw

    width, height = 1600, 2560
    canvas = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(canvas)
    ink = "#1A1A1A"
    orange = "#E06A12"
    muted = "#595959"
    display = _cover_font(bold=False, size=34)
    title = _cover_font(bold=True, size=118)
    meta = _cover_font(bold=False, size=42)
    years = _year_span(posts)
    count = f"{len(posts):,} {'post' if len(posts) == 1 else 'posts'}"
    lines = [
        ("OFFLINE ARCHIVE", display, orange, 56),
        ("Cloudflare", title, ink, 12),
        ("Blog", title, orange, 72),
    ]
    if years:
        lines.append((years, meta, ink, 16))
    lines.append((count, meta, muted, 64))
    lines.append(("blog.cloudflare.com", display, muted, 0))

    measured = []
    total = 0
    for text, font, fill, gap in lines:
        box = draw.textbbox((0, 0), text, font=font)
        measured.append((text, font, fill, gap, box))
        total += (box[3] - box[1]) + gap
    y = (height - total) // 2
    for text, font, fill, gap, box in measured:
        line_width = box[2] - box[0]
        line_height = box[3] - box[1]
        draw.text(
            ((width - line_width) // 2 - box[0], y - box[1]), text, font=font, fill=fill
        )
        y += line_height + gap
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, format="JPEG", quality=92, optimize=True)


def _cover_font(*, bold: bool, size: int):
    from PIL import ImageFont

    names = (
        (
            "/usr/share/fonts/julietaula-montserrat-fonts/Montserrat-Bold.otf",
            "/usr/share/fonts/redhat/RedHatDisplay-Bold.otf",
            "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Bold.ttf",
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        )
        if bold
        else (
            "/usr/share/fonts/julietaula-montserrat-fonts/Montserrat-Medium.otf",
            "/usr/share/fonts/redhat/RedHatDisplay-Medium.otf",
            "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Regular.ttf",
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/Library/Fonts/Arial.ttf",
            "C:/Windows/Fonts/arial.ttf",
        )
    )
    for candidate in names:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _year_span(posts: list[dict]) -> str:
    years = sorted(
        {
            (post.get("published") or "")[:4]
            for post in posts
            if (post.get("published") or "")[:4].isdigit()
        }
    )
    if not years:
        return ""
    if years[0] == years[-1]:
        return years[0]
    return f"{years[0]}–{years[-1]}"


def _write_cover(
    path: Path, posts: list[dict], catalog: dict, complete: bool, generated: str
) -> None:
    listed = len(catalog.get("posts") or [])
    count = f"{len(posts):,} {'post' if len(posts) == 1 else 'posts'}"
    scope = f"This edition includes {count}."
    if listed and listed != len(posts):
        total = f"{listed:,} {'post' if listed == 1 else 'posts'}"
        scope = f"This edition includes {count}. The site lists {total}; this file does not include all of them."
    scope += f" Generated {generated[:10]}."
    image = el("img")
    image.set("class", "cover")
    image.set("src", "images/cover.jpg")
    image.set("alt", "Cloudflare Blog, an offline archive of the English posts")
    blocks = [
        image,
        el(
            "p",
            "A personal offline archive of the English posts. Chapters run newest first, and each post appears once.",
        ),
        el(
            "p",
            "A post with several tags is linked from each of those tag pages to the same chapter.",
        ),
        el(
            "p",
            "The reader contents list the site's featured topics. An index of every tag follows the chapters. Posts that carry none of those topics are listed on the Topics page under Outside these topics.",
        ),
        el(
            "p",
            "Post text and images are copyright Cloudflare. Each chapter keeps its authors and original URL. This ebook is for personal offline reading.",
        ),
        el("p", scope),
    ]
    if not complete:
        blocks.append(
            el(
                "p",
                "This is a partial archive. Tag pages count only posts that have been downloaded.",
            )
        )
    source = el("p")
    link = el("a", "https://blog.cloudflare.com/")
    link.set("href", "https://blog.cloudflare.com/")
    source.append(link)
    blocks.append(source)
    _write_xhtml(path, "Cover", "en", "css/book.css", blocks, body_class="cover")


def _write_topics_page(path, topics, grouped, uncategorized, context) -> None:
    from_file = "topics.xhtml"
    blocks = [
        el("h1", "Topics"),
        el(
            "p",
            "These are the topics in the site navigation. A post can belong to more than one.",
        ),
    ]
    listing = el("ul")
    listing.set("class", "listing")
    for topic in topics:
        slug = topic["slug"]
        if slug not in context["tag_hrefs"]:
            continue
        item = el("li")
        link = el("a", f"{topic['name']} ({len(grouped.get(slug, []))})")
        link.set("href", rel_link(from_file, context["tag_hrefs"][slug]))
        item.append(link)
        listing.append(item)
    blocks.append(listing)
    blocks.append(el("h2", "Outside these topics"))
    if uncategorized:
        outside = f"{len(uncategorized):,} {'post is' if len(uncategorized) == 1 else 'posts are'} not filed under those topics."
        blocks.append(el("p", outside))
        blocks.append(_post_listing(uncategorized, from_file, context))
    else:
        blocks.append(
            el("p", "Every included post is filed under one of the topics above.")
        )
    _write_xhtml(path, "Topics", "en", rel_link(from_file, "css/book.css"), blocks)


def _write_tag_index(path, tags, grouped, context) -> None:
    from_file = "tags.xhtml"
    blocks = [
        el("h1", "All tags"),
        el(
            "p",
            "Sorted by name. The number in parentheses is how many posts in this edition use the tag.",
        ),
    ]
    groups: dict[str, list[dict]] = defaultdict(list)
    for tag in tags:
        groups[_bucket(tag["name"])].append(tag)
    for key in sorted(groups, key=_bucket_key):
        blocks.append(el("h2", key))
        listing = el("ul")
        listing.set("class", "listing")
        for tag in groups[key]:
            item = el("li")
            link = el("a", f"{tag['name']} ({len(grouped.get(tag['slug'], []))})")
            link.set("href", rel_link(from_file, context["tag_hrefs"][tag["slug"]]))
            item.append(link)
            listing.append(item)
        blocks.append(listing)
    _write_xhtml(path, "All tags", "en", rel_link(from_file, "css/book.css"), blocks)


def _write_tag_page(path: Path, tag: dict, posts: list[dict], context) -> None:
    from_file = context["tag_hrefs"][tag["slug"]]
    blocks = [el("h1", tag["name"])]
    count = el("p", f"{len(posts):,} {'post' if len(posts) == 1 else 'posts'}")
    count.set("class", "meta")
    blocks.append(count)
    back = el("p")
    back.set("class", "meta")
    link = el("a", "All tags")
    link.set("href", rel_link(from_file, "tags.xhtml"))
    back.append(link)
    blocks.append(back)
    if posts:
        blocks.append(_post_listing(posts, from_file, context))
    else:
        blocks.append(el("p", "No posts under this tag in this edition."))
    _write_xhtml(path, tag["name"], "en", rel_link(from_file, "css/book.css"), blocks)


def _write_post(path: Path, post: dict, context) -> None:
    from_file = context["post_hrefs"][post["slug"]]
    blocks = [el("h1", post["title"])]
    bits = []
    if post.get("published"):
        bits.append(post["published"])
    authors = ", ".join(
        author["name"] for author in post.get("authors") or [] if author.get("name")
    )
    if authors:
        bits.append(authors)
    if bits:
        meta = el("p", " · ".join(bits))
        meta.set("class", "meta")
        blocks.append(meta)
    origin = el("p")
    origin.set("class", "meta")
    original = el("a", "Original")
    original.set("href", post["url"])
    origin.append(original)
    blocks.append(origin)
    tag_links = [
        tag for tag in post.get("tags") or [] if tag.get("slug") in context["tag_hrefs"]
    ]
    if tag_links:
        paragraph = el("p")
        paragraph.set("class", "tags")
        for index, tag in enumerate(tag_links):
            link = el("a", tag["name"])
            link.set("href", rel_link(from_file, context["tag_hrefs"][tag["slug"]]))
            if index:
                paragraph[-1].tail = " "
            paragraph.append(link)
        blocks.append(paragraph)
    if post.get("description"):
        summary = el("p", post["description"])
        summary.set("class", "summary")
        blocks.append(summary)
    hero = _hero_figure(post, from_file, context)
    if hero is not None:
        blocks.append(hero)
    blocks.extend(_body_blocks(post, from_file, context))
    _write_xhtml(path, post["title"], "en", rel_link(from_file, "css/book.css"), blocks)


def _post_listing(posts: list[dict], from_file: str, context) -> etree._Element:
    listing = el("ul")
    listing.set("class", "listing")
    for post in posts:
        item = el("li")
        link = el("a", post["title"])
        link.set("href", rel_link(from_file, context["post_hrefs"][post["slug"]]))
        item.append(link)
        bits = []
        if post.get("published"):
            bits.append(post["published"])
        authors = ", ".join(
            author["name"] for author in post.get("authors") or [] if author.get("name")
        )
        if authors:
            bits.append(authors)
        if bits:
            link.tail = " "
            meta = el("span", " · ".join(bits))
            meta.set("class", "meta")
            item.append(meta)
        listing.append(item)
    return listing


def _hero_figure(post: dict, from_file: str, context):
    hero = post.get("hero")
    if not hero or media_url(hero) in (post.get("body_html") or ""):
        return None
    info = context["images"].get(hero)
    if not info:
        return None
    figure = el("figure")
    image = el("img")
    image.set("src", rel_link(from_file, f"images/{info['file']}"))
    image.set("alt", post.get("title") or "")
    if info.get("width"):
        image.set("width", str(info["width"]))
        image.set("height", str(info["height"]))
    figure.append(image)
    return figure


def _body_blocks(post: dict, from_file: str, context) -> list[etree._Element]:
    raw = (post.get("body_html") or "").strip()
    if not raw:
        return []
    try:
        root = lhtml.fragment_fromstring(raw, create_parent="div")
    except (etree.ParserError, etree.XMLSyntaxError):
        return [el("p", "This chapter could not be turned into an ebook page.")]
    for anchor in root.xpath(".//a[@href]"):
        rewritten = _rewrite_href(anchor.get("href") or "", from_file, context)
        if rewritten:
            anchor.set("href", rewritten)
        else:
            anchor.drop_tag()
    for image in root.xpath(".//img[@src]"):
        filename = media_filename(image.get("src"))
        info = context["images"].get(filename) if filename else None
        if not info:
            continue
        image.set("src", rel_link(from_file, f"images/{info['file']}"))
        if info.get("width"):
            image.set("width", str(info["width"]))
            image.set("height", str(info["height"]))
    _retag(root)
    blocks = []
    if root.text and root.text.strip():
        blocks.append(el("p", root.text.strip()))
    blocks.extend(list(root))
    return blocks


def _rewrite_href(href: str, from_file: str, context) -> str:
    href = href.strip()
    if not href or href.startswith(("javascript:", "data:")):
        return ""
    if href.startswith("#") or href.startswith("mailto:"):
        return href
    if href.startswith("//"):
        href = "https:" + href
    fragment = ""
    if "#" in href:
        href, fragment = href.split("#", 1)
        fragment = "#" + fragment
    post_slug = post_slug_from_href(href, context["post_slugs"])
    if post_slug and post_slug in context["post_hrefs"]:
        return rel_link(from_file, context["post_hrefs"][post_slug]) + fragment
    tag_slug = tag_slug_from_href(href)
    if tag_slug and tag_slug in context["tag_hrefs"]:
        return rel_link(from_file, context["tag_hrefs"][tag_slug]) + fragment
    if href.startswith("/"):
        return "https://blog.cloudflare.com" + href + fragment
    return href + fragment


def _write_nav(path: Path, topics, grouped, tag_hrefs) -> None:
    html = etree.Element(f"{{{XHTML}}}html", nsmap={None: XHTML, "epub": EPUB_NS})
    html.set(f"{{{XML_NS}}}lang", "en")
    head = etree.SubElement(html, f"{{{XHTML}}}head")
    title = etree.SubElement(head, f"{{{XHTML}}}title")
    title.text = "Contents"
    body = etree.SubElement(html, f"{{{XHTML}}}body")
    nav = etree.SubElement(body, f"{{{XHTML}}}nav")
    nav.set(f"{{{EPUB_NS}}}type", "toc")
    nav.set("id", "toc")
    heading = etree.SubElement(nav, f"{{{XHTML}}}h1")
    heading.text = "Contents"
    listing = etree.SubElement(nav, f"{{{XHTML}}}ol")
    _nav_li(listing, "cover.xhtml", "Cover")
    topic_item = _nav_li(listing, "topics.xhtml", "Topics")
    nested = etree.SubElement(topic_item, f"{{{XHTML}}}ol")
    for topic in topics:
        if topic["slug"] not in tag_hrefs:
            continue
        label = f"{topic['name']} ({len(grouped.get(topic['slug'], []))})"
        _nav_li(nested, tag_hrefs[topic["slug"]], label)
    _nav_li(listing, "tags.xhtml", "All tags")
    _write_tree(path, html)


def _nav_li(parent, href: str, label: str):
    item = etree.SubElement(parent, f"{{{XHTML}}}li")
    link = etree.SubElement(item, f"{{{XHTML}}}a")
    link.set("href", href)
    link.text = label
    return item


def _write_ncx(path: Path, topics, grouped, tag_hrefs) -> None:
    ncx = etree.Element(f"{{{NCX_NS}}}ncx", nsmap={None: NCX_NS})
    ncx.set("version", "2005-1")
    ncx.set(f"{{{XML_NS}}}lang", "en")
    head = etree.SubElement(ncx, f"{{{NCX_NS}}}head")
    for name, content in (
        ("dtb:uid", BOOK_ID),
        ("dtb:depth", "2"),
        ("dtb:totalPageCount", "0"),
        ("dtb:maxPageNumber", "0"),
    ):
        meta = etree.SubElement(head, f"{{{NCX_NS}}}meta")
        meta.set("name", name)
        meta.set("content", content)
    title = etree.SubElement(
        etree.SubElement(ncx, f"{{{NCX_NS}}}docTitle"), f"{{{NCX_NS}}}text"
    )
    title.text = "Cloudflare Blog"
    author = etree.SubElement(
        etree.SubElement(ncx, f"{{{NCX_NS}}}docAuthor"), f"{{{NCX_NS}}}text"
    )
    author.text = "Cloudflare"
    nav_map = etree.SubElement(ncx, f"{{{NCX_NS}}}navMap")
    play = 0

    def point(parent, identifier: str, label: str, src: str):
        nonlocal play
        play += 1
        node = etree.SubElement(parent, f"{{{NCX_NS}}}navPoint")
        node.set("id", identifier)
        node.set("playOrder", str(play))
        text = etree.SubElement(
            etree.SubElement(node, f"{{{NCX_NS}}}navLabel"), f"{{{NCX_NS}}}text"
        )
        text.text = label
        content = etree.SubElement(node, f"{{{NCX_NS}}}content")
        content.set("src", src)
        return node

    point(nav_map, "cover", "Cover", "cover.xhtml")
    topics_point = point(nav_map, "topics", "Topics", "topics.xhtml")
    for topic in topics:
        if topic["slug"] not in tag_hrefs:
            continue
        label = f"{topic['name']} ({len(grouped.get(topic['slug'], []))})"
        point(topics_point, f"topic-{topic['slug']}", label, tag_hrefs[topic["slug"]])
    point(nav_map, "all-tags", "All tags", "tags.xhtml")
    doctype = (
        '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" '
        '"http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">\n'
    )
    body = etree.tostring(ncx, encoding="utf-8", xml_declaration=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b'<?xml version="1.0" encoding="utf-8"?>\n' + doctype.encode("utf-8") + body
    )


def _write_opf(path: Path, manifest, spine_ids: list[str], generated: str) -> None:
    package = etree.Element(f"{{{OPF}}}package", nsmap={None: OPF, "dc": DC})
    package.set("version", "3.0")
    package.set("unique-identifier", "BookId")
    metadata = etree.SubElement(package, f"{{{OPF}}}metadata")
    identifier = etree.SubElement(metadata, f"{{{DC}}}identifier")
    identifier.set("id", "BookId")
    identifier.text = BOOK_ID
    title = etree.SubElement(metadata, f"{{{DC}}}title")
    title.text = "Cloudflare Blog"
    language = etree.SubElement(metadata, f"{{{DC}}}language")
    language.text = "en"
    creator = etree.SubElement(metadata, f"{{{DC}}}creator")
    creator.text = "Cloudflare"
    source = etree.SubElement(metadata, f"{{{DC}}}source")
    source.text = "https://blog.cloudflare.com/"
    rights = etree.SubElement(metadata, f"{{{DC}}}rights")
    rights.text = (
        "Copyright Cloudflare. Personal offline archive; "
        "each chapter keeps its original URL and authors."
    )
    modified = etree.SubElement(metadata, f"{{{OPF}}}meta")
    modified.set("property", "dcterms:modified")
    modified.text = generated
    cover = etree.SubElement(metadata, f"{{{OPF}}}meta")
    cover.set("name", "cover")
    cover.set("content", "cover-image")
    manifest_el = etree.SubElement(package, f"{{{OPF}}}manifest")
    seen_hrefs: set[str] = set()
    for item_id, href, media_type, properties in manifest:
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        item = etree.SubElement(manifest_el, f"{{{OPF}}}item")
        item.set("id", item_id)
        item.set("href", href)
        item.set("media-type", media_type)
        if properties:
            item.set("properties", properties)
    spine = etree.SubElement(package, f"{{{OPF}}}spine")
    spine.set("toc", "ncx")
    for item_id in spine_ids:
        ref = etree.SubElement(spine, f"{{{OPF}}}itemref")
        ref.set("idref", item_id)
    _write_tree(path, package)


def _dedupe_ids(manifest) -> None:
    seen: set[str] = set()
    for item_id, _href, _media, _properties in manifest:
        if item_id in seen:
            raise RuntimeError(f"duplicate manifest id {item_id}")
        seen.add(item_id)


def _write_xhtml(
    path: Path, title: str, lang: str, css_href: str, blocks: list, body_class: str = ""
) -> None:
    html = etree.Element(f"{{{XHTML}}}html", nsmap={None: XHTML})
    html.set(f"{{{XML_NS}}}lang", lang)
    head = etree.SubElement(html, f"{{{XHTML}}}head")
    title_el = etree.SubElement(head, f"{{{XHTML}}}title")
    title_el.text = ILLEGAL_XML.sub("", title) or "Cloudflare Blog"
    link = etree.SubElement(head, f"{{{XHTML}}}link")
    link.set("rel", "stylesheet")
    link.set("type", "text/css")
    link.set("href", css_href)
    body = etree.SubElement(html, f"{{{XHTML}}}body")
    if body_class:
        body.set("class", body_class)
    for block in blocks:
        body.append(block)
    _write_tree(path, html)


def _write_tree(path: Path, element: etree._Element) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(
        path,
        etree.tostring(element, xml_declaration=True, encoding="utf-8"),
    )


def el(tag: str, text: str | None = None) -> etree._Element:
    node = etree.Element(f"{{{XHTML}}}{tag}")
    if text:
        node.text = ILLEGAL_XML.sub("", text)
    return node


def rel_link(from_file: str, to_file: str) -> str:
    return posixpath.relpath(to_file, posixpath.dirname(from_file))


def _retag(node: etree._Element) -> None:
    if isinstance(node.tag, str):
        node.tag = f"{{{XHTML}}}{etree.QName(node).localname}"
    for child in node:
        _retag(child)


def _bucket(name: str) -> str:
    letter = name[:1].upper()
    if letter.isascii() and letter.isalpha():
        return letter
    if letter.isdigit():
        return "0-9"
    return "#"


def _bucket_key(label: str) -> tuple:
    if label == "0-9":
        return (0, label)
    if label == "#":
        return (2, label)
    return (1, label)


def _epubcheck(path: Path) -> int:
    import subprocess

    executable = shutil.which("epubcheck")
    if not executable:
        print("epubcheck is not installed; structural checks passed")
        return 0
    result = subprocess.run([executable, str(path)], text=True, capture_output=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    if result.returncode != 0:
        print(f"epubcheck failed with status {result.returncode}")
        return result.returncode
    print("epubcheck passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build an EPUB from posts and images already on disk"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="include only the N newest saved posts"
    )
    parser.add_argument(
        "--images",
        choices=("original", "kindle"),
        default="original",
        help="original uses downloaded images; kindle writes a smaller JPEG copy, 1200px and quality 70 by default",
    )
    parser.add_argument(
        "--image-edge",
        type=_image_edge,
        default=KINDLE_EDGE,
        help="maximum image edge for --images kindle; images are scaled down, never cropped",
    )
    parser.add_argument(
        "--image-quality",
        type=_image_quality,
        default=KINDLE_QUALITY,
        help="JPEG quality for --images kindle, from 1 to 95",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="EPUB path. Relative paths are written next to these scripts. Defaults to cloudflare-blog-kindle.epub with --images kindle",
    )
    args = parser.parse_args(argv)
    if args.output is None:
        output = Path(
            "cloudflare-blog-kindle.epub"
            if args.images == "kindle"
            else "cloudflare-blog.epub"
        )
    else:
        output = args.output
    return build_book(
        limit=args.limit,
        output=output,
        images_profile=args.images,
        image_edge=args.image_edge,
        image_quality=args.image_quality,
    )


def _image_edge(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("edge must be at least 1")
    return number


def _image_quality(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an integer from 1 to 95") from exc
    if not 1 <= number <= 95:
        raise argparse.ArgumentTypeError("JPEG quality must be from 1 to 95")
    return number


if __name__ == "__main__":
    raise SystemExit(main())
