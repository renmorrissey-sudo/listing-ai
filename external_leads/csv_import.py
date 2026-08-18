"""CSV preview and commit for external leads."""

from __future__ import annotations

import csv
import io
import re

import external_leads_db as xdb
from external_leads.duplicates import find_duplicate
from external_leads.ingest import ingest_external_lead
from lead_service import normalize_phone_e164
from sms_validation import validate_e164_phone

CSV_FIELD_ALIASES = {
    "first_name": {"first_name", "firstname", "first"},
    "last_name": {"last_name", "lastname", "last"},
    "full_name": {"full_name", "name", "lead_name", "contact_name"},
    "phone": {"phone", "phone_number", "mobile", "cell", "telephone"},
    "email": {"email", "email_address"},
    "source": {"source", "lead_source", "provider"},
    "external_record_id": {"external_record_id", "record_id", "lead_id", "id", "external_id"},
    "property_address": {"property_address", "address", "listing_address"},
    "property_url": {"property_url", "listing_url", "url"},
    "inquiry_notes": {"inquiry_notes", "notes", "message", "comments"},
    "lead_type": {"lead_type", "type"},
    "created_date": {"created_date", "created_at", "date"},
    "agent": {"agent", "agent_name"},
    "brokerage": {"brokerage", "brokerage_name"},
    "original_consent_status": {"original_consent_status", "consent", "sms_consent", "consent_status"},
    "original_consent_date": {"original_consent_date", "consent_date"},
    "original_consent_text": {"original_consent_text", "consent_text", "disclosure"},
}

CSV_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB
CSV_MAX_ROWS = 5000
DUPLICATE_MODES = ("skip", "update")
SAMPLE_CSV_HEADERS = [
    "first_name",
    "last_name",
    "phone",
    "email",
    "external_record_id",
    "property_address",
    "consent",
    "consent_date",
    "consent_text",
]
SAMPLE_CSV_ROW = [
    "Alex",
    "Example",
    "7202891700",
    "alex.example@example.com",
    "demo-ext-001",
    "100 Demo Street, Denver CO",
    "true",
    "2026-01-15",
    "Example portal opt-in checkbox (not sufficient alone)",
]
def sample_csv_text() -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(SAMPLE_CSV_HEADERS)
    writer.writerow(SAMPLE_CSV_ROW)
    return buf.getvalue()


def neutralize_formula(value: str) -> str:
    """Prefix spreadsheet formula-like values so Excel/Sheets won't execute them.

    Preserves phone-like values (+1720… / 720…) so E.164 normalization still works.
    """
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    stripped = value.lstrip()
    if not stripped:
        return value
    digits = re.sub(r"\D", "", stripped)
    if stripped[0] in "+-" and len(digits) >= 10:
        return value
    if stripped[0] in "=+-@":
        return "'" + value
    return value


def _normalize_header(value):
    return (value or "").strip().lower().replace(" ", "_")


def suggest_mapping(headers):
    mapping = {}
    normalized = {_normalize_header(h): h for h in headers}
    for field, aliases in CSV_FIELD_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[field] = normalized[alias]
                break
    return mapping


def decode_csv_bytes(raw: bytes) -> tuple[str | None, str | None]:
    """Decode UTF-8 (with BOM) CSV bytes. Returns (text, error)."""
    if raw is None:
        return None, "Upload a CSV file."
    if len(raw) > CSV_MAX_BYTES:
        return None, f"CSV must be {CSV_MAX_BYTES // (1024 * 1024)} MB or smaller."
    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return None, "CSV must be UTF-8 encoded."


def validate_csv_filename(filename: str | None) -> str | None:
    if not filename:
        return None
    name = filename.strip().lower()
    if not name.endswith(".csv"):
        return "Only .csv files are accepted."
    return None


def _rows_from_text(text):
    # utf-8-sig already strips BOM at decode time; strip residual BOM just in case.
    text = (text or "").lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    rows = []
    for row in reader:
        cleaned = {}
        for k, v in row.items():
            if k is None:
                continue
            if isinstance(v, list):
                v = ",".join(str(x) for x in v)
            raw = (v or "").strip() if isinstance(v, str) else ("" if v is None else str(v).strip())
            cleaned[k] = neutralize_formula(raw)
        rows.append(cleaned)
    return list(headers), rows


def _apply_mapping(row, mapping):
    out = {}
    for field, header in (mapping or {}).items():
        if header and header in row:
            out[field] = row[header]
    if out.get("full_name") and not out.get("name"):
        out["name"] = out["full_name"]
    phone_raw = out.get("phone") or out.get("phone_number") or ""
    if phone_raw:
        normalized = normalize_phone_e164(phone_raw)
        phone_ok, _err = validate_e164_phone(phone_raw)
        out["phone"] = normalized or phone_raw
        out["phone_normalized"] = normalized
        out["phone_valid"] = bool(phone_ok)
    else:
        out["phone_normalized"] = ""
        out["phone_valid"] = False
    # Tenant cannot be overridden from CSV
    out.pop("user_id", None)
    out.pop("tenant_id", None)
    return out


def preview_csv(text, mapping=None, limit=25, user_id=None, duplicate_mode="skip"):
    headers, rows = _rows_from_text(text)
    if not headers:
        return {"error": "CSV has no headers.", "headers": [], "preview": [], "mapping": {}}
    if len(rows) > CSV_MAX_ROWS:
        return {
            "error": f"CSV has too many rows (max {CSV_MAX_ROWS}).",
            "headers": headers,
            "preview": [],
            "mapping": mapping or {},
        }
    mapping = mapping or suggest_mapping(headers)
    mode = duplicate_mode if duplicate_mode in DUPLICATE_MODES else "skip"
    preview = []
    invalid = 0
    duplicates = 0
    for i, row in enumerate(rows[:limit]):
        mapped = _apply_mapping(row, mapping)
        ok = bool(mapped.get("phone_valid"))
        if not ok:
            invalid += 1
        dup_match = None
        if user_id and ok:
            existing, match = find_duplicate(
                user_id,
                phone=mapped.get("phone"),
                email=mapped.get("email"),
                external_source_id=None,
                external_record_id=mapped.get("external_record_id"),
            )
            if existing:
                duplicates += 1
                dup_match = match
        preview.append(
            {
                "row_number": i + 2,
                "mapped": {
                    k: v
                    for k, v in mapped.items()
                    if k not in {"phone_normalized", "phone_valid"}
                },
                "phone_normalized": mapped.get("phone_normalized") or "",
                "valid_phone": ok,
                "duplicate_match": dup_match,
                "duplicate_action": (mode if dup_match else None),
            }
        )
    return {
        "headers": headers,
        "mapping": mapping,
        "preview": preview,
        "total_rows": len(rows),
        "invalid_in_preview": invalid,
        "duplicates_in_preview": duplicates,
        "duplicate_mode": mode,
        "note": (
            "Imported leads default to SMS consent Unverified/Not certified and Sending Blocked. "
            "Consent columns create pending evidence only — they never enable SMS. "
            f"Duplicate handling defaults to '{mode}' (skip keeps the existing lead unchanged)."
        ),
    }


def build_error_csv(errors: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["row", "error"])
    for item in errors or []:
        writer.writerow([item.get("row", ""), neutralize_formula(str(item.get("error") or ""))])
    return buf.getvalue()


def commit_csv(
    user_id,
    text,
    mapping,
    source_row=None,
    filename=None,
    actor_user_id=None,
    duplicate_mode="skip",
):
    headers, rows = _rows_from_text(text)
    if not headers:
        return {"error": "CSV has no headers."}
    if len(rows) > CSV_MAX_ROWS:
        return {"error": f"CSV has too many rows (max {CSV_MAX_ROWS})."}
    mode = duplicate_mode if duplicate_mode in DUPLICATE_MODES else "skip"
    batch_id = xdb.create_import_batch(
        user_id,
        external_source_id=(source_row or {}).get("id"),
        filename=filename,
    )
    stats = {
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "invalid": 0,
        "pending_evidence": 0,
        "errors": [],
        "batch_id": batch_id,
        "duplicate_mode": mode,
        "sms_sent": 0,
    }
    for i, row in enumerate(rows):
        mapped = _apply_mapping(row, mapping)
        mapped["raw_payload"] = row
        # Hard block CSV tenant override attempts
        mapped.pop("user_id", None)
        mapped.pop("tenant_id", None)

        if mode == "skip":
            existing, match = find_duplicate(
                user_id,
                phone=mapped.get("phone") if mapped.get("phone_valid") else None,
                email=mapped.get("email"),
                external_source_id=(source_row or {}).get("id"),
                external_record_id=mapped.get("external_record_id"),
            )
            if existing:
                stats["skipped"] += 1
                continue

        result = ingest_external_lead(
            user_id,
            mapped,
            source_row=source_row,
            method="csv",
            import_batch_id=batch_id,
            actor_user_id=actor_user_id or user_id,
            allow_identity_update=(mode == "update"),
        )
        if result.get("error"):
            stats["invalid"] += 1
            if len(stats["errors"]) < 200:
                stats["errors"].append({"row": i + 2, "error": result["error"]})
            continue
        action = result.get("action")
        if action == "created":
            stats["created"] += 1
        elif action == "updated":
            stats["updated"] += 1
        else:
            stats["skipped"] += 1
        if result.get("pending_evidence_id"):
            stats["pending_evidence"] += 1
    stats["error_summary"] = "; ".join(
        f"row {e['row']}: {e['error']}" for e in stats["errors"][:20]
    )
    stats["error_csv"] = build_error_csv(stats["errors"]) if stats["errors"] else ""
    xdb.finish_import_batch(batch_id, user_id, stats)
    # Never auto-send SMS from CSV import.
    stats["sms_sent"] = 0
    return stats
