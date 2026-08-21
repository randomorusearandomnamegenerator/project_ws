from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import streamlit as st

from app import (
    PDF_SOURCES,
    build_results,
    extract_text_lines,
    format_rows,
    now_sg,
    parse_bus_pdf,
    parse_demerit_pdf,
    parse_swo_pdf,
    parse_uens,
    parse_updated_on,
    resolve_pdf_url,
    safe_filename,
)


def fetch_pdf(source_key: str) -> Dict[str, Any]:
    meta = PDF_SOURCES[source_key]
    resolved_url, content = resolve_pdf_url(meta["url"])
    retrieved_at = now_sg().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "key": source_key,
        "label": meta["label"],
        "source_url": meta["url"],
        "resolved_url": resolved_url,
        "content": content,
        "filename": f"{safe_filename(meta['label'])}.pdf",
        "retrieved_at": retrieved_at,
    }


@st.cache_data(show_spinner=False)
def cached_fetch_pdf(source_key: str) -> Dict[str, Any]:
    return fetch_pdf(source_key)


def build_zip_bytes(items: Sequence[Dict[str, Any]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in items:
            archive.writestr(item["filename"], item["content"])
    return buffer.getvalue()


def fetch_selected_pdfs(selected_keys: Sequence[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    items: List[Dict[str, Any]] = []
    errors: List[str] = []

    for source_key in selected_keys:
        try:
            items.append(cached_fetch_pdf(source_key))
        except Exception as exc:  # noqa: BLE001 - show network and parsing errors in the UI
            errors.append(f"{PDF_SOURCES[source_key]['label']}: {exc}")

    return items, errors


def parse_lookup_data(items: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        temp_paths: Dict[str, Path] = {}

        for item in items:
            file_path = temp_root / f"{item['key']}.pdf"
            file_path.write_bytes(item["content"])
            temp_paths[item["key"]] = file_path

        demerit = parse_demerit_pdf(temp_paths["demerit"]) if "demerit" in temp_paths else {}
        bus = parse_bus_pdf(temp_paths["bus"]) if "bus" in temp_paths else {}
        swo = parse_swo_pdf(temp_paths["swo"]) if "swo" in temp_paths else {}

        updated_on = None
        for key in ("demerit", "bus", "swo"):
            if key not in temp_paths:
                continue
            lines = extract_text_lines(temp_paths[key])
            updated_on = parse_updated_on(lines) or updated_on

        return {
            "demerit": demerit,
            "bus": bus,
            "swo": swo,
            "updated_on": updated_on,
        }


def render_rows(title: str, rows: Sequence[Dict[str, Any]], empty_message: str) -> None:
    st.subheader(title)
    if not rows:
        st.info(empty_message)
        return

    st.dataframe(format_rows(list(rows)), use_container_width=True, hide_index=True)


def render_results(items: Sequence[Dict[str, Any]], errors: Sequence[str]) -> None:
    if errors:
        with st.expander("Warnings", expanded=False):
            for error in errors:
                st.warning(error)

    if not items:
        return

    st.success(f"Fetched {len(items)} PDF file(s).")

    columns = st.columns(min(3, len(items)))
    for index, item in enumerate(items):
        with columns[index % len(columns)]:
            st.download_button(
                label=f"Download {item['label']}",
                data=item["content"],
                file_name=item["filename"],
                mime="application/pdf",
                use_container_width=True,
                key=f"download-{item['key']}",
            )

    if len(items) > 1:
        st.download_button(
            label="Download all as ZIP",
            data=build_zip_bytes(items),
            file_name=f"mom_pdfs_{now_sg().strftime('%Y%m%d_%H%M%S')}.zip",
            mime="application/zip",
            use_container_width=True,
        )

    with st.expander("Fetch details", expanded=False):
        st.dataframe(
            [
                {
                    "PDF": item["label"],
                    "Retrieved at": item["retrieved_at"],
                    "Source URL": item["source_url"],
                    "Resolved PDF URL": item["resolved_url"],
                }
                for item in items
            ],
            use_container_width=True,
            hide_index=True,
        )


def render_lookup_results(lookup: Dict[str, Any], uens: List[str], criteria: Dict[str, Any]) -> None:
    results = build_results(
        uens,
        lookup.get("demerit", {}),
        lookup.get("bus", {}),
        lookup.get("swo", {}),
        criteria,
    )

    st.success(f"Checked {len(uens)} UEN(s).")
    with st.expander("Lookup summary", expanded=False):
        st.write(results.get("criteria_checks", []))
        st.write(f"Updated on: {lookup.get('updated_on') or 'Unknown'}")

    render_rows("Matches", results.get("meets", []), "No UENs matched the selected rules.")
    render_rows("Non-matches", results.get("not_meet", []), "All UENs matched the selected rules.")


def render_app() -> None:
    st.set_page_config(page_title="MOM PDF Scraper", layout="centered")
    st.title("MOM PDF Scraper")
    st.write("Fetch the public MOM PDF links and look up company data by UEN.")

    with st.form("pdf_fetch_form", clear_on_submit=False):
        selected_keys = st.multiselect(
            "PDF sources",
            options=list(PDF_SOURCES.keys()),
            default=list(PDF_SOURCES.keys()),
            format_func=lambda key: PDF_SOURCES[key]["label"],
        )
        uens_input = st.text_area(
            "UENs",
            placeholder="199403976M, 53146389C",
            help="Separate multiple UENs with commas, spaces, or new lines.",
            height=90,
        )
        with st.expander("Lookup options", expanded=False):
            demerit_threshold = st.number_input("Max demerit points", min_value=0, value=50, step=1)
            exclude_bus = st.checkbox("Exclude companies under BUS", value=True)
        submitted = st.form_submit_button("Fetch and search")

    if submitted:
        if not selected_keys:
            st.warning("Select at least one PDF source.")
            st.session_state.pop("pdf_fetch_result", None)
            st.session_state.pop("pdf_lookup_result", None)
            return

        with st.spinner("Fetching PDF links..."):
            items, errors = fetch_selected_pdfs(selected_keys)
        st.session_state["pdf_fetch_result"] = {"items": items, "errors": errors}

        if uens_input.strip():
            uens = parse_uens(uens_input)
            if not uens:
                st.warning("Enter at least one valid UEN.")
                st.session_state.pop("pdf_lookup_result", None)
            elif items:
                with st.spinner("Parsing PDF data..."):
                    lookup = parse_lookup_data(items)
                st.session_state["pdf_lookup_result"] = {
                    "lookup": lookup,
                    "uens": uens,
                    "criteria": {
                        "demerit_threshold": int(demerit_threshold),
                        "exclude_bus": exclude_bus,
                    },
                }

    result = st.session_state.get("pdf_fetch_result")
    if result:
        render_results(result.get("items", []), result.get("errors", []))
    else:
        st.caption("The app keeps downloads in memory only. Nothing is written to the local data folders.")

    lookup_result = st.session_state.get("pdf_lookup_result")
    if lookup_result:
        st.divider()
        render_lookup_results(
            lookup_result["lookup"],
            lookup_result["uens"],
            lookup_result["criteria"],
        )


if __name__ == "__main__":
    render_app()