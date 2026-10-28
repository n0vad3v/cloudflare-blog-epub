"""Directories next to these scripts."""

from __future__ import annotations

from pathlib import Path


def root() -> Path:
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    return root() / "data"


def raw_dir() -> Path:
    return data_dir() / "raw"


def posts_dir() -> Path:
    return data_dir() / "posts"


def images_dir() -> Path:
    return data_dir() / "images"


def kindle_images_dir(edge: int, quality: int) -> Path:
    return data_dir() / "images-kindle" / f"e{edge}-q{quality}"


def catalog_path() -> Path:
    return data_dir() / "catalog.json"


def image_manifest_path() -> Path:
    return data_dir() / "image-manifest.json"


def build_dir() -> Path:
    return root() / "build"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
