"""Tenant-scoped persistence for Competitive Market Analysis reports."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from db import get_db


def _loads(value, fallback):
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _row_to_dict(row):
    if not row:
        return None
    item = dict(row)
    item["criteria"] = _loads(item.pop("criteria_json", None), {})
    item["selected_comparables"] = _loads(item.pop("comparables_json", None), [])
    item["excluded_comparables"] = _loads(item.pop("excluded_json", None), [])
    item["analysis"] = _loads(item.pop("analysis_json", None), {})
    return item


def create_report(user_id, report):
    criteria = report["criteria"]
    if not user_id:
        raise ValueError("user_id is required")
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO cma_reports (
                user_id, prospect_type, prospect_name, subject_address, city, state,
                comp_count, date_range_months, criteria_json, comparables_json,
                excluded_json, analysis_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                criteria["prospect_type"],
                criteria.get("prospect_name") or None,
                criteria["subject_address"],
                criteria["city"],
                criteria["state"],
                criteria["comp_count"],
                criteria["date_range_months"],
                json.dumps(criteria),
                json.dumps(report["selected_comparables"]),
                json.dumps(report.get("excluded_comparables") or []),
                json.dumps(report["analysis"]),
                now,
            ),
        )
        report_id = cur.lastrowid
    return get_report(user_id, report_id)


def get_report(user_id, report_id):
    if not user_id or not report_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM cma_reports WHERE id = ? AND user_id = ?",
            (report_id, user_id),
        ).fetchone()
    return _row_to_dict(row)


def list_recent(user_id, limit=12):
    if not user_id:
        return []
    limit = max(1, min(50, int(limit or 12)))
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM cma_reports
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]
