from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import pdfplumber
import requests
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, send_file, render_template_string

APP_ROOT = Path(__file__).parent
DATA_DIR = APP_ROOT / "data"
PDF_DIR = DATA_DIR / "pdfs"
DOWNLOAD_DIR = DATA_DIR / "downloads"
try:
    SG_TZ = ZoneInfo("Asia/Singapore")
except Exception:
    SG_TZ = timezone(timedelta(hours=8), name="Asia/Singapore")

PDF_SOURCES = {
    "demerit": {
        "label": "Demerit Points",
        "url": "https://www.mom.gov.sg/orca/list-of-companies-with-demerits",
    },
    "bus": {
        "label": "Business Under Surveillance",
        "url": "https://www.mom.gov.sg/-/media/mom/documents/safety-health/reports-stats/list-of-companies-under-bus.pdf",
    },
    "swo": {
        "label": "Stop Work Orders",
        "url": "https://www.mom.gov.sg/-/media/mom/documents/safety-health/reports-stats/stop-work-orders.pdf",
    },
}


def ensure_dirs() -> None:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def now_sg() -> datetime:
    return datetime.now(tz=SG_TZ)


def normalize_company_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", value.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return cleaned or "file"


def is_uen_candidate(value: str) -> bool:
    return any(ch.isdigit() for ch in value) and any(ch.isalpha() for ch in value)


UEN_PATTERN = re.compile(r"[A-Za-z0-9]{8,12}")


def extract_uen(text: str) -> Optional[str]:
    if not text:
        return None

    for candidate in UEN_PATTERN.findall(text):
        if is_uen_candidate(candidate):
            return candidate.upper()

    compact = re.sub(r"\s+", "", text)
    for candidate in UEN_PATTERN.findall(compact):
        if is_uen_candidate(candidate):
            return candidate.upper()

    return None


def parse_int(value: str) -> Optional[int]:
    if not value:
        return None
    digits = re.findall(r"\d+", value.replace(",", ""))
    return int(digits[0]) if digits else None


def extract_tables(pdf_path: Path) -> List[List[List[str]]]:
    tables: List[List[List[str]]] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                cleaned: List[List[str]] = []
                for row in table:
                    if not row:
                        continue
                    cleaned_row = [cell.strip() if cell else "" for cell in row]
                    if any(cell for cell in cleaned_row):
                        cleaned.append(cleaned_row)
                if cleaned:
                    tables.append(cleaned)
    return tables


def extract_text_lines(pdf_path: Path) -> List[str]:
    lines: List[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend([line.strip() for line in text.splitlines() if line.strip()])
    return lines


def find_uen_in_row(row: List[str]) -> Tuple[Optional[str], Optional[int]]:
    for idx, cell in enumerate(row):
        uen = extract_uen(cell or "")
        if uen:
            return uen, idx
    return None, None


def detect_header(row: List[str], header_map: Dict[str, List[str]]) -> Dict[str, int]:
    normalized = [normalize_text(cell) for cell in row]
    mapping: Dict[str, int] = {}
    for key, synonyms in header_map.items():
        for idx, cell in enumerate(normalized):
            if any(token in cell for token in synonyms):
                mapping[key] = idx
                break
    return mapping


def resolve_pdf_url(url: str) -> Tuple[str, bytes]:
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, timeout=30, headers=headers)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if "application/pdf" in content_type or url.lower().endswith(".pdf"):
        return url, response.content

    soup = BeautifulSoup(response.text, "html.parser")
    link = None
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if ".pdf" in href.lower():
            link = href
            break
    if not link:
        raise ValueError("No PDF link found on page")

    pdf_url = urljoin(url, link)
    pdf_response = requests.get(pdf_url, timeout=30, headers=headers)
    pdf_response.raise_for_status()
    return pdf_url, pdf_response.content


def download_pdfs() -> Tuple[Dict[str, Any], List[str], str, str]:
    ensure_dirs()
    pdf_info: Dict[str, Any] = {}
    errors: List[str] = []
    bundle_dt = now_sg()
    bundle_display = bundle_dt.strftime("%Y-%m-%d %H:%M:%S")
    bundle_stamp = bundle_dt.strftime("%Y%m%d_%H%M%S")

    for key, meta in PDF_SOURCES.items():
        try:
            resolved_url, content = resolve_pdf_url(meta["url"])
            file_path = PDF_DIR / f"{key}.pdf"
            file_path.write_bytes(content)
            retrieved_dt = now_sg()
            pdf_info[key] = {
                "label": meta["label"],
                "source_url": meta["url"],
                "resolved_url": resolved_url,
                "path": file_path,
                "retrieved_at": retrieved_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "retrieved_at_stamp": retrieved_dt.strftime("%Y%m%d_%H%M%S"),
            }
        except Exception as exc:  # noqa: BLE001 - surface download errors to client
            errors.append(f"{meta['label']}: {exc}")

    return pdf_info, errors, bundle_display, bundle_stamp


def parse_updated_on(lines: List[str]) -> Optional[str]:
    for line in lines:
        match = re.search(r"Updated on\s+([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})", line)
        if match:
            return match.group(1)
    for line in lines:
        match = re.search(r"accurate as at\s+([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})", line, re.IGNORECASE)
        if match:
            return match.group(1)
    for line in lines:
        match = re.search(r"as at\s+([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})", line, re.IGNORECASE)
        if match:
            return match.group(1)
    for line in lines:
        match = re.search(r"\b([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})\b", line)
        if match:
            return match.group(1)
    return None


def parse_demerit_pdf(pdf_path: Path) -> Dict[str, Dict[str, Any]]:
    header_map = {
        "uen": ["uen"],
        "name": ["name of company", "company name", "name"],
        "points": ["demerit points", "demerit point"],
        "debarment": ["debarment phase", "debarment period"],
    }
    records: Dict[str, Dict[str, Any]] = {}

    for table in extract_tables(pdf_path):
        header_index = None
        mapping: Dict[str, int] = {}
        for idx, row in enumerate(table):
            mapping = detect_header(row, header_map)
            if "name" in mapping:
                header_index = idx
                break
        if header_index is None:
            for row in table:
                uen, uen_idx = find_uen_in_row(row)
                if not uen or uen_idx is None:
                    continue
                name = row[uen_idx + 1] if uen_idx + 1 < len(row) else ""
                points_raw = row[uen_idx + 2] if uen_idx + 2 < len(row) else ""
                debarment = row[uen_idx + 3] if uen_idx + 3 < len(row) else ""
                records[uen] = {
                    "uen": uen,
                    "name": name.strip(),
                    "demerit_points": parse_int(points_raw),
                    "debarment": debarment.strip(),
                }
            continue

        for row in table[header_index + 1 :]:
            uen = extract_uen(row[mapping["uen"]]) if mapping.get("uen") is not None else None
            if not uen:
                continue
            name = row[mapping.get("name", -1)] if mapping.get("name") is not None else ""
            points_raw = row[mapping.get("points", -1)] if mapping.get("points") is not None else ""
            debarment = row[mapping.get("debarment", -1)] if mapping.get("debarment") is not None else ""
            records[uen] = {
                "uen": uen,
                "name": name.strip(),
                "demerit_points": parse_int(points_raw),
                "debarment": debarment.strip(),
            }

    if records:
        return records

    for line in extract_text_lines(pdf_path):
        uen = extract_uen(line)
        if not uen:
            continue
        tokens = line.replace(uen, "").strip().split()
        name = " ".join(tokens[:-1]) if len(tokens) > 1 else ""
        points = parse_int(tokens[-1]) if tokens else None
        records[uen] = {
            "uen": uen,
            "name": name,
            "demerit_points": points,
            "debarment": "",
        }

    return records


def parse_bus_pdf(pdf_path: Path) -> Dict[str, Dict[str, Any]]:
    header_map = {
        "uen": ["uen", "acra no", "acra"],
        "name": ["name of company", "company", "company name", "name"],
        "entry": ["entry", "date"],
    }
    records: Dict[str, Dict[str, Any]] = {}

    for table in extract_tables(pdf_path):
        header_index = None
        mapping: Dict[str, int] = {}
        for idx, row in enumerate(table):
            mapping = detect_header(row, header_map)
            if "uen" in mapping:
                header_index = idx
                break
        if header_index is None:
            for row in table:
                uen, uen_idx = find_uen_in_row(row)
                if not uen or uen_idx is None:
                    continue
                name = row[uen_idx + 1] if uen_idx + 1 < len(row) else ""
                entry = row[uen_idx + 2] if uen_idx + 2 < len(row) else ""
                records[uen] = {
                    "uen": uen,
                    "name": name.strip(),
                    "entry_date": entry.strip(),
                }
            continue

        for row in table[header_index + 1 :]:
            uen = extract_uen(row[mapping["uen"]]) if mapping.get("uen") is not None else None
            if not uen:
                continue
            name = row[mapping.get("name", -1)] if mapping.get("name") is not None else ""
            entry = row[mapping.get("entry", -1)] if mapping.get("entry") is not None else ""
            records[uen] = {
                "uen": uen,
                "name": name.strip(),
                "entry_date": entry.strip(),
            }

    if records:
        return records

    for line in extract_text_lines(pdf_path):
        uen = extract_uen(line)
        if not uen:
            continue
        name = line.replace(uen, "").strip()
        records[uen] = {"uen": uen, "name": name, "entry_date": ""}

    return records


def parse_swo_pdf(pdf_path: Path) -> Dict[str, Dict[str, Any]]:
    header_map = {
        "name": ["name of company", "company name", "company", "name"],
    }
    records: Dict[str, Dict[str, Any]] = {}

    for table in extract_tables(pdf_path):
        header_index = None
        mapping: Dict[str, int] = {}
        for idx, row in enumerate(table):
            mapping = detect_header(row, header_map)
            if "uen" in mapping:
                header_index = idx
                break
        if header_index is None:
            for row in table:
                if not row:
                    continue
                row_text = normalize_text(" ".join(row))
                if "name of company" in row_text and "s/no" in row_text:
                    continue
                name = row[1] if len(row) > 1 else row[0]
                normalized = normalize_company_name(name)
                if not normalized:
                    continue
                existing = records.get(normalized, {"name": name.strip(), "count": 0})
                existing["count"] += 1
                records[normalized] = existing
            continue

        for row in table[header_index + 1 :]:
            name = row[mapping.get("name", -1)] if mapping.get("name") is not None else ""
            normalized = normalize_company_name(name)
            if not normalized:
                continue
            existing = records.get(normalized, {"name": name.strip(), "count": 0})
            existing["count"] += 1
            records[normalized] = existing

    if records:
        return records

    for line in extract_text_lines(pdf_path):
        normalized = normalize_company_name(line)
        if not normalized:
            continue
        existing = records.get(normalized, {"name": line, "count": 0})
        existing["count"] += 1
        records[normalized] = existing

    return records


def parse_uens(raw: str) -> List[str]:
    if not raw:
        return []
    parts = re.split(r"[\s,;]+", raw.strip())
    uens = [part.strip().upper() for part in parts if part.strip()]
    return list(dict.fromkeys(uens))


def create_zip_bytes(pdf_info: Dict[str, Any], bundle_stamp: str) -> Tuple[str, bytes]:
    ensure_dirs()
    zip_name = f"mom_pdfs_{bundle_stamp}.zip"

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for meta in pdf_info.values():
            label = safe_filename(meta.get("label", meta["path"].stem))
            suffix = meta["path"].suffix
            file_stamp = meta.get("retrieved_at_stamp", bundle_stamp)
            stamped_name = f"{label}_{file_stamp}{suffix}"
            zf.write(meta["path"], stamped_name)

    return zip_name, buffer.getvalue()


def build_results(
    uens: List[str],
    demerit: Dict[str, Any],
    bus: Dict[str, Any],
    swo: Dict[str, Any],
    criteria: Dict[str, Any],
) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    criteria_checks = []

    if criteria.get("demerit_threshold") is not None:
        criteria_checks.append(f"Number of demerit points < {criteria['demerit_threshold']}")
    if criteria.get("exclude_bus"):
        criteria_checks.append("NOT Under BUS")

    for uen in uens:
        demerit_row = demerit.get(uen, {})
        bus_row = bus.get(uen, {})
        company_name = demerit_row.get("name") or bus_row.get("name") or ""
        swo_key = normalize_company_name(company_name)
        swo_row = swo.get(swo_key, {}) if swo_key else {}

        demerit_points = demerit_row.get("demerit_points")
        demerit_found = demerit_points is not None
        if demerit_points is None:
            demerit_points = 0
        is_under_bus = uen in bus
        swo_count = swo_row.get("count") if swo_row else None

        checks: List[Tuple[str, bool]] = []
        if criteria.get("demerit_threshold") is not None:
            checks.append((
                f"Demerit points < {criteria['demerit_threshold']}",
                demerit_points < criteria["demerit_threshold"],
            ))

        if criteria.get("exclude_bus"):
            checks.append(("Under BUS", not is_under_bus))

        notes = [label for label, passed in checks if not passed]

        results.append(
            {
                "uen": uen,
                "name": company_name or swo_row.get("name") or "",
                "demerit_points": demerit_points,
                "demerit_found": demerit_found,
                "debarment": demerit_row.get("debarment", ""),
                "is_under_bus": is_under_bus,
                "bus_entry_date": bus_row.get("entry_date", ""),
                "swo_count": swo_count,
                "notes": "; ".join(notes),
            }
        )

    meets = [row for row in results if not row["notes"]]
    not_meet = [row for row in results if row["notes"]]

    return {
        "rows": results,
        "meets": meets,
        "not_meet": not_meet,
        "criteria_checks": criteria_checks,
    }


app = Flask(__name__)
app.config["LAST_BUNDLE"] = None


INDEX_HTML = """
<html>
  <body>
    <h2>MOM Company Info Scraper (Flask)</h2>
    <form method="post" action="/run">
      <label>UENs (comma separated):</label><br>
      <textarea name="uens" rows="4" cols="60"></textarea><br>
      <label>Demerit threshold:</label>
      <input type="number" name="demerit_threshold" value="50" min="0"/><br>
      <label>Exclude BUS:</label>
      <input type="checkbox" name="exclude_bus" checked/><br>
      <button type="submit">Run Scraper</button>
    </form>
    <p>Use <a href="/download_zip">/download_zip</a> to download the last PDF bundle.</p>
  </body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML)


@app.route("/run", methods=["POST"])
def run_scraper():
    uens_input = request.form.get("uens", "")
    demerit_threshold = int(request.form.get("demerit_threshold", "50"))
    exclude_bus = request.form.get("exclude_bus") is not None

    uens = parse_uens(uens_input)
    if not uens:
        return jsonify({"error": "No UENs provided"}), 400

    pdf_info, errors, bundle_display, bundle_stamp = download_pdfs()

    demerit = parse_demerit_pdf(pdf_info["demerit"]["path"]) if "demerit" in pdf_info else {}
    bus = parse_bus_pdf(pdf_info["bus"]["path"]) if "bus" in pdf_info else {}
    swo = parse_swo_pdf(pdf_info["swo"]["path"]) if "swo" in pdf_info else {}

    updated_on = None
    for key in ["demerit", "bus", "swo"]:
        if key not in pdf_info:
            continue
        lines = extract_text_lines(pdf_info[key]["path"])
        updated_on = parse_updated_on(lines) or updated_on

    criteria = {"demerit_threshold": demerit_threshold, "exclude_bus": exclude_bus}
    results = build_results(uens, demerit, bus, swo, criteria)

    zip_name, zip_bytes = (None, None)
    if pdf_info:
        zip_name, zip_bytes = create_zip_bytes(pdf_info, bundle_stamp)
        app.config["LAST_BUNDLE"] = (zip_name, zip_bytes)

    return jsonify(
        {
            "bundle_display": bundle_display,
            "bundle_stamp": bundle_stamp,
            "errors": errors,
            "updated_on": updated_on,
            "parsed_counts": {"demerit": len(demerit), "bus": len(bus), "swo": len(swo)},
            "results": results,
            "zip_name": zip_name,
        }
    )


@app.route("/download_zip", methods=["GET"])
def download_zip():
    bundle = app.config.get("LAST_BUNDLE")
    if not bundle:
        return jsonify({"error": "No bundle available. Run /run first."}), 404
    zip_name, zip_bytes = bundle
    return send_file(io.BytesIO(zip_bytes), mimetype="application/zip", as_attachment=True, download_name=zip_name)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8501)
