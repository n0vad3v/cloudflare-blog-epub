"""Extract a Cloudflare Blog post and the site-wide topic menu from page HTML."""

from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, Comment, Tag

# Single path segment. Underscores show up in older slugs such as io_uring.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# Language homes are /{ll-cc}/... . A one-segment slug can still look like this
# (the post cf-ui), so this is only applied when another segment follows.
LOCALE_RE = re.compile(r"^[a-z]{2}-[a-z]{2}$")
MEDIA_RE = re.compile(r"/_emdash/api/media/file/([^/?#&\s]+)", re.IGNORECASE)
ILLEGAL_XML = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\uD800-\uDFFF]")
DROP_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "button",
    "svg",
    "form",
    "input",
    "textarea",
    "canvas",
    "nav",
    "source",
}
EMBED_TAGS = {"iframe", "video", "audio", "embed", "object"}
UNWRAP_TAGS = {"div", "span", "u", "section", "font", "picture", "center", "aside"}

# Used only when a fetched page does not contain the header topic menu.
FALLBACK_TOPICS = [
    {"slug": "ai", "name": "AI"},
    {"slug": "developers", "name": "Developers"},
    {"slug": "cloudflare-radar", "name": "Radar"},
    {"slug": "product-news", "name": "Product News"},
    {"slug": "security", "name": "Security"},
    {"slug": "policy", "name": "Policy & Legal"},
    {"slug": "zero-trust", "name": "Zero Trust"},
    {"slug": "speed-and-reliability", "name": "Speed & Reliability"},
    {"slug": "life-at-cloudflare", "name": "Life at Cloudflare"},
    {"slug": "partners", "name": "Partners"},
]


class ParseError(ValueError):
    pass


def is_safe_slug(slug: str) -> bool:
    return bool(SLUG_RE.fullmatch(slug)) and slug not in {".", ".."}


def english_post_slug(url: str) -> str | None:
    """Return the slug for an untranslated post URL, else None.

    Translated copies live at /{ll-cc}/{slug}. Those are the same article.
    """
    try:
        path = urlparse(url).path
    except ValueError:
        return None
    parts = [part for part in path.split("/") if part]
    if len(parts) != 1:
        return None
    slug = parts[0].lower()
    if not is_safe_slug(slug):
        return None
    return slug


def tag_slug_from_href(href: str) -> str | None:
    parts = _blog_path_parts(href)
    if parts is None:
        return None
    if len(parts) >= 2 and LOCALE_RE.fullmatch(parts[0]):
        parts = parts[1:]
    if len(parts) == 2 and parts[0].lower() == "tag":
        slug = parts[1].lower()
        if is_safe_slug(slug):
            return slug
    return None


def post_slug_from_href(href: str, known_posts: set[str]) -> str | None:
    parts = _blog_path_parts(href)
    if parts is None or not parts:
        return None
    if parts[0].lower() in {"tag", "author", "page", "preview", "cdn-cgi", "_emdash"}:
        return None
    if len(parts) >= 2 and LOCALE_RE.fullmatch(parts[0].lower()):
        parts = parts[1:]
    if len(parts) == 1 and parts[0].lower() in known_posts:
        return parts[0].lower()
    return None


def media_filename(value: str | None) -> str | None:
    if not value:
        return None
    decoded = unquote(unescape(value))
    match = MEDIA_RE.search(decoded)
    if not match:
        return None
    name = match.group(1).strip()
    if not name or "/" in name or name in {".", ".."}:
        return None
    return name


def media_url(filename: str) -> str:
    return "https://blog.cloudflare.com/_emdash/api/media/file/" + filename


def parse_topics(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    for listing in soup.select("div.dropdown-container ul"):
        items: list[dict[str, str]] = []
        for item in listing.find_all("li", recursive=False):
            if "data-tag-item" in item.attrs:
                continue
            anchor = item.find("a", href=True)
            if anchor is None:
                continue
            slug = tag_slug_from_href(anchor["href"])
            name = _clean(anchor.get_text(" ", strip=True))
            if slug and name:
                items.append({"slug": slug, "name": name})
        slugs = [item["slug"] for item in items]
        if "ai" in slugs and 5 <= len(items) <= 30:
            return _dedupe_named(items)
    return []


def parse_tag_directory(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    items: list[dict[str, str]] = []
    for item in soup.select("li[data-tag-item]"):
        anchor = item.find("a", href=True)
        if anchor is None:
            continue
        slug = tag_slug_from_href(anchor["href"])
        if not slug:
            continue
        name = _clean(anchor.get_text(" ", strip=True)) or _clean(
            item.get("data-tag-label") or ""
        )
        if name:
            items.append({"slug": slug, "name": name})
    return _dedupe_named(items)


def parse_post(html: str, *, slug: str, url: str, lastmod: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article") or soup
    content = article.find("div", class_="article-content")
    heading = article.find("h1")
    if content is None or heading is None:
        raise ParseError(f"{slug}: missing article heading or body")

    linked = _header_links(heading, content)
    meta = _json_ld(soup)
    title = (
        _clean(heading.get_text(" ", strip=True))
        or _clean(str(meta.get("headline") or ""))
        or slug
    )
    description = _clean(str(meta.get("description") or "")) or _meta_content(
        soup, "og:description"
    )
    published_at = str(meta.get("datePublished") or "")
    published = (
        published_at[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", published_at[:10])
        else ""
    )
    authors = linked["authors"] or _authors_from_meta(meta)
    hero = media_filename(_image_value(meta.get("image")))
    images = _clean_article(content)
    if hero and hero not in images:
        images.insert(0, hero)

    return {
        "slug": slug,
        "url": url,
        "title": title,
        "description": description,
        "published": published,
        "published_at": published_at,
        "authors": authors,
        "tags": linked["tags"],
        "hero": hero,
        "images": images,
        "body_html": content.decode_contents(),
        "lastmod": lastmod,
    }


def _header_links(heading: Tag, content: Tag) -> dict[str, list]:
    """Authors sit beside the title. This post's tags are the footer tag row.

    The same header also embeds every tag on the site, starting at
    data-all-tags-heading. Those links are not this post's tags.
    """
    article = heading.find_parent("article") or heading.parent
    tags = _post_tags(article)
    authors: list[dict[str, str]] = []
    for element in heading.next_elements:
        if element == content:
            break
        if not isinstance(element, Tag) or element.name != "a":
            continue
        author_slug = _author_slug(element.get("href") or "")
        text = _clean(element.get_text(" ", strip=True))
        if author_slug and text:
            authors.append({"slug": author_slug, "name": text})
    return {"tags": tags, "authors": _dedupe_named(authors)}


def _post_tags(article: Tag) -> list[dict[str, str]]:
    for row in article.select("div.tags"):
        if row.find_parent("div", class_="article-content") is not None:
            continue
        if row.find_parent(attrs={"data-tag-item": True}) is not None:
            continue
        tags = _tag_anchors(row.find_all("a", href=True))
        if tags:
            return tags
    header = article.find("header") or article
    tags = []
    for anchor in header.find_all("a", href=True):
        if anchor.find_parent(attrs={"data-tag-item": True}) is not None:
            break
        if anchor.find_parent(attrs={"data-all-tags-heading": True}) is not None:
            break
        tags.extend(_tag_anchors([anchor]))
    return _dedupe_named(tags)


def _tag_anchors(anchors) -> list[dict[str, str]]:
    tags = []
    for anchor in anchors:
        slug = tag_slug_from_href(anchor.get("href") or "")
        text = _clean(anchor.get_text(" ", strip=True))
        if slug and text:
            tags.append({"slug": slug, "name": text})
    return _dedupe_named(tags)


def _author_slug(href: str) -> str | None:
    parts = _blog_path_parts(href)
    if parts is None:
        return None
    if len(parts) >= 2 and LOCALE_RE.fullmatch(parts[0]):
        parts = parts[1:]
    if len(parts) == 2 and parts[0].lower() == "author":
        slug = parts[1].lower()
        if is_safe_slug(slug):
            return slug
    return None


def _blog_path_parts(href: str) -> list[str] | None:
    if not href or href.startswith(("#", "mailto:", "javascript:", "data:")):
        return None
    try:
        parsed = urlparse(href)
    except ValueError:
        return None
    if parsed.scheme in {"http", "https"}:
        host = parsed.netloc.lower().removeprefix("www.")
        if host != "blog.cloudflare.com":
            return None
        path = parsed.path
    elif href.startswith("/"):
        path = href.split("?", 1)[0].split("#", 1)[0]
    else:
        return None
    return [part for part in path.split("/") if part]


def _json_ld(soup: BeautifulSoup) -> dict:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes = payload if isinstance(payload, list) else [payload]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if (
                "BlogPosting" in kinds
                or "Article" in kinds
                or node.get("datePublished")
            ):
                return node
    return {}


def _image_value(image) -> str | None:
    if isinstance(image, str):
        return image
    if isinstance(image, list) and image:
        return _image_value(image[0])
    if isinstance(image, dict):
        url = image.get("url")
        return url if isinstance(url, str) else None
    return None


def _authors_from_meta(meta: dict) -> list[dict[str, str]]:
    author = meta.get("author")
    nodes = author if isinstance(author, list) else [author]
    found: list[dict[str, str]] = []
    for node in nodes:
        if isinstance(node, str) and node.strip():
            found.append({"slug": "", "name": _clean(node)})
        elif isinstance(node, dict) and node.get("name"):
            found.append({"slug": "", "name": _clean(str(node["name"]))})
    return _dedupe_named(found)


def _meta_content(soup: BeautifulSoup, property_name: str) -> str:
    tag = soup.find("meta", attrs={"property": property_name}) or soup.find(
        "meta", attrs={"name": property_name}
    )
    if tag is None:
        return ""
    return _clean(tag.get("content") or "")


def _clean_article(content: Tag) -> list[str]:
    for comment in content.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for tag in list(content.find_all(EMBED_TAGS)):
        src = tag.get("src")
        if not src:
            source = tag.find("source")
            src = source.get("src") if source else None
        replacement = None
        if src and not str(src).startswith(("javascript:", "data:")):
            paragraph = content.new_tag("p")
            anchor = content.new_tag("a", href=str(src))
            anchor.string = str(src)
            paragraph.append(anchor)
            replacement = paragraph
        if replacement is None:
            tag.decompose()
        else:
            tag.replace_with(replacement)
    for tag in list(content.find_all(DROP_TAGS)):
        tag.decompose()

    images: list[str] = []
    for image in list(content.find_all("img")):
        src = str(image.get("src") or "")
        srcset = image.get("srcset")
        srcset_text = (
            " ".join(srcset) if isinstance(srcset, list) else str(srcset or "")
        )
        placeholder = image.has_attr("data-image-placeholder") or src.startswith(
            "data:"
        )
        filename = (
            None
            if placeholder
            else (media_filename(src) or media_filename(srcset_text))
        )
        if not filename:
            image.decompose()
            continue
        alt = _clean(image.get("alt") or "")
        image.attrs = {"src": media_url(filename)}
        if alt:
            image["alt"] = alt
        if filename not in images:
            images.append(filename)

    for block in list(content.find_all("pre")):
        lines = block.select(".line")
        text = (
            "\n".join(line.get_text() for line in lines) if lines else block.get_text()
        )
        text = ILLEGAL_XML.sub("", text).strip("\n")
        block.clear()
        block.attrs = {}
        code = content.new_tag("code")
        code.string = text
        block.append(code)

    for tag in list(content.find_all(UNWRAP_TAGS)):
        tag.unwrap()

    for tag in list(content.find_all(True)):
        if not isinstance(tag, Tag) or tag.parent is None:
            continue
        href = tag.get("href")
        if (
            tag.name == "a"
            and href
            and str(href).lower().startswith(("javascript:", "data:"))
        ):
            tag.unwrap()
            continue
        kept: dict[str, str] = {}
        if tag.name == "a" and href:
            kept["href"] = str(href).strip()
        if tag.name == "img":
            kept["src"] = str(tag.get("src") or "")
            if tag.get("alt"):
                kept["alt"] = str(tag["alt"])
        if tag.name in {"td", "th"}:
            for key in ("colspan", "rowspan"):
                if tag.get(key):
                    kept[key] = str(tag[key])
        if tag.name == "ol" and tag.get("start"):
            kept["start"] = str(tag["start"])
        identifier = tag.get("id")
        if identifier and re.fullmatch(r"[A-Za-z][\w:.-]*", str(identifier)):
            kept["id"] = str(identifier)
        tag.attrs = kept

    for text in list(content.find_all(string=True)):
        cleaned = ILLEGAL_XML.sub("", str(text))
        if cleaned != str(text):
            text.replace_with(cleaned)

    for tag in reversed(list(content.find_all(True))):
        if tag.parent is None or tag.name in {"img", "br", "hr"}:
            continue
        if not tag.get_text(strip=True) and tag.find("img") is None:
            tag.decompose()
    return images


def _dedupe_named(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for item in items:
        key = item.get("slug") or item.get("name") or ""
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _clean(value: str) -> str:
    return ILLEGAL_XML.sub("", " ".join(value.split()))
