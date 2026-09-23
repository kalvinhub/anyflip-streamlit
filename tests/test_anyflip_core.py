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

@pytest.mark.parametrize(('filename', 'expected'), [
    ('abc.webp', 'files/large/abc.webp'),
    ('files/large/abc.webp', 'files/large/abc.webp'),
    ('../files/large/abc.webp', 'files/large/abc.webp'),
    ('../../files/large/abc.webp', 'files/large/abc.webp'),
    ('./files/mobile/page1.jpg', 'files/mobile/page1.jpg'),
    ('/files/large/page-1.webp', 'files/large/page-1.webp'),
    ('/user/book/files/large/page-1.webp', 'files/large/page-1.webp'),
    ('https://online.anyflip.com/user/book/files/large/abc.webp', 'files/large/abc.webp'),
    ('files/large/my%20page.webp', 'files/large/my%20page.webp'),
    ('files/large/页一.webp', 'files/large/%E9%A1%B5%E4%B8%80.webp'),
    ('files/large/abc.webp?v=2', 'files/large/abc.webp?v=2'),
])
def test_manifest_image_path_variants(filename, expected):
    result = core.normalize_page_image_url(filename, '/user/book/')
    assert result == f'https://online.anyflip.com/user/book/{expected}'


@pytest.mark.parametrize('filename', [
    '../../private.png',
    'files/large/../../private.png',
    'files/large/%252e%252e/private.webp',
    'files/large/%2e%2e/private.webp',
    'https://evil.example/a.webp',
    '//evil.example/a.webp',
    'http://online.anyflip.com/user/book/files/large/abc.webp',
    'https://online.anyflip.com/other/book/files/large/abc.webp',
    'https://online.anyflip.com/user/book/private.webp',
    '/other/book/files/large/abc.webp',
    'file:///etc/passwd',
    'files/large/a.webp\r\nInjected: header',
])
def test_rejects_off_book_or_unsafe_images(filename):
    with pytest.raises(core.DownloadError):
        core.normalize_page_image_url(filename, '/user/book/')


def test_actual_relative_manifest_produces_pdf(monkeypatch):
    image = Image.new('RGB', (140, 180), 'white')
    buf = io.BytesIO()
    image.save(buf, format='WEBP')
    expected_urls = [
        '/user/book/files/large/first.webp',
        '/user/book/files/mobile/second.webp',
    ]
    requested = []

    def handler(request):
        if request.url.path.endswith('/mobile/javascript/config.js'):
            return httpx.Response(200, text='var config = {"title":"Relative Paths",'
                                  '"pageCount":2,"fliphtml5_pages":['
                                  '{"n":["../files/large/first.webp"]},'
                                  '{"n":["./files/mobile/second.webp"]}]};')
        requested.append(request.url.path)
        if request.url.path in expected_urls:
            return httpx.Response(200, content=buf.getvalue())
        return httpx.Response(404)

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(core.httpx, 'Client', lambda **kwargs: real_client(transport=transport, **kwargs))
    result = core.create_pdf('https://online.anyflip.com/user/book/mobile/index.html')
    assert result.pages == 2
    assert requested == expected_urls
    with fitz.open(stream=result.content, filetype='pdf') as pdf:
        assert pdf.page_count == 2


def test_999_page_book_auto_splits_into_ten_pdfs(monkeypatch):
    """Verifies the page limit is functional, not only a label in the UI."""
    import zipfile

    assert core.MAX_PAGES == 999
    image = Image.new('RGB', (100, 140), 'white')
    buf = io.BytesIO()
    image.save(buf, format='WEBP')
    progress = []

    def handler(request):
        assert request.url.host == 'online.anyflip.com'
        if request.url.path.endswith('/mobile/javascript/config.js'):
            return httpx.Response(200, text='var config = {"title":"Big book","pageCount":999};')
        if request.url.path.startswith('/user/book/files/large/'):
            return httpx.Response(200, content=buf.getvalue())
        return httpx.Response(404)

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(core.httpx, 'Client', lambda **kwargs: real_client(transport=transport, **kwargs))
    result = core.create_pdf('https://online.anyflip.com/user/book/',
                             progress=lambda done, total: progress.append((done, total)))
    assert result.pages == 999
    assert result.parts == 10
    assert result.mime == 'application/zip'
    assert result.filename == 'Big book.zip'
    assert progress[-1] == (999, 999)
    assert len(progress) == 999
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        names = archive.namelist()
        assert len(names) == 10
        assert names[0].endswith('p1-100.pdf')
        assert names[-1].endswith('p901-999.pdf')
        assert sum(fitz.open(stream=archive.read(name), filetype='pdf').page_count for name in names) == 999


def test_page_range_from_999_page_book(monkeypatch):
    image = Image.new('RGB', (60, 90), 'white')
    buf = io.BytesIO()
    image.save(buf, format='WEBP')
    calls = []

    def handler(request):
        if request.url.path.endswith('/mobile/javascript/config.js'):
            return httpx.Response(200, text='var config = {"pageCount":999};')
        calls.append(request.url.path)
        return httpx.Response(200, content=buf.getvalue())

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(core.httpx, 'Client', lambda **kwargs: real_client(transport=transport, **kwargs))
    result = core.create_pdf('https://anyflip.com/user/book/', page_start=801, page_end=999)
    assert result.pages == 199
    assert result.parts == 1
    assert result.mime == 'application/pdf'
    assert 'pages 801-999' in result.filename
    assert calls[0].endswith('/801.webp')
    assert calls[-1].endswith('/999.webp')
    with fitz.open(stream=result.content, filetype='pdf') as pdf:
        assert pdf.page_count == 199


def test_rejects_manifest_with_more_than_999_pages_and_invalid_range():
    with pytest.raises(core.DownloadError, match='999'):
        core.get_page_urls({'pageCount': 1000}, '/user/book/')
    with pytest.raises(core.DownloadError):
        core.create_pdf('https://anyflip.com/user/book/', page_start=0)
    with pytest.raises(core.DownloadError):
        core.create_pdf('https://anyflip.com/user/book/', page_start=900, page_end=899)
    with pytest.raises(core.DownloadError):
        core.create_pdf('https://anyflip.com/user/book/', output_mode='magic')
