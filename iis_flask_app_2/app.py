from __future__ import annotations

import io
import csv
import os
import re
import zipfile
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import camelot
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, send_file
from pypdf import PdfReader
from zoneinfo import ZoneInfo

APP_ROOT = Path(__file__).parent
DATA_DIR = APP_ROOT / "data"
DOWNLOAD_DIR = DATA_DIR / "downloads"
PDF_ARCHIVE_ROOT = Path(os.environ.get("MOM_PDF_ARCHIVE_ROOT", str(DATA_DIR / "pdf_archives")))

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

DEMERIT_COLUMNS = [
    "UEN",
    "Name of company",
    "Demerit points accumulated by company",
    "Debarment phase and period",
]

UEN_PATTERN = re.compile(r"[A-Za-z0-9]{8,12}")

app = Flask(__name__)
app.config["LAST_BUNDLE"] = None
app.config["LAST_RESULTS"] = None
app.config["PARSED_PDF_RECORDS"] = None
app.config["CURRENT_BUNDLE_DIR"] = None


def ensure_dirs() -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def make_bundle_dir(bundle_stamp: str) -> Path:
    bundle_dir = PDF_ARCHIVE_ROOT / bundle_stamp
    bundle_dir.mkdir(parents=True, exist_ok=True)
    app.config["CURRENT_BUNDLE_DIR"] = bundle_dir
    return bundle_dir


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
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=r"No tables found in table area .*", category=UserWarning)
            extracted = camelot.read_pdf(str(pdf_path), pages="all", flavor="stream")
    except Exception:
        return tables

    for table in extracted:
        cleaned: List[List[str]] = []
        for _, row in table.df.iterrows():
            cleaned_row = [str(cell).strip() for cell in row.tolist()]
            if any(cell for cell in cleaned_row):
                cleaned.append(cleaned_row)
        if cleaned:
            tables.append(cleaned)

    return tables


def extract_text_lines(pdf_path: Path) -> List[str]:
    lines: List[str] = []
    try:
        reader = PdfReader(str(pdf_path))
    except Exception:
        return lines

    for page in reader.pages:
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
    bundle_dir = make_bundle_dir(bundle_stamp)

    for key, meta in PDF_SOURCES.items():
        try:
            resolved_url, content = resolve_pdf_url(meta["url"])
            file_path = bundle_dir / f"{key}.pdf"
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
        except Exception as exc:  # noqa: BLE001 - surface download errors in UI
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
        "points": ["demerit points", "demerit point", "accumulated by", "accumulated by company"],
        "debarment": ["debarment phase", "debarment period"],
    }
    records: Dict[str, Dict[str, Any]] = {}

    def cell_at(row: List[str], index: Optional[int]) -> str:
        if index is None or index < 0 or index >= len(row):
            return ""
        return row[index]

    def infer_points_cell(row: List[str], uen_idx: Optional[int], mapping: Dict[str, int]) -> str:
        points_idx = mapping.get("points")
        if points_idx is not None:
            return cell_at(row, points_idx)
        if uen_idx is not None and uen_idx + 2 < len(row):
            return row[uen_idx + 2]
        name_idx = mapping.get("name")
        if name_idx is not None and name_idx + 1 < len(row):
            candidate = row[name_idx + 1]
            if parse_int(candidate) is not None:
                return candidate
        for cell in row:
            if parse_int(cell) is not None:
                return cell
        return ""

    def infer_debarment_cell(row: List[str], uen_idx: Optional[int], mapping: Dict[str, int]) -> str:
        debarment_idx = mapping.get("debarment")
        if debarment_idx is not None:
            return cell_at(row, debarment_idx)
        if uen_idx is not None and uen_idx + 3 < len(row):
            return row[uen_idx + 3]
        return ""

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
                points_raw = infer_points_cell(row, uen_idx, mapping)
                debarment = infer_debarment_cell(row, uen_idx, mapping)
                records[uen] = {
                    "uen": uen,
                    "name": name.strip(),
                    "demerit_points": parse_int(points_raw),
                    "debarment": debarment.strip(),
                }
            continue

        for row in table[header_index + 1 :]:
            uen = extract_uen(cell_at(row, mapping.get("uen"))) if mapping.get("uen") is not None else None
            if not uen:
                continue
            _, uen_idx = find_uen_in_row(row)
            name = cell_at(row, mapping.get("name"))
            points_raw = infer_points_cell(row, uen_idx, mapping)
            debarment = infer_debarment_cell(row, uen_idx, mapping)
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


def load_pdf_bundle() -> Dict[str, Any]:
    if "pdf_bundle" in app.config:
        return app.config["pdf_bundle"]

    pdf_info, errors, bundle_display, bundle_stamp = download_pdfs()
    zip_name, zip_bytes = create_zip_bytes(pdf_info, bundle_stamp) if pdf_info else (None, None)
    bundle = {
        "pdf_info": pdf_info,
        "errors": errors,
        "bundle_display": bundle_display,
        "bundle_stamp": bundle_stamp,
        "zip_name": zip_name,
        "zip_bytes": zip_bytes,
    }
    app.config["pdf_bundle"] = bundle
    return bundle


def load_parsed_records(pdf_info: Dict[str, Any]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    cached = app.config.get("PARSED_PDF_RECORDS")
    if cached and cached.get("bundle_stamp") == app.config.get("pdf_bundle", {}).get("bundle_stamp"):
        return cached["records"]

    records = {
        "demerit": parse_demerit_pdf(pdf_info["demerit"]["path"]) if "demerit" in pdf_info else {},
        "bus": parse_bus_pdf(pdf_info["bus"]["path"]) if "bus" in pdf_info else {},
        "swo": parse_swo_pdf(pdf_info["swo"]["path"]) if "swo" in pdf_info else {},
    }
    app.config["PARSED_PDF_RECORDS"] = {
        "bundle_stamp": app.config.get("pdf_bundle", {}).get("bundle_stamp"),
        "records": records,
    }
    return records


def build_bundle_view(pdf_bundle: Dict[str, Any]) -> Dict[str, Any]:
    pdf_info = pdf_bundle.get("pdf_info", {})
    retrieval_rows = [
        {
            "label": meta.get("label", key),
            "retrieved_at": meta.get("retrieved_at", "-"),
            "resolved_url": meta.get("resolved_url", ""),
        }
        for key, meta in pdf_info.items()
    ]
    return {
        "bundle_display": pdf_bundle.get("bundle_display"),
        "zip_name": pdf_bundle.get("zip_name"),
        "zip_ready": bool(pdf_bundle.get("zip_name") and pdf_bundle.get("zip_bytes")),
        "errors": pdf_bundle.get("errors", []),
        "retrieval_rows": retrieval_rows,
        "pdf_info": pdf_info,
    }


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


def build_results_csv(results: Dict[str, Any]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "Status",
        "UEN",
        "Company Name",
        "Demerit Points",
        "Debarment Phase/Period",
        "Is under BUS",
        "BUS Entry Date",
        "SWO Count",
        "Notes",
    ])

    for row in results.get("meets", []):
        writer.writerow([
            "Meets criteria",
            row.get("uen", ""),
            row.get("name", ""),
            row.get("demerit_points", ""),
            row.get("debarment", ""),
            row.get("is_under_bus", ""),
            row.get("bus_entry_date", ""),
            row.get("swo_count", ""),
            row.get("notes", ""),
        ])

    for row in results.get("not_meet", []):
        writer.writerow([
            "Did not meet criteria",
            row.get("uen", ""),
            row.get("name", ""),
            row.get("demerit_points", ""),
            row.get("debarment", ""),
            row.get("is_under_bus", ""),
            row.get("bus_entry_date", ""),
            row.get("swo_count", ""),
            row.get("notes", ""),
        ])

    return buffer.getvalue().encode("utf-8-sig")


def format_cell(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


@app.template_filter("fmt")
def fmt(value: Any) -> str:
    return format_cell(value)


def format_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    formatted: List[Dict[str, str]] = []
    for row in rows:
        formatted.append(
            {
                "UEN": format_cell(row.get("uen")),
                "Company Name": format_cell(row.get("name")),
                "Demerit Points": format_cell(row.get("demerit_points")),
                "Debarment Phase/Period": format_cell(row.get("debarment")),
                "Is under BUS": format_cell(row.get("is_under_bus")),
                "BUS Entry Date": format_cell(row.get("bus_entry_date")),
                "SWO Count": format_cell(row.get("swo_count")),
                "Notes": format_cell(row.get("notes")),
            }
        )
    return formatted


@app.route("/", methods=["GET", "POST"])
def index() -> str:
    pdf_bundle = load_pdf_bundle()
    bundle_view = build_bundle_view(pdf_bundle)
    defaults = {
        "demerit_threshold": 50,
        "exclude_bus": True,
    }
    context: Dict[str, Any] = {
        "input_uens": "",
        "criteria": defaults,
        "results": None,
        "errors": bundle_view["errors"],
        "now": now_sg().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_on": None,
        "download_name": bundle_view["zip_name"],
        "demerit_columns": DEMERIT_COLUMNS,
        "bundle_display": bundle_view["bundle_display"],
        "retrieval_rows": bundle_view["retrieval_rows"],
        "pdf_info": bundle_view["pdf_info"],
        "stats": None,
        "zip_ready": bundle_view["zip_ready"],
    }

    if request.method == "POST":
        raw_uens = request.form.get("uens", "")
        context["input_uens"] = raw_uens

        criteria = {
            "demerit_threshold": parse_int(request.form.get("demerit_threshold", "")) or defaults["demerit_threshold"],
            "exclude_bus": request.form.get("exclude_bus") == "on",
        }
        context["criteria"] = criteria

        uens = parse_uens(raw_uens)
        if uens:
            if pdf_bundle.get("pdf_info"):
                pdf_info = pdf_bundle["pdf_info"]
                parsed_records = load_parsed_records(pdf_info)
                demerit = parsed_records["demerit"]
                bus = parsed_records["bus"]
                swo = parsed_records["swo"]

                updated_on = None
                for key in ["demerit", "bus", "swo"]:
                    if key not in pdf_info:
                        continue
                    lines = extract_text_lines(pdf_info[key]["path"])
                    updated_on = parse_updated_on(lines) or updated_on

                results = build_results(uens, demerit, bus, swo, criteria)
                app.config["LAST_RESULTS"] = {
                    "results": results,
                    "updated_on": updated_on,
                    "retrieved": pdf_bundle["bundle_display"],
                }
                context["updated_on"] = updated_on
                context["results"] = results
                context["download_name"] = pdf_bundle["zip_name"]
                context["zip_ready"] = bool(pdf_bundle["zip_name"])
                context["stats"] = {
                    "demerit_count": len(demerit),
                    "bus_count": len(bus),
                    "swo_count": len(swo),
                    "resolved": {key: meta["resolved_url"] for key, meta in pdf_info.items()},
                    "retrieved": pdf_bundle["bundle_display"],
                }

    return render_template("index.html", **context)


@app.route("/download/results.csv")
def download_results_csv():
    payload = app.config.get("LAST_RESULTS")
    if not payload or not payload.get("results"):
        return "No results available for export", 404

    csv_bytes = build_results_csv(payload["results"])
    filename = f"mom_scraper_results_{now_sg().strftime('%Y%m%d_%H%M%S')}.csv"
    return send_file(
        io.BytesIO(csv_bytes),
        as_attachment=True,
        download_name=filename,
        mimetype="text/csv",
    )


@app.route("/download/<path:filename>")
def download(filename: str):
    file_path = DOWNLOAD_DIR / filename
    if not file_path.exists():
        bundle = app.config.get("pdf_bundle")
        if not bundle or bundle.get("zip_name") != filename or not bundle.get("zip_bytes"):
            return "File not found", 404
        return send_file(
            io.BytesIO(bundle["zip_bytes"]),
            as_attachment=True,
            download_name=filename,
            mimetype="application/zip",
        )
    return send_file(file_path, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True)
