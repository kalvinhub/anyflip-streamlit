# AnyFlip Streamlit Downloader — 999-page edition

Browser-based image-to-PDF conversion for **public AnyFlip books you own or have permission to save**. It does not run the original Windows `.exe` and does not bypass access restrictions or DRM.

## What's new

- Accepts public books containing **up to 999 pages** instead of 120.
- **Automatic** output: a single PDF for up to 200 selected pages; for larger selections, a ZIP containing PDFs split into **100-page parts** (e.g. a 999-page book becomes 10 PDF files in one ZIP).
- Optional *one PDF* and *split ZIP* modes. One PDF may consume more memory.
- Optional page-range field, e.g. `201-400`, for large books that exceed free-hosting limits.
- Improved memory management: build one ZIP part at a time, compress page images to a 1,800-pixel maximum edge with quality 76, and reject overly large jobs.
- Preserves previous fixes for `../files/large/...` relative page-image paths and blocks cross-host or outside-book URLs.

**999 pages is a supported upper bound, not a guarantee of successful conversion on free Streamlit hosting.** Network reliability, image sizes, unsupported manifest variations, CPU/RAM pressure and the provider's own limits can prevent full-book completion. This edition enforces a 30-minute app-side deadline, one conversion at a time, 120 MB for a single PDF, 180 MB for a ZIP, 12 MB per image and 165 MB total converted JPEG images. ZIP parts have a 75 MB per-part cap. If a book is too large, use the page-range field, or deploy a dedicated backend with a job queue and persistent storage. Public Streamlit hosting and its download widget may hold sizeable results in RAM.

## Update your existing Streamlit app

1. Extract this archive and replace **`anyflip_core.py`**, **`streamlit_app.py`**, **`README.md`** and **`tests/test_anyflip_core.py`** in the root of your existing GitHub repository. (You can also upload the entire archive's contents.) Do **not** upload the original Windows `.exe` or just the ZIP.
2. Commit to your deployment branch, typically `main`.
3. Streamlit normally redeploys automatically; if needed, reboot/redeploy the app in Streamlit Community Cloud.
4. Test a small public book that you have permission to save before trying a large book.

## Deploy new

1. Create a GitHub repository and upload the **contents** of this archive into its root.
2. Go to https://share.streamlit.io/ and connect your GitHub account.
3. Choose your repo and branch, set main file to `streamlit_app.py`, and select a supported Python version (3.12 is suitable).
4. Deploy and share your `*.streamlit.app` URL.

GitHub Pages alone cannot run this Python backend; the code lives on GitHub and executes on Streamlit.

## Local testing

```bash
python -m pip install -r requirements.txt pytest
python -m pytest -q
streamlit run streamlit_app.py
```

## Security and availability

- Requests are pinned to `online.anyflip.com`, with no cross-host redirects and a bounded allowed page-image directory.
- Only publicly available page images and supported public manifests are processed; authorization/DRM bypass is unsupported.
- Resource limits prevent unbounded page sizes and response sizes. A 12-second per-session cooldown is not robust per-IP rate limiting.
- Free shared Streamlit hosting is appropriate for low-traffic experiments. For a public site with sustained traffic, add server-side IP rate limiting, a job queue and object storage.
- Generated documents use page images; searchable/selectable text and hyperlinks are generally not retained.
- Tests mock network responses. Live AnyFlip download of a 999-page book and successful completion on Streamlit Community Cloud have **not** been verified.
