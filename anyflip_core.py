"""AnyFlip image-to-PDF core for books the user is allowed to save.

Requests are restricted to an exact AnyFlip host; no authentication, DRM
circumvention, arbitrary remote URL fetching, or cross-host redirects.
"""
from __future__ import annotations

import io
import json
import os
import re
import tempfile
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import fitz
import httpx
from PIL import Image, UnidentifiedImageError

ASSET_HOST = "https://online.anyflip.com"
BOOK_PART = re.compile(r"[a-zA-Z0-9_-]{1,80}\Z")
SAFE_IMAGE_PATH = re.compile(r"[a-zA-Z0-9_./-]{1,250}\Z")
MAX_PAGES = max(1, min(300, int(os.getenv("ANYFLIP_MAX_PAGES", "120"))))
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_PDF_BYTES = 90 * 1024 * 1024
MAX_TOTAL_JPEG_BYTES = 90 * 1024 * 1024
MAX_SECONDS = 300
# One-process overload protection; Streamlit Cloud isn't a job queue.
DOWNLOAD_SLOTS = threading.BoundedSemaphore(value=2)
Progress = Callable[[int, int], None]


class DownloadError(Exception):
    """An actionable error safe to display in the app."""


@dataclass(frozen=True)
class PDFResult:
    filename: str
    content: bytes
    pages: int


def parse_book_url(raw: str) -> str:
    if not isinstance(raw, str) or len(raw) > 2048:
        raise DownloadError("Enter a valid AnyFlip book URL.")
    url = urlsplit(raw.strip())
    if (
        url.scheme not in {"https", "http"}
        or (url.hostname or "").lower() not in {"anyflip.com", "www.anyflip.com", "online.anyflip.com"}
        or url.username or url.password or url.port is not None
    ):
        raise DownloadError("Use a normal AnyFlip book URL (not another website).")
    parts = [part for part in url.path.split("/") if part]
    if len(parts) < 2 or not BOOK_PART.fullmatch(parts[0]) or not BOOK_PART.fullmatch(parts[1]):
        raise DownloadError("The link must contain a valid AnyFlip user ID and book ID.")
    # The pasted host/path never controls the outgoing host.
    return f"/{parts[0]}/{parts[1]}/"


def extract_config(script: str) -> dict:
    first, last = script.find("{"), script.rfind("}")
    if first == -1 or last <= first:
        raise DownloadError("This book does not expose a supported public configuration.")
    try:
        config = json.loads(script[first:last + 1])
    except (ValueError, TypeError) as exc:
        raise DownloadError("The book uses an unsupported AnyFlip configuration format.") from exc
    if not isinstance(config, dict):
        raise DownloadError("Invalid book configuration.")
    return config


def get_page_urls(config: dict, path: str) -> tuple[str, list[str]]:
    metadata = config.get("meta") if isinstance(config.get("meta"), dict) else {}
    book_config = config.get("bookConfig") if isinstance(config.get("bookConfig"), dict) else {}
    raw_title = config.get("title") or metadata.get("title") or book_config.get("bookTitle") or "AnyFlip Book"
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(raw_title)).strip(" .")[:80] or "AnyFlip Book"
    pages = config.get("fliphtml5_pages") or []
    if not isinstance(pages, list):
        pages = []
    count = (config.get("totalPageCount") or config.get("pageCount")
             or metadata.get("pageCount") or metadata.get("totalPageCount")
             or book_config.get("pageCount") or book_config.get("totalPageCount") or len(pages))
    try:
        count = int(count)
    except (ValueError, TypeError) as exc:
        raise DownloadError("Cannot determine the book's page count.") from exc
    if not 1 <= count <= MAX_PAGES:
        raise DownloadError(f"This app supports books containing 1–{MAX_PAGES} pages.")
    urls = []
    for index in range(count):
        filename = ""
        if index < len(pages) and isinstance(pages[index], dict):
            names = pages[index].get("n") or []
            if isinstance(names, list) and names and isinstance(names[0], str):
                filename = names[0].lstrip("/")
        if not filename:
            filename = f"files/large/{index + 1}.webp"
        elif not filename.startswith("files/"):
            filename = f"files/large/{filename}"
        if ".." in filename or not SAFE_IMAGE_PATH.fullmatch(filename):
            raise DownloadError("The book contains an invalid page-image path.")
        urls.append(f"{ASSET_HOST}{path}{filename}")
    return title, urls


def fetch_limited(client: httpx.Client, url: str, max_bytes: int) -> bytes:
    try:
        with client.stream("GET", url, follow_redirects=False) as response:
            if response.is_redirect:
                raise DownloadError("AnyFlip redirected a file request. This book is not supported.")
            if response.status_code != 200:
                raise DownloadError(f"AnyFlip returned HTTP {response.status_code}; the book may be unavailable or restricted.")
            length = response.headers.get("content-length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise DownloadError("A requested page exceeds the file-size limit.")
            content = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise DownloadError("A requested page exceeds the file-size limit.")
            return bytes(content)
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise DownloadError("Could not reach AnyFlip. Try again later.") from exc


def create_pdf(book_url: str, progress: Progress | None = None) -> PDFResult:
    """Download public page images and return a PDF; temporarily uses disk, not a long-lived job store."""
    book_path = parse_book_url(book_url)
    if not DOWNLOAD_SLOTS.acquire(blocking=False):
        raise DownloadError("Two downloads are already running. Try again shortly.")
    started = time.monotonic()
    try:
        with httpx.Client(
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0", "Referer": f"{ASSET_HOST}{book_path}mobile/index.html"},
        ) as client:
            config_js = fetch_limited(client, f"{ASSET_HOST}{book_path}mobile/javascript/config.js", MAX_CONFIG_BYTES)
            title, urls = get_page_urls(extract_config(config_js.decode("utf-8-sig")), book_path)
            pdf = fitz.open()
            jpeg_size = 0
            try:
                for index, image_url in enumerate(urls, start=1):
                    if time.monotonic() - started > MAX_SECONDS:
                        raise DownloadError("This download exceeded the five-minute processing limit.")
                    raw = fetch_limited(client, image_url, MAX_IMAGE_BYTES)
                    try:
                        with warnings.catch_warnings():
                            warnings.simplefilter("error", Image.DecompressionBombWarning)
                            with Image.open(io.BytesIO(raw)) as image:
                                image.load()
                                width, height = image.size
                                if width < 1 or height < 1 or width * height > 25_000_000 or max(width, height) > 10000:
                                    raise DownloadError(f"Page {index} exceeds the supported image dimensions.")
                                jpeg = io.BytesIO()
                                image.convert("RGB").save(jpeg, format="JPEG", quality=84, optimize=True)
                    except (UnidentifiedImageError, OSError, Image.DecompressionBombWarning) as exc:
                        raise DownloadError(f"Page {index} is not a supported public page image.") from exc
                    jpg = jpeg.getvalue()
                    jpeg_size += len(jpg)
                    if jpeg_size > MAX_TOTAL_JPEG_BYTES:
                        raise DownloadError("The finished document is too large for free hosting.")
                    page = pdf.new_page(width=width * 0.72, height=height * 0.72)
                    page.insert_image(page.rect, stream=jpg)
                    if progress:
                        progress(index, len(urls))
                with tempfile.TemporaryDirectory(prefix="anyflip_") as tmp:
                    filename = Path(tmp) / "book.pdf"
                    pdf.save(filename, garbage=3, deflate=True)
                    if filename.stat().st_size > MAX_PDF_BYTES:
                        raise DownloadError("The PDF exceeds the 90 MB download limit.")
                    result = PDFResult(filename=f"{title}.pdf", content=filename.read_bytes(), pages=len(urls))
                return result
            finally:
                pdf.close()
    finally:
        DOWNLOAD_SLOTS.release()
