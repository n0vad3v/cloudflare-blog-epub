"""Download blog media and resize it so one EPUB stays readable offline."""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager

from PIL import Image, ImageOps, UnidentifiedImageError
from tqdm import tqdm

from article import media_url
from fetch import client, get
from paths import atomic_write_bytes, atomic_write_text, image_manifest_path, images_dir, kindle_images_dir

MAX_EDGE = 1400
MAX_BYTES = 40_000_000
Image.MAX_IMAGE_PIXELS = 120_000_000
_MANIFEST_LOCK = threading.Lock()
_EXECUTOR: ThreadPoolExecutor | None = None
_WORKERS = 4


@contextmanager
def download_pool(workers: int) -> Iterator[None]:
    """Share one image-download pool for the duration of a sync."""
    global _EXECUTOR, _WORKERS
    if workers < 1:
        raise ValueError("concurrency must be at least 1")
    executor = ThreadPoolExecutor(max_workers=workers)
    previous_executor = _EXECUTOR
    previous_workers = _WORKERS
    _EXECUTOR = executor
    _WORKERS = workers
    try:
        yield
    finally:
        _EXECUTOR = previous_executor
        _WORKERS = previous_workers
        executor.shutdown(wait=True)


def cached_images(names: list[str]) -> dict[str, dict]:
    """Return manifest entries that already exist on disk. Does not download."""
    manifest = _load_manifest()
    found = {}
    for name in dict.fromkeys(names):
        entry = manifest.get(name)
        if entry and (images_dir() / entry["file"]).is_file():
            found[name] = entry
    return found


def ensure_images(names: list[str], *, progress: tqdm | None = None) -> dict[str, dict]:
    """Download any missing media and return the entries now on disk.

    Files are cached under data/images. A name missing from the result failed
    to download. When progress is set, it advances once per requested name.
    """
    unique = list(dict.fromkeys(names))
    found: dict[str, dict] = {}
    needed: list[str] = []
    with _MANIFEST_LOCK:
        manifest = _load_manifest()
        for name in unique:
            entry = manifest.get(name)
            if entry and (images_dir() / entry["file"]).is_file():
                found[name] = entry
                if progress is not None:
                    progress.update(1)
            else:
                needed.append(name)
    if not needed:
        return found

    pool = _EXECUTOR
    own_pool = pool is None
    if pool is None:
        pool = ThreadPoolExecutor(max_workers=_WORKERS)
    new_entries: dict[str, dict] = {}
    try:
        futures = {pool.submit(_fetch_image, name): name for name in needed}
        for future in as_completed(futures):
            name = futures[future]
            try:
                entry = future.result()
            except Exception as exc:
                tqdm.write(f"image failed {name}: {exc}")
                entry = None
            if entry:
                new_entries[name] = entry
            if progress is not None:
                progress.update(1)
    finally:
        if own_pool:
            pool.shutdown(wait=True)

    with _MANIFEST_LOCK:
        manifest = _load_manifest()
        manifest.update(new_entries)
        if new_entries:
            _save_manifest(manifest)
        for name in unique:
            entry = manifest.get(name)
            if entry and (images_dir() / entry["file"]).is_file():
                found[name] = entry
    return found


def _load_manifest() -> dict[str, dict]:
    path = image_manifest_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_manifest(manifest: dict[str, dict]) -> None:
    atomic_write_text(image_manifest_path(), json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))


def _fetch_image(name: str) -> dict | None:
    with client() as http:
        response = get(http, media_url(name))
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type or "application/json" in content_type:
        raise RuntimeError(f"unexpected content-type {content_type}")
    data = response.content
    if len(data) > MAX_BYTES:
        raise RuntimeError(f"image is {len(data)} bytes")
    blob, stored, media_type, width, height = _encode(name, data)
    atomic_write_bytes(images_dir() / stored, blob)
    return {
        "file": stored,
        "media_type": media_type,
        "width": width,
        "height": height,
    }


def _encode(name: str, data: bytes) -> tuple[bytes, str, str, int, int]:
    """Return png/jpeg/gif/svg bytes, filename, media type, width, height."""
    lower = name.lower()
    if lower.endswith(".svg") or data.lstrip().startswith(b"<svg") or data.lstrip().startswith(b"<?xml"):
        return data, _with_ext(name, ".svg"), "image/svg+xml", 0, 0

    try:
        image = Image.open(io.BytesIO(data))
    except UnidentifiedImageError:
        raise RuntimeError("unrecognized image") from None

    frames = getattr(image, "n_frames", 1) or 1
    if image.format == "GIF" and frames > 1:
        width, height = image.size
        return data, _with_ext(name, ".gif"), "image/gif", width, height

    image = ImageOps.exif_transpose(image) or image
    image.load()
    has_alpha = image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info)
    if image.mode == "P":
        image = image.convert("RGBA" if has_alpha else "RGB")
    elif image.mode == "CMYK":
        image = image.convert("RGB")
    elif has_alpha and image.mode != "RGBA":
        image = image.convert("RGBA")
    elif not has_alpha and image.mode != "RGB":
        image = image.convert("RGB")

    image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
    width, height = image.size
    source_ext = _extension(name)
    use_png = has_alpha or source_ext in {".png", ".gif"}
    buffer = io.BytesIO()
    if use_png:
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue(), _with_ext(name, ".png"), "image/png", width, height
    image.save(buffer, format="JPEG", quality=82, optimize=True)
    return buffer.getvalue(), _with_ext(name, ".jpg"), "image/jpeg", width, height


def _extension(name: str) -> str:
    lower = name.lower()
    for ext in (".jpeg", ".jpg", ".png", ".gif", ".webp", ".svg", ".avif"):
        if lower.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ""


def _with_ext(name: str, ext: str) -> str:
    current = _extension(name)
    stem = name[: -len(current)] if current else name
    return stem + ext


KINDLE_EDGE = 1200
KINDLE_QUALITY = 70
_KINDLE_ENCODER = 1
_KINDLE_WORKERS = 4


def prepare_kindle_images(
    entries: dict[str, dict],
    *,
    edge: int = KINDLE_EDGE,
    quality: int = KINDLE_QUALITY,
) -> dict[str, dict]:
    """Write a Kindle-sized copy of each image and return those cache entries.

    Files already in data/images are only read. Compressed copies go under
    data/images-kindle/e{edge}-q{quality}/. Kindle AZW3 accepts JPEG, PNG, GIF,
    BMP, and SVG; WebP and AVIF are not used.
    """
    if edge < 1:
        raise ValueError("image edge must be at least 1")
    if not 1 <= quality <= 95:
        raise ValueError("jpeg quality must be from 1 to 95")

    directory = kindle_images_dir(edge, quality)
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    manifest = _load_json_dict(manifest_path)
    ready: dict[str, dict] = {}
    needed: list[str] = []
    for name, entry in entries.items():
        source = images_dir() / entry["file"]
        if not source.is_file():
            continue
        stat = source.stat()
        cached = manifest.get(name)
        target_name = cached.get("file") if isinstance(cached, dict) else None
        target = directory / target_name if target_name else None
        if (
            isinstance(cached, dict)
            and cached.get("encoder") == _KINDLE_ENCODER
            and cached.get("source_size") == stat.st_size
            and cached.get("source_mtime_ns") == str(stat.st_mtime_ns)
            and target is not None
            and target.is_file()
        ):
            ready[name] = cached
            continue
        needed.append(name)

    if ready or needed:
        with tqdm(total=len(ready) + len(needed), desc="compress", unit="img", disable=None) as bar:
            bar.update(len(ready))
            if not needed:
                return ready
            with ThreadPoolExecutor(max_workers=_KINDLE_WORKERS) as pool:
                futures = {
                    pool.submit(_kindle_image, name, entries[name], directory, edge, quality): name
                    for name in needed
                }
                pending_save = 0
                for future in as_completed(futures):
                    name = futures[future]
                    try:
                        record = future.result()
                    except Exception as exc:
                        tqdm.write(f"image compress failed {name}: {exc}")
                        record = None
                    if record:
                        manifest[name] = record
                        ready[name] = record
                        pending_save += 1
                        if pending_save >= 25:
                            atomic_write_text(manifest_path, _manifest_text(manifest))
                            pending_save = 0
                    bar.update(1)
            if pending_save:
                atomic_write_text(manifest_path, _manifest_text(manifest))
    return ready


def _kindle_image(name: str, entry: dict, directory: Path, edge: int, quality: int) -> dict:
    source = images_dir() / entry["file"]
    stat = source.stat()
    blob, stored, media_type, width, height = _kindle_encode(source, entry, edge, quality)
    atomic_write_bytes(directory / stored, blob)
    return {
        "encoder": _KINDLE_ENCODER,
        "source_file": entry["file"],
        "source_size": stat.st_size,
        "source_mtime_ns": str(stat.st_mtime_ns),
        "file": stored,
        "media_type": media_type,
        "width": width,
        "height": height,
    }


def _kindle_encode(source: Path, entry: dict, edge: int, quality: int) -> tuple[bytes, str, str, int, int]:
    media_type = str(entry.get("media_type") or "")
    if media_type == "image/svg+xml" or source.suffix.lower() == ".svg":
        return source.read_bytes(), source.name, "image/svg+xml", int(entry.get("width") or 0), int(entry.get("height") or 0)

    with Image.open(source) as image:
        frames = getattr(image, "n_frames", 1) or 1
        animated = image.format == "GIF" and frames > 1
        if animated:
            image.seek(0)
        converted = _flatten_rgb(image)
        converted.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        width, height = converted.size
        buffer = io.BytesIO()
        converted.save(buffer, format="JPEG", quality=quality, optimize=True, subsampling=0)
        blob = buffer.getvalue()
    if not animated and len(blob) >= source.stat().st_size:
        return (
            source.read_bytes(),
            source.name,
            media_type or "application/octet-stream",
            int(entry.get("width") or width),
            int(entry.get("height") or height),
        )
    return blob, f"{source.stem}.jpg", "image/jpeg", width, height


def _flatten_rgb(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image) or image
    image.load()
    if image.mode == "P":
        image = image.convert("RGBA" if "transparency" in image.info else "RGB")
    if image.mode in {"RGBA", "LA"}:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def _load_json_dict(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _manifest_text(manifest: dict) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
