"""Streamlit Community Cloud entrypoint: streamlit run streamlit_app.py"""
import re
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
<p>Save an authorized copy of a public AnyFlip book. Supports up to 999 pages.</p></div>
""", unsafe_allow_html=True)

OUTPUTS = {
    "Automatic — PDF up to 200 pages, ZIP for larger books": "auto",
    "One PDF — up to 120 MB": "single",
    "Split ZIP — a PDF every 100 pages (recommended for large books)": "split",
}
with st.form("download_form", clear_on_submit=False):
    url = st.text_input("AnyFlip book URL", placeholder="https://online.anyflip.com/username/bookid/", max_chars=2048)
    output_label = st.selectbox("Output format", list(OUTPUTS), index=0)
    page_range = st.text_input("Page range (optional)", placeholder="Leave blank for all pages, or enter 201-400", max_chars=7)
    st.caption("For large books, download smaller ranges if you hit hosting or file-size limits.")
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
        start, end = 1, None
        match = re.fullmatch(r"\s*(\d{1,3})\s*-\s*(\d{1,3})\s*", page_range) if page_range.strip() else None
        if page_range.strip() and not match:
            st.error("Enter the page range as start-end, for example 201-400.")
        elif match and not (1 <= int(match.group(1)) <= int(match.group(2)) <= MAX_PAGES):
            st.error(f"Choose a page range between 1 and {MAX_PAGES}.")
        else:
            if match:
                start, end = int(match.group(1)), int(match.group(2))
            status = None
            try:
                parse_book_url(url)
                st.session_state["last_attempt"] = time.monotonic()
                st.session_state.pop("pdf", None)
                progress_bar = st.progress(0, text="Reading book details…")
                with st.status("Creating your document…", expanded=True) as status:
                    def report(done: int, total: int) -> None:
                        # Avoid sending 999 separate progress updates to the browser.
                        if done == total or done == 1 or done % max(1, total // 100) == 0:
                            progress_bar.progress(done / total, text=f"Processed {done} of {total} pages")
                    result = create_pdf(url, progress=report, output_mode=OUTPUTS[output_label],
                                        page_start=start, page_end=end)
                    status.update(label=f"Completed {result.pages} pages", state="complete", expanded=False)
                st.session_state["pdf"] = result
                st.success(f"Ready: {result.pages} pages, {result.parts} file(s), {len(result.content) / (1024**2):.1f} MB")
            except DownloadError as exc:
                if status is not None:
                    status.update(label="Could not generate PDF", state="error", expanded=False)
                st.error(str(exc))
            except Exception:
                if status is not None:
                    status.update(label="PDF generation failed", state="error", expanded=False)
                st.error("An unexpected error occurred. Check the server logs or try a smaller page range.")

if "pdf" in st.session_state:
    result = st.session_state["pdf"]
    label = f"⬇️ Download {'ZIP of PDFs' if result.mime == 'application/zip' else 'PDF'} ({result.pages} pages)"
    st.download_button(
        label=label,
        data=result.content,
        file_name=result.filename,
        mime=result.mime,
        type="primary",
        use_container_width=True,
        on_click="ignore",
    )
    if st.button("Clear generated download"):
        del st.session_state["pdf"]
        st.rerun()

st.divider()
st.markdown(f"<p class='small-note'>Accepts books up to {MAX_PAGES} pages. "
            "Large books automatically become ZIP files of 100-page PDFs. "
            "Free hosting limits total output to 180 MB and processing to 30 minutes; "
            "Streamlit may stop resource-intensive jobs earlier. "
            "Use a smaller page range if needed. Public, accessible AnyFlip books only. "
            "Page-image PDFs may not retain selectable text or hyperlinks.</p>", unsafe_allow_html=True)
with st.expander("Supported books and limitations"):
    st.write("Only download books you own or have permission to save. "
             "This tool reads public AnyFlip page images; it does not bypass passwords, "
             "sign-in restrictions, copy protection or DRM.")
    st.write("Maximums: 999 pages per book, one PDF up to 120 MB, split ZIP up to 180 MB, "
             "30-minute app-side deadline and one job per Python process. "
             "The hosting provider can enforce stricter memory, CPU and time limits. "
             "Some public AnyFlip formats and page URLs may not be compatible.")
    st.write("Large outputs are temporarily stored in your Streamlit session memory. "
             "For frequent 999-page downloads or many simultaneous visitors, use a dedicated "
             "background worker with disk/object storage instead of free shared hosting.")
