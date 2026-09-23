import io
import uuid

import fitz
import httpx
import pytest
from PIL import Image

import anyflip_core as core


def test_accepted_book_urls():
    assert core.parse_book_url("https://online.anyflip.com/user123/book456/?source=share") == "/user123/book456/"
    assert core.parse_book_url("http://www.anyflip.com/aa/bb/mobile/index.html") == "/aa/bb/"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/u/b", "https://evil.com/u/b", "https://anyflip.com.evil.com/u/b",
    "file:///etc/passwd", "https://anyflip.com/u", "https://anyflip.com:444/u/b",
    "https://user:pass@anyflip.com/u/b", "https://anyflip.com/%2e%2e/b",
])
def test_untrusted_urls_rejected(url):
    with pytest.raises(core.DownloadError):
        core.parse_book_url(url)


def test_config_parsing_and_host_pinning():
    conf = core.extract_config('var config = {"title":"My Sample","pageCount":2,"fliphtml5_pages":[{"n":["1.webp"]},{"n":["2.webp"]}]};')
    title, urls = core.get_page_urls(conf, "/user/book/")
    assert title == "My Sample"
    assert urls == [
        "https://online.anyflip.com/user/book/files/large/1.webp",
        "https://online.anyflip.com/user/book/files/large/2.webp",
    ]


def test_rejects_unsafe_config():
    with pytest.raises(core.DownloadError):
        core.get_page_urls({"pageCount": 1, "fliphtml5_pages": [{"n": ["../../private.png"]}]}, "/a/b/")
    with pytest.raises(core.DownloadError):
        core.get_page_urls({"pageCount": 1, "fliphtml5_pages": [{"n": ["https://evil.com/img"]}]}, "/a/b/")
    with pytest.raises(core.DownloadError):
        core.get_page_urls({"pageCount": core.MAX_PAGES + 1}, "/a/b/")
    with pytest.raises(core.DownloadError):
        core.extract_config("not a JSON config")


def test_download_two_page_pdf_without_external_network(monkeypatch):
    im = Image.new("RGB", (200, 300), "white")
    img_buffer = io.BytesIO()
    im.save(img_buffer, format="WEBP")
    progress = []

    def handler(req):
        assert req.url.host == "online.anyflip.com"
        if req.url.path.endswith("/mobile/javascript/config.js"):
            return httpx.Response(200, text='var config = {"title":"Test Book","pageCount":2,"fliphtml5_pages":[{"n":["first.webp"]},{"n":["second.webp"]}]};')
        if req.url.path.endswith(".webp"):
            return httpx.Response(200, content=img_buffer.getvalue())
        return httpx.Response(404)

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(core.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))
    result = core.create_pdf("https://anyflip.com/user/book/", progress=lambda done, total: progress.append((done, total)))
    pdf = fitz.open(stream=result.content, filetype="pdf")
    assert result.filename == "Test Book.pdf"
    assert result.pages == pdf.page_count == 2
    assert round(pdf[0].rect.width) == 144
    assert progress == [(1, 2), (2, 2)]
    pdf.close()


def test_redirects_are_rejected(monkeypatch):
    real_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "https://evil.com/private"}))
    monkeypatch.setattr(core.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))
    with pytest.raises(core.DownloadError, match="redirected"):
        core.create_pdf("https://anyflip.com/u/b/")


def test_oversized_download_rejected():
    real_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-length": "999999999"}))
    with real_client(transport=transport) as client:
        with pytest.raises(core.DownloadError, match="size limit"):
            core.fetch_limited(client, "https://online.anyflip.com/a/b/x.webp", 1024)
