# Content notice

This repository holds two kinds of material. The MIT License covers the software. It does not cover the blog.

## Software

The Python scripts, the GitHub Actions workflow, and the original documentation in this repository are licensed under the [MIT License](LICENSE). Copyright for that software is held by n0vad3v.

## Blog posts and images

Text, images, and other media fetched from [blog.cloudflare.com](https://blog.cloudflare.com/) remain the property of Cloudflare, Inc. and the posts' authors. This project does not grant a license to that material.

`sync.py` saves copies under `data/`. `build.py` packs those copies into an EPUB. The book is a personal offline archive:

- The cover and the EPUB metadata state that post text and images are copyright Cloudflare.
- Each chapter keeps its date, authors, tags, and a link to the original URL.
- The book is for personal offline reading.

Running these tools, or downloading a generated EPUB from a GitHub Release, does not transfer copyright in the posts or images. Do not republish the posts, images, or the finished book as your own work. The MIT License is not permission to do so.

Screenshots in this repository show pages of that book and include Cloudflare's content.

## Trademarks

Cloudflare, the Cloudflare logo, and related marks are trademarks of Cloudflare, Inc. `docs/logos/cloudflare.svg` is used to identify the source of the archive. This project is not affiliated with, endorsed by, or sponsored by Cloudflare.

## Generated files

These paths are not part of the MIT-licensed source. They contain copyrighted blog content, or they are produced from it:

- `data/`
- `build/`
- `*.epub`
- Release assets such as `cloudflare-blog-kindle.epub`

`data/`, `build/`, and `*.epub` are gitignored. Release assets are built for personal offline reading of the English posts.
