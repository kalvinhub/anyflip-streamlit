"""Streamlit Community Cloud entrypoint: streamlit run streamlit_app.py"""
import time

import streamlit as st

from anyflip_core import DownloadError, create_pdf, parse_book_url, MAX_PAGES

st.set_page_config(page_title="AnyFlip PDF Downloader", page_icon="📘", layout="centered")
st.markdown("""
<style>
.block-container {max-width: 780px; padding-top: 2.5rem;}
.hero {background: linear-gradient(115deg, #142d54, #126d9c); border-radius: 18px;
       color: white; padding: 29px 28px; margin-bottom: 20px;}
.hero h1 {font-size: 2.0rem; font-weight: 780; margin: 0 0 7px; color: white;}
.hero p {font-size: 1rem; opacity: .9; margin: 0;}
.small-note {color: #64748b; font-size: .87rem;}
</style>
<div class="hero"><h1>📘 AnyFlip PDF Downloader</h1>
<p>Paste a public AnyFlip book link and save an authorized copy as a PDF.</p></div>
""", unsafe_allow_html=True)

with st.form("download_form", clear_on_submit=False):
    url = st.text_input("AnyFlip book URL", placeholder="https://online.anyflip.com/username/bookid/", max_chars=2048)
    permission = st.checkbox("I own this book or have permission to download it.")
    submitted = st.form_submit_button("Create PDF", type="primary", use_container_width=True)

if submitted:
    if not url.strip():
        st.error("Paste an AnyFlip book link first.")
    elif not permission:
        st.error("Confirm that you're authorized to download this book.")
    elif time.monotonic() - st.session_state.get("last_attempt", 0) < 12:
        st.warning("Please leave 12 seconds between download requests.")
    else:
        try:
            # Fail fast on invalid input before using a public server's CPU.
            parse_book_url(url)
            st.session_state["last_attempt"] = time.monotonic()
            st.session_state.pop("pdf", None)
            progress_bar = st.progress(0, text="Reading book details…")
            with st.status("Creating your PDF…", expanded=True) as status:
                def report(done: int, total: int) -> None:
                    progress_bar.progress(done / total, text=f"Processed {done} of {total} pages")
                pdf = create_pdf(url, progress=report)
                status.update(label=f"Completed {pdf.pages} pages", state="complete", expanded=False)
            st.session_state["pdf"] = pdf
            st.success("Your PDF is ready.")
        except DownloadError as exc:
            st.error(str(exc))
        except Exception:
            # Keep filesystem paths and internals out of the public UI.
            st.error("An unexpected error occurred. Check the server logs or try another authorized book.")

if "pdf" in st.session_state:
    pdf = st.session_state["pdf"]
    st.download_button(
        label=f"⬇️ Download PDF ({pdf.pages} pages)",
        data=pdf.content,
        file_name=pdf.filename,
        mime="application/pdf",
        type="primary",
        use_container_width=True,
        on_click="ignore",
    )
    if st.button("Clear generated PDF"):
        del st.session_state["pdf"]
        st.rerun()

st.divider()
st.markdown(f"<p class='small-note'>Supports up to {MAX_PAGES} pages per book. Public, accessible AnyFlip books only. The PDF contains page images and may not retain selectable text or hyperlinks. Downloads are temporarily kept in your active browser session.</p>", unsafe_allow_html=True)
with st.expander("Supported books and limitations"):
    st.write("This tool reads public AnyFlip page images and assembles them into a PDF. It does not bypass sign-in, permissions, passwords, or DRM. Some AnyFlip books use different configurations and may not be compatible.")
    st.write("This free-hosting version limits page count, PDF size, processing time and concurrent jobs. For high public traffic, use a dedicated backend with per-IP rate limiting and a job queue.")
