"""Tenant-scoped calculations and inputs for external lead source ROI."""

from __future__ import annotations

from datetime import datetime, timezone

from db import get_db


def _now():
    return datetime.now(timezone.utc).isoformat()


def add_source_spend(user_id, source_id, amount_cents, spent_on, notes=None):
    if amount_cents <= 0:
        return None, "Marketing cost must be greater than zero."
    with get_db() as conn:
        source = conn.execute(
            "SELECT id FROM external_lead_sources WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if not source:
            return None, "Lead source not found."
        cur = conn.execute(
            """
            INSERT INTO external_lead_source_spend
                (user_id, external_source_id, amount_cents, spent_on, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, source_id, amount_cents, spent_on, (notes or "")[:500] or None, _now()),
        )
        return cur.lastrowid, None


def delete_source_spend(user_id, spend_id):
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM external_lead_source_spend WHERE id = ? AND user_id = ?",
            (spend_id, user_id),
        )
        return cur.rowcount > 0

def set_lead_gci(user_id, lead_id, amount_cents):
    if amount_cents < 0:
        return None, "GCI cannot be negative."
    with get_db() as conn:
        lead = conn.execute(
            """
            SELECT id, status, external_source_id
            FROM leads
            WHERE id = ? AND user_id = ?
            """,
            (lead_id, user_id),
        ).fetchone()
        if not lead or not lead.get("external_source_id"):
            return None, "External lead not found."
        if lead.get("status") != "closed_won":
            return None, "GCI can be recorded after a lead is marked Closed Won."
        conn.execute(
            """
            UPDATE leads
            SET gross_commission_income_cents = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (amount_cents, _now(), lead_id, user_id),
        )
    return amount_cents, None


def get_source_roi_dashboard(user_id):
    with get_db() as conn:
        source_rows = conn.execute(
            """
            SELECT id, name, category, provider_key, active
            FROM external_lead_sources
            WHERE user_id = ?
            ORDER BY name ASC
            """,
            (user_id,),
        ).fetchall()
        lead_rows = conn.execute(
            """
            SELECT id, external_source_id, name, status,
                   COALESCE(gross_commission_income_cents, 0) AS gci_cents
            FROM leads
            WHERE user_id = ? AND external_source_id IS NOT NULL
            """,
            (user_id,),
        ).fetchall()
        conversation_rows = conn.execute(
            """
            SELECT DISTINCT l.id, l.external_source_id
            FROM leads l
            WHERE l.user_id = ? AND l.external_source_id IS NOT NULL
              AND (
                EXISTS (
                  SELECT 1 FROM sms_messages sm
                  WHERE sm.user_id = l.user_id AND sm.lead_id = l.id
                    AND (sm.direction = 'inbound' OR sm.status = 'received')
                )
                OR EXISTS (
                  SELECT 1 FROM voice_calls vc
                  WHERE vc.user_id = l.user_id AND vc.lead_id = l.id
                    AND vc.status = 'completed'
                )
                OR EXISTS (
                  SELECT 1 FROM lead_activities la
                  WHERE la.user_id = l.user_id AND la.lead_id = l.id
                    AND la.event_type IN ('sms_inbound', 'voice_call_connected', 'voice_call_completed')
                )
              )
            """,
            (user_id,),
        ).fetchall()
        appointment_rows = conn.execute(
            """
            SELECT l.external_source_id, a.id AS appointment_id
            FROM appointments a
            JOIN leads l ON l.id = a.lead_id AND l.user_id = a.user_id
            WHERE a.user_id = ? AND l.external_source_id IS NOT NULL
              AND COALESCE(a.status, '') != 'cancelled'
            """,
            (user_id,),
        ).fetchall()
        spend_rows = conn.execute(
            """
            SELECT external_source_id, COALESCE(SUM(amount_cents), 0) AS cost_cents
            FROM external_lead_source_spend
            WHERE user_id = ?
            GROUP BY external_source_id
            """,
            (user_id,),
        ).fetchall()
        recent_spend = conn.execute(
            """
            SELECT sp.id, sp.external_source_id, sp.amount_cents, sp.spent_on, sp.notes,
                   src.name AS source_name
            FROM external_lead_source_spend sp
            JOIN external_lead_sources src
              ON src.id = sp.external_source_id AND src.user_id = sp.user_id
            WHERE sp.user_id = ?
            ORDER BY sp.spent_on DESC, sp.id DESC
            LIMIT 12
            """,
            (user_id,),
        ).fetchall()

    metrics = {}
    for row in source_rows:
        source = dict(row)
        metrics[source["id"]] = {
            **source,
            "lead_label": "Prospects" if source.get("provider_key") == "redx" else "Leads",
            "client_label": "Listings / clients" if source.get("provider_key") == "redx" else "Clients",
            "leads": 0,
            "conversations": 0,
            "appointments": 0,
            "clients": 0,
            "closings": 0,
            "gci_cents": 0,
            "cost_cents": 0,
            "closed_leads": [],
        }

    for row in lead_rows:
        item = metrics.get(row["external_source_id"])
        if not item:
            continue
        item["leads"] += 1
        if row["status"] in {"under_contract", "closed_won"}:
            item["clients"] += 1
        if row["status"] == "closed_won":
            item["closings"] += 1
            item["gci_cents"] += int(row["gci_cents"] or 0)
            item["closed_leads"].append(
                {"id": row["id"], "name": row["name"] or "Lead", "gci_cents": int(row["gci_cents"] or 0)}
            )

    for row in conversation_rows:
        if row["external_source_id"] in metrics:
            metrics[row["external_source_id"]]["conversations"] += 1
    for row in appointment_rows:
        if row["external_source_id"] in metrics:
            metrics[row["external_source_id"]]["appointments"] += 1
    for row in spend_rows:
        if row["external_source_id"] in metrics:
            metrics[row["external_source_id"]]["cost_cents"] = int(row["cost_cents"] or 0)

    source_metrics = []
    for item in metrics.values():
        cost = item["cost_cents"]
        item["roi_multiple"] = (item["gci_cents"] / cost) if cost else None
        item["close_rate"] = (item["closings"] / item["leads"] * 100) if item["leads"] else None
        item["cost_per_closing_cents"] = (cost / item["closings"]) if item["closings"] else None
        source_metrics.append(item)

    eligible = [m for m in source_metrics if m["roi_multiple"] is not None and m["closings"] > 0]
    recommendation = max(eligible, key=lambda m: m["roi_multiple"]) if eligible else None
    if recommendation:
        recommendation = dict(recommendation)
        recommendation["projected_gci_cents"] = int(round(recommendation["roi_multiple"] * 100000))

    return {
        "sources": source_metrics,
        "recommendation": recommendation,
        "recent_spend": [dict(row) for row in recent_spend],
        "total_gci_cents": sum(m["gci_cents"] for m in source_metrics),
        "total_cost_cents": sum(m["cost_cents"] for m in source_metrics),
    }
