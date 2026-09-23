# AnyFlip Streamlit Downloader

A browser-based Python image-to-PDF converter for public AnyFlip books that you **own or have permission to save**. This is a reimplementation of the download workflow; the Windows `.exe` is **not** run on the server.

## Deploy on Streamlit Community Cloud (recommended)

1. Create a new **GitHub repository** (e.g. `anyflip-streamlit`). Unzip this archive and put its **contents** into the repository root. Do **not** upload the zip itself or the original `.exe`.
2. Go to **https://share.streamlit.io/** and sign in with GitHub. Choose **Create app → Yup, I have an app**.
3. Select your repository, `main` branch and **Main file path** `streamlit_app.py`. Select **Python 3.12** in Advanced settings, if offered, and choose an available `*.streamlit.app` URL.
4. Click **Deploy**. Share the public URL. Changes pushed to GitHub are redeployed automatically.

No environment variables or API keys are required. For a private repository, ensure Streamlit has repository access; choose your app's visibility based on the audience.

## Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run streamlit_app.py
```

Open `http://localhost:8501`. To run tests: `python -m pip install pytest && python -m pytest -q`.

## Why not GitHub Pages?

GitHub Pages hosts static files only and **cannot run Python**. The GitHub repository holds your code; **Streamlit Community Cloud runs your Python app** and supplies the public website.

## Safety, limits and caveats

- Outgoing requests are pinned to `https://online.anyflip.com`. The input URL accepts only AnyFlip user/book paths; redirects, arbitrary hosts and suspicious page filenames are rejected.
- Supports public compatible `mobile/javascript/config.js` books and public page images. Does **not** bypass passwords, sign-in restrictions, copy protection or DRM. Only download books for which you have the rights.
- Shared free hosting is for light use: default **120-page maximum**, **90 MB PDF maximum**, **5-minute processing timeout** and **2 concurrent downloads per Python process**. The app also has a 12-second *per-browser-session* cooldown, which is not robust abuse protection. For a popular public site add platform-level per-IP rate limiting, authentication, a proper job queue and object storage, or move to a dedicated backend.
- Finished PDFs are held in the browser's server-side Streamlit session memory for download. Session expiry or app restart removes them. Session state and download widgets can consume RAM; avoid large traffic on a free plan.
- Page images are re-encoded to JPEG, so generated PDFs may not retain searchable text and links.
- Automated tests mock AnyFlip responses. **The live AnyFlip download workflow has not been verified** on a real book during creation of this project. Check a book you own after deploying.
