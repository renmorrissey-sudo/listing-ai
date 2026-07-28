"""CSV preview and commit for external leads."""

from __future__ import annotations

import csv
import io

import external_leads_db as xdb
from external_leads.ingest import ingest_external_lead

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


def _rows_from_text(text):
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
            cleaned[k] = (v or "").strip() if isinstance(v, str) else ("" if v is None else str(v).strip())
        rows.append(cleaned)
    return headers, rows


def preview_csv(text, mapping=None, limit=25):
    headers, rows = _rows_from_text(text)
    if not headers:
        return {"error": "CSV has no headers.", "headers": [], "preview": [], "mapping": {}}
    mapping = mapping or suggest_mapping(headers)
    preview = []
    invalid = 0
    for i, row in enumerate(rows[:limit]):
        mapped = _apply_mapping(row, mapping)
        ok = bool(mapped.get("phone") or mapped.get("phone_number"))
        if not ok:
            invalid += 1
        preview.append({"row_number": i + 2, "mapped": mapped, "valid_phone": ok})
    return {
        "headers": headers,
        "mapping": mapping,
        "preview": preview,
        "total_rows": len(rows),
        "invalid_in_preview": invalid,
        "note": (
            "Even if CSV consent columns say true/yes, imported leads remain "
            "SMS consent Unverified and Sending Blocked until an agent confirms evidence."
        ),
    }


def _apply_mapping(row, mapping):
    out = {}
    for field, header in (mapping or {}).items():
        if header and header in row:
            out[field] = row[header]
    if out.get("full_name") and not out.get("name"):
        out["name"] = out["full_name"]
    return out


def commit_csv(user_id, text, mapping, source_row=None, filename=None, actor_user_id=None):
    headers, rows = _rows_from_text(text)
    if not headers:
        return {"error": "CSV has no headers."}
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
    }
    for i, row in enumerate(rows):
        mapped = _apply_mapping(row, mapping)
        mapped["raw_payload"] = row
        result = ingest_external_lead(
            user_id,
            mapped,
            source_row=source_row,
            method="csv",
            import_batch_id=batch_id,
            actor_user_id=actor_user_id or user_id,
        )
        if result.get("error"):
            stats["invalid"] += 1
            if len(stats["errors"]) < 20:
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
        f"row {e['row']}: {e['error']}" for e in stats["errors"]
    )
    xdb.finish_import_batch(batch_id, user_id, stats)
    return stats
