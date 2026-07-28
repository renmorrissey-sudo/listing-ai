"""DB helpers for external lead sources, consent evidence, ponds, and audit."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from db import get_db, get_lead, mark_lead_opt_out


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row(row):
    return dict(row) if row else None


def create_external_lead_source(
    user_id,
    *,
    name,
    category="other",
    provider_key,
    import_method="manual",
    default_lead_type=None,
    default_lead_status="new",
    default_pond_status="claimable",
    webhook_secret_hash=None,
    metadata_mapping_json=None,
):
    now = _now()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO external_lead_sources
                (user_id, name, category, provider_key, active, import_method,
                 consent_behavior, default_lead_type, default_lead_status,
                 default_pond_status, webhook_secret_hash, metadata_mapping_json,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, 'unverified_blocked', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                name,
                category,
                provider_key,
                import_method,
                default_lead_type,
                default_lead_status,
                default_pond_status,
                webhook_secret_hash,
                metadata_mapping_json,
                now,
                now,
            ),
        )
        return cur.lastrowid


def list_external_lead_sources(user_id, active_only=False):
    with get_db() as conn:
        if active_only:
            rows = conn.execute(
                """
                SELECT * FROM external_lead_sources
                WHERE user_id = ? AND active = 1
                ORDER BY name ASC
                """,
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM external_lead_sources
                WHERE user_id = ?
                ORDER BY name ASC
                """,
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_external_lead_source(source_id, user_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM external_lead_sources WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        return _row(row)


def get_external_lead_source_by_key(user_id, provider_key):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM external_lead_sources
            WHERE user_id = ? AND provider_key = ? AND active = 1
            LIMIT 1
            """,
            (user_id, provider_key),
        ).fetchone()
        return _row(row)


def find_source_by_webhook_provider_key(provider_key):
    """Locate active source by provider_key (tenant resolved via secret)."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM external_lead_sources
            WHERE provider_key = ? AND active = 1
            """,
            (provider_key,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_source_webhook_secret(source_id, user_id, secret_hash):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE external_lead_sources
            SET webhook_secret_hash = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (secret_hash, _now(), source_id, user_id),
        )


def create_import_batch(user_id, external_source_id=None, filename=None):
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO external_lead_import_batches
                (user_id, external_source_id, filename, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, external_source_id, filename, _now()),
        )
        return cur.lastrowid


def finish_import_batch(batch_id, user_id, stats):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE external_lead_import_batches
            SET created_count = ?, updated_count = ?, skipped_count = ?,
                invalid_count = ?, pending_evidence_count = ?, error_summary = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                stats.get("created", 0),
                stats.get("updated", 0),
                stats.get("skipped", 0),
                stats.get("invalid", 0),
                stats.get("pending_evidence", 0),
                (stats.get("error_summary") or "")[:2000] or None,
                batch_id,
                user_id,
            ),
        )


def find_lead_by_external_record(user_id, external_source_id, external_record_id):
    if not external_source_id or not external_record_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM leads
            WHERE user_id = ? AND external_source_id = ? AND external_record_id = ?
            LIMIT 1
            """,
            (user_id, external_source_id, external_record_id),
        ).fetchone()
        return _row(row)


def find_lead_by_email(user_id, email):
    if not email:
        return None
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM leads
            WHERE user_id = ? AND lower(email) = lower(?)
            LIMIT 1
            """,
            (user_id, email.strip()),
        ).fetchone()
        return _row(row)


def create_external_lead(
    user_id,
    *,
    phone_number,
    name,
    email=None,
    lead_type=None,
    status="new",
    source="external",
    notes=None,
    external_source_id=None,
    external_record_id=None,
    external_payload_meta=None,
    pond_status="claimable",
    import_batch_id=None,
    property_interest=None,
):
    """Always creates with not_certified + blocked SMS consent."""
    now = _now()
    meta = external_payload_meta
    if isinstance(meta, (dict, list)):
        meta = json.dumps(meta)
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO leads
                (user_id, name, phone_number, email, lead_type, property_interest, status, source,
                 notes, assigned_user_id, created_at, updated_at,
                 sms_consent_status, sms_sending_blocked, external_source_id, external_record_id,
                 external_payload_meta, pond_status, import_batch_id, consent_status, opt_out_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'not_certified', 1, ?, ?, ?, ?, ?, 'unknown', 'active')
            """,
            (
                user_id,
                name or "Lead",
                phone_number,
                email,
                lead_type,
                property_interest,
                status or "new",
                source,
                notes,
                user_id,
                now,
                now,
                external_source_id,
                external_record_id,
                meta,
                pond_status or "claimable",
                import_batch_id,
            ),
        )
        return cur.lastrowid


def update_external_lead_fields(lead_id, user_id, **fields):
    """Update non-consent fields on duplicate match. Never changes consent/opt-out."""
    allowed = {
        "name",
        "email",
        "notes",
        "lead_type",
        "property_interest",
        "external_payload_meta",
        "external_record_id",
        "import_batch_id",
    }
    lead = get_lead(lead_id, user_id)
    if not lead:
        return False
    # Never restore SMS for opted_out/revoked
    updates = []
    values = []
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key == "external_payload_meta" and isinstance(value, (dict, list)):
            value = json.dumps(value)
        if key == "notes" and value:
            existing = lead.get("notes") or ""
            if value not in existing:
                value = (existing + "\n" + value).strip() if existing else value
        updates.append(f"{key} = ?")
        values.append(value)
    if not updates:
        return False
    updates.append("updated_at = ?")
    values.append(_now())
    values.extend([lead_id, user_id])
    with get_db() as conn:
        conn.execute(
            f"UPDATE leads SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
            tuple(values),
        )
    return True


def set_lead_sms_consent_state(
    lead_id,
    user_id,
    *,
    sms_consent_status,
    sms_sending_blocked,
    sync_legacy=True,
    actor_user_id=None,
    source="system",
    metadata=None,
):
    lead = get_lead(lead_id, user_id)
    if not lead:
        return False
    prev = {
        "sms_consent_status": lead.get("sms_consent_status"),
        "sms_sending_blocked": bool(lead.get("sms_sending_blocked")),
    }
    blocked_int = 1 if sms_sending_blocked else 0
    legacy_consent = lead.get("consent_status")
    legacy_opt = lead.get("opt_out_status")
    if sync_legacy:
        if sms_consent_status in {"verified", "user_certified"} and not sms_sending_blocked:
            legacy_consent = "confirmed"
            legacy_opt = "active"
        elif sms_consent_status == "opted_out":
            legacy_consent = lead.get("consent_status") or "unknown"
            legacy_opt = "opted_out"
        elif sms_consent_status in {
            "revoked",
            "not_permitted",
            "unverified",
            "not_certified",
            "suppressed",
            "invalid_number",
        }:
            if sms_consent_status == "revoked":
                legacy_consent = "revoked"
            legacy_opt = legacy_opt or "active"
    with get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET sms_consent_status = ?,
                sms_sending_blocked = ?,
                consent_status = ?,
                opt_out_status = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                sms_consent_status,
                blocked_int,
                legacy_consent,
                legacy_opt,
                _now(),
                lead_id,
                user_id,
            ),
        )
    append_consent_audit(
        user_id,
        lead_id,
        actor_user_id=actor_user_id or user_id,
        action="consent_state_changed",
        previous_value=json.dumps(prev),
        new_value=json.dumps(
            {
                "sms_consent_status": sms_consent_status,
                "sms_sending_blocked": bool(sms_sending_blocked),
            }
        ),
        source=source,
        metadata=metadata,
    )
    return True


def append_consent_audit(
    user_id,
    lead_id,
    *,
    action,
    actor_user_id=None,
    previous_value=None,
    new_value=None,
    source=None,
    metadata=None,
):
    meta = metadata
    if isinstance(meta, (dict, list)):
        meta = json.dumps(meta)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO consent_audit_events
                (user_id, lead_id, actor_user_id, action, previous_value, new_value,
                 source, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                lead_id,
                actor_user_id,
                action,
                previous_value,
                new_value,
                source,
                meta,
                _now(),
            ),
        )


def list_consent_audit(user_id, lead_id, limit=100):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM consent_audit_events
            WHERE user_id = ? AND lead_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, lead_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def create_consent_evidence(user_id, lead_id, data):
    now = _now()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sms_consent_evidence
                (user_id, lead_id, consent_status, consent_method, source_provider,
                 source_record_id, source_url, consent_at, recorded_at,
                 authorized_agent_name, authorized_brokerage_name, phone_number,
                 communication_purpose, disclosure_text, disclosure_version,
                 evidence_type, upload_ref, notes, confirmed_by_user_id, confirmed_at,
                 revoked_at, attestation_accepted, audit_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                lead_id,
                data.get("consent_status") or "pending",
                data.get("consent_method"),
                data.get("source_provider"),
                data.get("source_record_id"),
                data.get("source_url"),
                data.get("consent_at"),
                data.get("recorded_at") or now,
                data.get("authorized_agent_name"),
                data.get("authorized_brokerage_name"),
                data.get("phone_number"),
                data.get("communication_purpose"),
                data.get("disclosure_text"),
                data.get("disclosure_version"),
                data.get("evidence_type"),
                data.get("upload_ref"),
                data.get("notes"),
                data.get("confirmed_by_user_id"),
                data.get("confirmed_at"),
                data.get("revoked_at"),
                1 if data.get("attestation_accepted") else 0,
                json.dumps(data.get("audit_json")) if isinstance(data.get("audit_json"), dict) else data.get("audit_json"),
                now,
            ),
        )
        return cur.lastrowid


def get_consent_evidence(evidence_id, user_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sms_consent_evidence WHERE id = ? AND user_id = ?",
            (evidence_id, user_id),
        ).fetchone()
        return _row(row)


def list_consent_evidence(user_id, lead_id, limit=50):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM sms_consent_evidence
            WHERE user_id = ? AND lead_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, lead_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_evidence_confirmed(evidence_id, user_id, confirmer_id):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE sms_consent_evidence
            SET consent_status = 'confirmed',
                confirmed_by_user_id = ?,
                confirmed_at = ?,
                attestation_accepted = 1
            WHERE id = ? AND user_id = ?
            """,
            (confirmer_id, _now(), evidence_id, user_id),
        )


def claim_lead(lead_id, user_id):
    """Claim a pond lead. Does not change SMS consent."""
    lead = get_lead(lead_id, user_id)
    if not lead:
        return None, "Lead not found."
    if (lead.get("pond_status") or "") not in {"unassigned", "claimable"}:
        return None, "Lead is not available to claim."
    now = _now()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET pond_status = 'claimed',
                claimed_at = ?,
                claimed_by_user_id = ?,
                assigned_user_id = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, user_id, user_id, now, lead_id, user_id),
        )
    append_consent_audit(
        user_id,
        lead_id,
        actor_user_id=user_id,
        action="lead_claimed",
        previous_value=lead.get("pond_status"),
        new_value="claimed",
        source="pond",
    )
    return get_lead(lead_id, user_id), None


def apply_opt_out_consent(lead_id, user_id, *, actor_user_id=None, source="sms_keyword"):
    """Opt-out: mark opted_out + blocked; preserve across imports."""
    mark_lead_opt_out(lead_id, user_id)
    set_lead_sms_consent_state(
        lead_id,
        user_id,
        sms_consent_status="opted_out",
        sms_sending_blocked=True,
        actor_user_id=actor_user_id or user_id,
        source=source,
    )
