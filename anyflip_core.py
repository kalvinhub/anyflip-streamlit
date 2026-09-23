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
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote, urlsplit

import fitz
import httpx
from PIL import Image, UnidentifiedImageError

ASSET_HOST = "https://online.anyflip.com"
BOOK_PART = re.compile(r"[a-zA-Z0-9_-]{1,80}\Z")
MAX_PAGES = max(1, min(999, int(os.getenv("ANYFLIP_MAX_PAGES", "999"))))
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_SINGLE_PDF_BYTES = 120 * 1024 * 1024
MAX_PART_PDF_BYTES = 75 * 1024 * 1024
MAX_OUTPUT_BYTES = 180 * 1024 * 1024
MAX_TOTAL_JPEG_BYTES = 165 * 1024 * 1024
MAX_SECONDS = 1800  # 30-minute app-side deadline; host may stop a job earlier.
PART_PAGES = 100
AUTO_SPLIT_ABOVE = 200
# Shared free hosting: one conversion per Python process to limit peak RAM.
DOWNLOAD_SLOTS = threading.BoundedSemaphore(value=1)
Progress = Callable[[int, int], None]


class DownloadError(Exception):
    """An actionable error safe to display in the app."""


@dataclass(frozen=True)
class PDFResult:
    filename: str
    content: bytes
    pages: int
    mime: str = "application/pdf"
    parts: int = 1


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


def normalize_page_image_url(raw_name: str, book_path: str) -> str:
    """Handle normal AnyFlip image filenames and same-book relative paths.

    Some public manifests prefix filenames with ``../files/large/`` or use
    absolute same-book URLs. Anchor *every* result to this book's ``/files/``
    directory and to online.anyflip.com; never follow arbitrary URLs.
    """
    if not isinstance(raw_name, str) or not 0 < len(raw_name) <= 600:
        raise DownloadError("The book contains an invalid page-image path.")
    raw_name = raw_name.strip().replace("\\", "/")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw_name):
        raise DownloadError("The book contains an invalid page-image path.")
    parsed = urlsplit(raw_name)
    if parsed.scheme or parsed.netloc:
        if (parsed.scheme.lower() != "https" or parsed.hostname != "online.anyflip.com"
                or parsed.username or parsed.password or parsed.port is not None):
            raise DownloadError("The book references an unsupported external image URL.")
        # Absolute URLs must already belong to the exact supplied book.
        if not parsed.path.startswith(book_path + "files/"):
            raise DownloadError("The book contains an image URL outside its book directory.")
        image_path = parsed.path[len(book_path):]
    else:
        image_path = parsed.path
        if image_path.startswith(book_path + "files/"):
            image_path = image_path[len(book_path):]
        elif image_path.startswith("/files/"):
            image_path = image_path[1:]
        elif image_path.startswith("/"):
            raise DownloadError("The book contains an image URL outside its book directory.")
        # A common public manifest format uses ../files/large/page.webp.
        # Only allow parent prefixes when followed by the book's files folder.
        if image_path.startswith("../"):
            trimmed = image_path
            while trimmed.startswith("../"):
                trimmed = trimmed[3:]
            if not trimmed.startswith("files/"):
                raise DownloadError("The book contains an invalid page-image path.")
            image_path = trimmed
        while image_path.startswith("./"):
            image_path = image_path[2:]
        if not image_path.startswith("files/"):
            image_path = f"files/large/{image_path}"

    # Decode once to support URL-encoded Unicode/spaces, then quote once.
    # Reject any remaining percent sign to avoid double-encoding ambiguity.
    image_path = unquote(image_path)
    parts = image_path.split("/")
    if (len(image_path) > 350 or len(parts) < 3 or parts[0] != "files"
            or parts[1] not in {"large", "mobile", "small"}
            or any(not part or part in {".", ".."} for part in parts)
            or any(any(ord(c) < 32 or ord(c) == 127 for c in part) for part in parts)
            or any(c in image_path for c in "\\?#%:")):
        raise DownloadError("The book contains an invalid page-image path.")
    query = parsed.query
    if len(query) > 256 or not re.fullmatch(r"[A-Za-z0-9_~.=&%+-]*", query):
        raise DownloadError("The book contains an invalid image query string.")
    encoded_path = quote(image_path, safe="/-._~")
    return f"{ASSET_HOST}{book_path}{encoded_path}" + (f"?{query}" if query else "")


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
                filename = names[0]
            elif isinstance(names, str):
                filename = names
        if not filename:
            filename = f"files/large/{index + 1}.webp"
        try:
            urls.append(normalize_page_image_url(filename, path))
        except DownloadError as exc:
            raise DownloadError(f"Page {index + 1}: {exc}") from exc
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


def create_pdf(
    book_url: str,
    progress: Progress | None = None,
    output_mode: str = "auto",
    page_start: int = 1,
    page_end: int | None = None,
) -> PDFResult:
    """Create one PDF or a ZIP of 100-page PDFs for authorized, public books.

    A 999-page book is supported at the manifest level, but actual success
    remains subject to image availability, hosting resources and output limits.
    """
    if output_mode not in {"auto", "single", "split"}:
        raise DownloadError("Choose automatic, single PDF, or split ZIP output.")
    if isinstance(page_start, bool) or not isinstance(page_start, int) or not 1 <= page_start <= MAX_PAGES:
        raise DownloadError(f"The starting page must be between 1 and {MAX_PAGES}.")
    if page_end is not None and (isinstance(page_end, bool) or not isinstance(page_end, int)
                                 or not page_start <= page_end <= MAX_PAGES):
        raise DownloadError(f"The ending page must be between {page_start} and {MAX_PAGES}.")

    book_path = parse_book_url(book_url)
    if not DOWNLOAD_SLOTS.acquire(blocking=False):
        raise DownloadError("Another download is running. Try again shortly.")
    started = time.monotonic()
    try:
        with httpx.Client(
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0", "Referer": f"{ASSET_HOST}{book_path}mobile/index.html"},
        ) as client:
            config_js = fetch_limited(client, f"{ASSET_HOST}{book_path}mobile/javascript/config.js", MAX_CONFIG_BYTES)
            title, all_urls = get_page_urls(extract_config(config_js.decode("utf-8-sig")), book_path)
            last_page = len(all_urls) if page_end is None else page_end
            if page_start > len(all_urls) or last_page > len(all_urls):
                raise DownloadError(f"This book only has {len(all_urls)} pages. Adjust the requested page range.")
            urls = all_urls[page_start - 1:last_page]
            split = output_mode == "split" or (output_mode == "auto" and len(urls) > AUTO_SPLIT_ABOVE)
            suffix = f" - pages {page_start}-{last_page}" if (page_start > 1 or last_page < len(all_urls)) else ""
            basename = title + suffix
            jpeg_total = 0
            output_total = 0
            part = 0
            pdf = fitz.open()
            try:
                with tempfile.TemporaryDirectory(prefix="anyflip_") as tmp:
                    folder = Path(tmp)
                    zip_path = folder / "output.zip"
                    archive = zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=False) if split else None
                    try:
                        for index, image_url in enumerate(urls, start=1):
                            if time.monotonic() - started > MAX_SECONDS:
                                raise DownloadError("The download exceeded the 30-minute app processing limit. Try a smaller page range.")
                            raw = fetch_limited(client, image_url, MAX_IMAGE_BYTES)
                            try:
                                with warnings.catch_warnings():
                                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                                    with Image.open(io.BytesIO(raw)) as image:
                                        image.load()
                                        width, height = image.size
                                        if (width < 1 or height < 1 or width * height > 25_000_000
                                                or max(width, height) > 10000):
                                            raise DownloadError(f"Page {page_start + index - 1} exceeds supported dimensions.")
                                        # Max 1,800-pixel edge / JPEG 76 helps big books fit on free hosting.
                                        if max(width, height) > 1800:
                                            ratio = 1800 / max(width, height)
                                            image = image.resize((max(1, round(width * ratio)),
                                                                  max(1, round(height * ratio))), Image.Resampling.LANCZOS)
                                            width, height = image.size
                                        jpeg = io.BytesIO()
                                        image.convert("RGB").save(jpeg, format="JPEG", quality=76, optimize=True)
                            except (UnidentifiedImageError, OSError, Image.DecompressionBombWarning) as exc:
                                raise DownloadError(f"Page {page_start + index - 1} is not a supported public page image.") from exc
                            jpg = jpeg.getvalue()
                            jpeg_total += len(jpg)
                            if jpeg_total > MAX_TOTAL_JPEG_BYTES:
                                raise DownloadError("The book exceeds the 165 MB image budget. Download a smaller page range.")
                            if not split and jpeg_total > MAX_SINGLE_PDF_BYTES - 8 * 1024 * 1024:
                                raise DownloadError("A single PDF would exceed the 120 MB free-host limit. Use split ZIP or a smaller page range.")
                            page = pdf.new_page(width=width * 0.72, height=height * 0.72)
                            page.insert_image(page.rect, stream=jpg)
                            if progress:
                                progress(index, len(urls))

                            if split and (index % PART_PAGES == 0 or index == len(urls)):
                                part += 1
                                part_start = page_start + index - pdf.page_count
                                part_end = page_start + index - 1
                                part_name = f"{basename} - part {part:02d} - p{part_start}-{part_end}.pdf"
                                part_path = folder / f"part_{part:03d}.pdf"
                                pdf.save(part_path, garbage=3, deflate=True)
                                pdf.close()
                                pdf = fitz.open()
                                part_size = part_path.stat().st_size
                                if part_size > MAX_PART_PDF_BYTES:
                                    raise DownloadError(f"Part {part} exceeds 75 MB. Select a smaller page range.")
                                output_total += part_size
                                if output_total > MAX_OUTPUT_BYTES:
                                    raise DownloadError("The ZIP exceeds the 180 MB free-host limit. Download smaller page ranges.")
                                archive.write(part_path, arcname=part_name)
                                part_path.unlink()
                        if split:
                            archive.close()
                            archive = None
                            if zip_path.stat().st_size > MAX_OUTPUT_BYTES:
                                raise DownloadError("The ZIP exceeds the 180 MB free-host limit. Download smaller page ranges.")
                            return PDFResult(filename=f"{basename}.zip", content=zip_path.read_bytes(),
                                             pages=len(urls), mime="application/zip", parts=part)
                        pdf_path = folder / "book.pdf"
                        pdf.save(pdf_path, garbage=3, deflate=True)
                        if pdf_path.stat().st_size > MAX_SINGLE_PDF_BYTES:
                            raise DownloadError("The PDF exceeds the 120 MB free-host limit. Use split ZIP or a smaller page range.")
                        return PDFResult(filename=f"{basename}.pdf", content=pdf_path.read_bytes(),
                                         pages=len(urls))
                    finally:
                        if archive is not None:
                            archive.close()
            finally:
                pdf.close()
    finally:
        DOWNLOAD_SLOTS.release()
