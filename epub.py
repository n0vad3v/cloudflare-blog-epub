"""Pack an OEBPS tree into an EPUB and check its internal links."""

from __future__ import annotations

import posixpath
import zipfile
from pathlib import Path
from urllib.parse import unquote

from lxml import etree
from tqdm import tqdm

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def write_container(book_dir: Path) -> None:
    meta = book_dir / "META-INF"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "container.xml").write_text(CONTAINER, encoding="utf-8")
    (book_dir / "mimetype").write_bytes(b"application/epub+zip")


def pack(book_dir: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        info = zipfile.ZipInfo("mimetype", date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_STORED
        info.external_attr = 0o644 << 16
        archive.writestr(info, b"application/epub+zip")
        files = [path for path in book_dir.rglob("*") if path.is_file() and path.name != "mimetype"]
        for path in tqdm(sorted(files), desc="pack", unit="file", disable=None):
            archive.write(path, path.relative_to(book_dir).as_posix())


def validate_epub(path: Path) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if not infos or infos[0].filename != "mimetype":
            errors.append("mimetype is not the first zip entry")
        elif infos[0].compress_type != zipfile.ZIP_STORED:
            errors.append("mimetype is compressed")
        elif archive.read("mimetype") != b"application/epub+zip":
            errors.append("mimetype contents are wrong")
        names = set(archive.namelist())
        if "META-INF/container.xml" not in names:
            errors.append("missing META-INF/container.xml")
            return errors
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
        container = etree.fromstring(archive.read("META-INF/container.xml"), parser)
        rootfiles = container.xpath("//*[local-name()='rootfile']/@full-path")
        if not rootfiles:
            errors.append("container.xml has no rootfile")
            return errors
        opf_path = rootfiles[0]
        if opf_path not in names:
            errors.append(f"missing package document {opf_path}")
            return errors
        package = etree.fromstring(archive.read(opf_path), parser)
        base = posixpath.dirname(opf_path)
        manifest: dict[str, str] = {}
        nav_count = 0
        for item in package.xpath("//*[local-name()='item']"):
            href = item.get("href") or ""
            item_id = item.get("id") or ""
            full = posixpath.normpath(posixpath.join(base, unquote(href.split("#", 1)[0])))
            if full not in names:
                errors.append(f"manifest item {item_id} is missing: {full}")
            manifest[item_id] = full
            if "nav" in (item.get("properties") or "").split():
                nav_count += 1
        if nav_count != 1:
            errors.append(f"expected one nav document, found {nav_count}")
        spine = package.xpath("//*[local-name()='itemref']/@idref")
        if not spine:
            errors.append("spine is empty")
        for item_id in spine:
            if item_id not in manifest:
                errors.append(f"spine references unknown id {item_id}")
        seen: set[str] = set()
        for item_id, full in manifest.items():
            if item_id in seen:
                errors.append(f"duplicate manifest id {item_id}")
            seen.add(item_id)
            if full not in names or not full.endswith((".xhtml", ".html", ".ncx", ".opf")):
                continue
            try:
                document = etree.fromstring(archive.read(full), parser)
            except etree.XMLSyntaxError as exc:
                errors.append(f"{full} is not well-formed XML: {exc}")
                continue
            if not full.endswith((".xhtml", ".html")):
                continue
            for href in document.xpath(
                "//*[local-name()='a']/@href | //*[local-name()='img']/@src | //*[local-name()='link']/@href"
            ):
                if not href or href.startswith(("http://", "https://", "mailto:", "data:", "#")):
                    continue
                target = unquote(href.split("#", 1)[0])
                if not target:
                    continue
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(full), target))
                if resolved not in names or resolved.startswith(".."):
                    errors.append(f"broken link in {full}: {href}")
    return errors
