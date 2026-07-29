"""DB helpers for tenant senders, attestations, suppression, campaigns, jobs."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from db import get_db, get_lead
from db_backend import bind_bool, sql_is_true


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row(row):
    return dict(row) if row else None


def message_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# --- Tenant senders ---


def get_active_sender(user_id):
    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT * FROM tenant_sms_senders
            WHERE user_id = ? AND {sql_is_true("sms_enabled")}
              AND registration_status = 'verified'
            ORDER BY id DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        return _row(row)


def get_sender_by_number(sender_number):
    digits = "".join(c for c in (sender_number or "") if c.isdigit())
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM tenant_sms_senders WHERE {sql_is_true('sms_enabled')}"
        ).fetchall()
    for row in rows:
        sn = "".join(c for c in (row["sender_number"] or "") if c.isdigit())
        if sn and (sn == digits or sn.endswith(digits) or digits.endswith(sn)):
            return dict(row)
    return None


def upsert_tenant_sender(
    user_id,
    *,
    sender_number,
    sms_provider="simpletexting",
    provider_number_id=None,
    provider_account_reference=None,
    sms_enabled=False,
    registration_status="pending",
    metadata=None,
):
    now = _now()
    meta = json.dumps(metadata) if isinstance(metadata, dict) else metadata
    enabled = bind_bool(sms_enabled)
    existing = None
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM tenant_sms_senders WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE tenant_sms_senders
                SET sender_number = ?, sms_provider = ?, provider_number_id = ?,
                    provider_account_reference = ?, sms_enabled = ?,
                    registration_status = ?, metadata_json = ?, updated_at = ?,
                    activated_at = CASE WHEN ? AND activated_at IS NULL THEN ? ELSE activated_at END
                WHERE user_id = ?
                """,
                (
                    sender_number,
                    sms_provider,
                    provider_number_id,
                    provider_account_reference,
                    enabled,
                    registration_status,
                    meta,
                    now,
                    enabled,
                    now,
                    user_id,
                ),
            )
            return existing["id"]
        cur = conn.execute(
            """
            INSERT INTO tenant_sms_senders
                (user_id, sms_provider, sender_number, provider_number_id,
                 provider_account_reference, sms_enabled, registration_status,
                 activated_at, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                sms_provider,
                sender_number,
                provider_number_id,
                provider_account_reference,
                enabled,
                registration_status,
                now if sms_enabled else None,
                meta,
                now,
                now,
            ),
        )
        return cur.lastrowid


def ensure_telnyx_platform_sender(user_id):
    """
    Assign the platform Telnyx From number to this subscriber without creating duplicates.
    Upserts the single tenant_sms_senders row for user_id.
    """
    import config

    phone = (config.TELNYX_PHONE_NUMBER or "").strip()
    if not phone:
        return None
    return upsert_tenant_sender(
        user_id,
        sender_number=phone,
        sms_provider="telnyx",
        provider_number_id=(config.TELNYX_MESSAGING_PROFILE_ID or "").strip() or None,
        provider_account_reference="platform",
        sms_enabled=True,
        registration_status="verified",
        metadata={
            "source": "telnyx_platform",
            "messaging_profile_id": (config.TELNYX_MESSAGING_PROFILE_ID or "").strip() or None,
        },
    )


def list_tenant_senders(user_id=None):
    with get_db() as conn:
        if user_id:
            rows = conn.execute(
                "SELECT * FROM tenant_sms_senders WHERE user_id = ? ORDER BY id DESC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tenant_sms_senders ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


# --- Suppression ---


def is_suppressed(user_id, phone_number):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM sms_suppression_list
            WHERE user_id = ? AND phone_number = ?
            LIMIT 1
            """,
            (user_id, phone_number),
        ).fetchone()
        return bool(row)


def add_suppression(user_id, phone_number, *, reason="opted_out", source="system", lead_id=None):
    now = _now()
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM sms_suppression_list WHERE user_id = ? AND phone_number = ?",
            (user_id, phone_number),
        ).fetchone()
        if existing:
            return existing["id"]
        cur = conn.execute(
            """
            INSERT INTO sms_suppression_list
                (user_id, phone_number, reason, source, lead_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, phone_number, reason, source, lead_id, now),
        )
        return cur.lastrowid


# --- Attestations ---


def create_subscriber_attestation(
    user_id,
    actor_user_id,
    lead_id,
    *,
    message_purpose,
    message_body,
    source_page,
    provider,
    campaign_id=None,
    text_version=None,
):
    import config

    now = _now()
    version = text_version or config.SMS_CERT_TEXT_VERSION_ONE_TO_ONE
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sms_subscriber_attestations
                (user_id, actor_user_id, lead_id, campaign_id, message_purpose,
                 message_hash, certification_text_version, source_page, provider,
                 certified_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                actor_user_id,
                lead_id,
                campaign_id,
                message_purpose,
                message_hash(message_body),
                version,
                source_page,
                provider,
                now,
                now,
            ),
        )
        return cur.lastrowid


def latest_attestation_for_lead(user_id, lead_id, *, message_body=None, purpose=None):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM sms_subscriber_attestations
            WHERE user_id = ? AND lead_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, lead_id),
        ).fetchone()
    if not row:
        return None
    att = dict(row)
    if message_body is not None and att.get("message_hash") != message_hash(message_body):
        return None
    if purpose and att.get("message_purpose") != purpose:
        return None
    return att


def create_campaign_attestation(
    user_id,
    actor_user_id,
    campaign_id,
    *,
    eligible_count,
    excluded_count,
    campaign_purpose,
    message_body,
    audience_snapshot_id,
    provider,
    scheduled_launch_at=None,
):
    import config

    now = _now()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sms_campaign_attestations
                (user_id, actor_user_id, campaign_id, eligible_count, excluded_count,
                 campaign_purpose, message_hash, audience_snapshot_id,
                 certification_text_version, provider, scheduled_launch_at,
                 certified_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                actor_user_id,
                campaign_id,
                eligible_count,
                excluded_count,
                campaign_purpose,
                message_hash(message_body),
                audience_snapshot_id,
                config.SMS_CERT_TEXT_VERSION_CAMPAIGN,
                provider,
                scheduled_launch_at,
                now,
                now,
            ),
        )
        return cur.lastrowid


def get_valid_campaign_attestation(user_id, campaign_id, *, message_body, audience_snapshot_id, purpose):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM sms_campaign_attestations
            WHERE user_id = ? AND campaign_id = ? AND invalidated_at IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, campaign_id),
        ).fetchone()
    if not row:
        return None
    att = dict(row)
    if att.get("message_hash") != message_hash(message_body):
        return None
    if att.get("audience_snapshot_id") != audience_snapshot_id:
        return None
    if att.get("campaign_purpose") != purpose:
        return None
    return att


def invalidate_campaign_attestations(user_id, campaign_id):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE sms_campaign_attestations
            SET invalidated_at = ?
            WHERE user_id = ? AND campaign_id = ? AND invalidated_at IS NULL
            """,
            (_now(), user_id, campaign_id),
        )


# --- Terms ---


def has_accepted_sms_terms(user_id, terms_version=None):
    import config

    version = terms_version or config.SMS_TERMS_VERSION
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM sms_terms_acceptances
            WHERE user_id = ? AND terms_version = ?
            LIMIT 1
            """,
            (user_id, version),
        ).fetchone()
        return bool(row)


def accept_sms_terms(user_id, actor_user_id, *, ip_address=None, user_agent=None, terms_version=None):
    import config

    version = terms_version or config.SMS_TERMS_VERSION
    now = _now()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sms_terms_acceptances
                (user_id, actor_user_id, terms_version, ip_address, user_agent, accepted_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, actor_user_id, version, ip_address, user_agent, now),
        )
        return cur.lastrowid


# --- Audit ---


def append_sms_audit(
    user_id,
    action,
    *,
    actor_user_id=None,
    campaign_id=None,
    lead_id=None,
    previous_value=None,
    new_value=None,
    metadata=None,
):
    meta = json.dumps(metadata) if isinstance(metadata, dict) else metadata
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO sms_audit_events
                (user_id, actor_user_id, campaign_id, lead_id, action,
                 previous_value, new_value, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                actor_user_id,
                campaign_id,
                lead_id,
                action,
                previous_value,
                new_value,
                meta,
                _now(),
            ),
        )


# --- Campaigns ---


def create_campaign(user_id, title, **fields):
    now = _now()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sms_campaigns
                (user_id, title, status, message_template, merge_defaults_json,
                 campaign_purpose, sender_number, scheduled_at, content_fingerprint,
                 audience_snapshot_id, test_mode, limits_json, stats_json,
                 created_at, updated_at)
            VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                title,
                fields.get("message_template"),
                json.dumps(fields.get("merge_defaults") or {}),
                fields.get("campaign_purpose"),
                fields.get("sender_number"),
                fields.get("scheduled_at"),
                fields.get("content_fingerprint"),
                fields.get("audience_snapshot_id") or str(uuid.uuid4()),
                1 if fields.get("test_mode") else 0,
                json.dumps(fields.get("limits") or {}),
                json.dumps({}),
                now,
                now,
            ),
        )
        return cur.lastrowid


def get_campaign(campaign_id, user_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sms_campaigns WHERE id = ? AND user_id = ?",
            (campaign_id, user_id),
        ).fetchone()
        return _row(row)


def list_campaigns(user_id, limit=50):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM sms_campaigns WHERE user_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def update_campaign(campaign_id, user_id, **fields):
    allowed = {
        "title",
        "status",
        "message_template",
        "merge_defaults_json",
        "campaign_purpose",
        "sender_number",
        "scheduled_at",
        "started_at",
        "completed_at",
        "content_fingerprint",
        "audience_snapshot_id",
        "attestation_id",
        "test_mode",
        "limits_json",
        "stats_json",
    }
    updates = []
    values = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key.endswith("_json") and isinstance(value, (dict, list)):
            value = json.dumps(value)
        if key == "test_mode":
            value = bind_bool(value)
        updates.append(f"{key} = ?")
        values.append(value)
    if not updates:
        return False
    updates.append("updated_at = ?")
    values.append(_now())
    values.extend([campaign_id, user_id])
    with get_db() as conn:
        conn.execute(
            f"UPDATE sms_campaigns SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
            values,
        )
    return True


def replace_campaign_recipients(campaign_id, user_id, recipients):
    """recipients: list of dicts with phone_number, lead_id?, merge_fields?, eligible?, exclusion_reason?"""
    now = _now()
    with get_db() as conn:
        conn.execute(
            "DELETE FROM sms_campaign_recipients WHERE campaign_id = ? AND user_id = ?",
            (campaign_id, user_id),
        )
        for r in recipients:
            conn.execute(
                """
                INSERT INTO sms_campaign_recipients
                    (campaign_id, user_id, lead_id, phone_number, merge_fields_json,
                     exclusion_reason, eligible, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    user_id,
                    r.get("lead_id"),
                    r["phone_number"],
                    json.dumps(r.get("merge_fields") or {}),
                    r.get("exclusion_reason"),
                    bind_bool(r.get("eligible", True)),
                    now,
                ),
            )
    # New audience requires new snapshot id + invalidate attestations
    snapshot = str(uuid.uuid4())
    update_campaign(campaign_id, user_id, audience_snapshot_id=snapshot)
    invalidate_campaign_attestations(user_id, campaign_id)
    return snapshot


def list_campaign_recipients(campaign_id, user_id, eligible_only=False):
    with get_db() as conn:
        sql = "SELECT * FROM sms_campaign_recipients WHERE campaign_id = ? AND user_id = ?"
        params = [campaign_id, user_id]
        if eligible_only:
            sql += f" AND {sql_is_true('eligible')}"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def create_jobs_for_campaign(campaign_id, user_id):
    recipients = list_campaign_recipients(campaign_id, user_id, eligible_only=True)
    now = _now()
    created = 0
    with get_db() as conn:
        for r in recipients:
            key = f"c{campaign_id}-r{r['id']}-{r['phone_number']}"
            try:
                conn.execute(
                    """
                    INSERT INTO sms_campaign_jobs
                        (campaign_id, user_id, recipient_id, lead_id, phone_number,
                         status, idempotency_key, attempts, next_attempt_at,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?, 0, ?, ?, ?)
                    """,
                    (
                        campaign_id,
                        user_id,
                        r["id"],
                        r.get("lead_id"),
                        r["phone_number"],
                        key,
                        now,
                        now,
                        now,
                    ),
                )
                created += 1
            except Exception:
                continue
    return created


def claim_next_job(worker_id):
    """Atomically claim one pending job. SQLite uses immediate transaction; PG uses SKIP LOCKED."""
    import config

    now = _now()
    with get_db() as conn:
        if config.DB_ENGINE == "postgres":
            row = conn.execute(
                """
                UPDATE sms_campaign_jobs
                SET status = 'claimed', claimed_at = %s, claimed_by = %s, updated_at = %s,
                    attempts = attempts + 1
                WHERE id = (
                    SELECT j.id FROM sms_campaign_jobs j
                    JOIN sms_campaigns c ON c.id = j.campaign_id
                    WHERE j.status = 'pending'
                      AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= %s)
                      AND c.status IN ('processing', 'scheduled')
                    ORDER BY j.id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING *
                """,
                (now, worker_id, now, now),
            ).fetchone()
            return _row(row)

        row = conn.execute(
            """
            SELECT j.* FROM sms_campaign_jobs j
            JOIN sms_campaigns c ON c.id = j.campaign_id
            WHERE j.status = 'pending'
              AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= ?)
              AND c.status IN ('processing', 'scheduled')
            ORDER BY j.id LIMIT 1
            """,
            (now,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """
            UPDATE sms_campaign_jobs
            SET status = 'claimed', claimed_at = ?, claimed_by = ?,
                attempts = attempts + 1, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (now, worker_id, now, row["id"]),
        )
        return _row(
            conn.execute("SELECT * FROM sms_campaign_jobs WHERE id = ?", (row["id"],)).fetchone()
        )


def update_job(job_id, **fields):
    allowed = {
        "status",
        "provider_message_id",
        "sms_message_id",
        "failure_code",
        "failure_message",
        "submitted_at",
        "delivered_at",
        "failed_at",
        "next_attempt_at",
        "claimed_at",
        "claimed_by",
    }
    updates = []
    values = []
    for k, v in fields.items():
        if k in allowed:
            updates.append(f"{k} = ?")
            values.append(v)
    if not updates:
        return
    updates.append("updated_at = ?")
    values.append(_now())
    values.append(job_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE sms_campaign_jobs SET {', '.join(updates)} WHERE id = ?",
            values,
        )


def count_jobs_by_status(campaign_id, user_id):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count FROM sms_campaign_jobs
            WHERE campaign_id = ? AND user_id = ?
            GROUP BY status
            """,
            (campaign_id, user_id),
        ).fetchall()
        return {r["status"]: r["count"] for r in rows}


def create_tracking_link(user_id, destination_url, *, campaign_id=None, lead_id=None):
    token = secrets.token_urlsafe(16)
    now = _now()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO sms_link_clicks
                (user_id, campaign_id, lead_id, tracking_token, destination_url,
                 total_clicks, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (user_id, campaign_id, lead_id, token, destination_url, now),
        )
    return token


def record_link_click(token):
    now = _now()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sms_link_clicks WHERE tracking_token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """
            UPDATE sms_link_clicks
            SET total_clicks = total_clicks + 1,
                first_clicked_at = COALESCE(first_clicked_at, ?),
                latest_clicked_at = ?
            WHERE tracking_token = ?
            """,
            (now, now, token),
        )
        return dict(row)


def count_sends_since(user_id, since_iso):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count FROM sms_messages
            WHERE user_id = ? AND direction = 'outbound'
              AND status IN ('queued', 'submitted', 'sent', 'delivered')
              AND created_at >= ?
            """,
            (user_id, since_iso),
        ).fetchone()
        return int(row["count"] if row else 0)


def count_sends_to_contact_since(user_id, phone_number, since_iso):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count FROM sms_messages
            WHERE user_id = ? AND phone_number = ? AND direction = 'outbound'
              AND status IN ('queued', 'submitted', 'sent', 'delivered')
              AND created_at >= ?
            """,
            (user_id, phone_number, since_iso),
        ).fetchone()
        return int(row["count"] if row else 0)


def get_any_telnyx_sender():
    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT * FROM tenant_sms_senders
            WHERE {sql_is_true("sms_enabled")} AND registration_status = 'verified'
              AND (sms_provider = 'telnyx' OR sms_provider IS NULL)
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        return _row(row)


def record_webhook_event(
    *,
    provider,
    provider_event_id,
    event_type,
    tenant_id=None,
    provider_message_id=None,
    processing_status="received",
    safe_metadata=None,
):
    now = _now()
    meta = json.dumps(safe_metadata) if isinstance(safe_metadata, dict) else safe_metadata
    with get_db() as conn:
        try:
            conn.execute(
                """
                INSERT INTO sms_webhook_events
                    (provider, provider_event_id, event_type, tenant_id, provider_message_id,
                     processing_status, received_at, safe_metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider,
                    provider_event_id,
                    event_type,
                    tenant_id,
                    provider_message_id,
                    processing_status,
                    now,
                    meta,
                ),
            )
        except Exception:
            # Unique constraint / table missing during partial migrate
            return None


def get_webhook_event_by_provider_id(provider, provider_event_id):
    if not provider_event_id:
        return None
    with get_db() as conn:
        try:
            row = conn.execute(
                """
                SELECT * FROM sms_webhook_events
                WHERE provider = ? AND provider_event_id = ?
                LIMIT 1
                """,
                (provider, provider_event_id),
            ).fetchone()
            return _row(row)
        except Exception:
            return None


def mark_webhook_processed(provider, provider_event_id, status="processed"):
    if not provider_event_id:
        return
    with get_db() as conn:
        try:
            conn.execute(
                """
                UPDATE sms_webhook_events
                SET processing_status = ?, processed_at = ?
                WHERE provider = ? AND provider_event_id = ?
                """,
                (status, _now(), provider, provider_event_id),
            )
        except Exception:
            pass


def latest_sms_event(user_id=None, *, direction=None, provider=None):
    with get_db() as conn:
        sql = "SELECT * FROM sms_messages WHERE 1=1"
        params = []
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        if direction:
            sql += " AND direction = ?"
            params.append(direction)
        if provider:
            sql += " AND provider = ?"
            params.append(provider)
        sql += " ORDER BY id DESC LIMIT 1"
        return _row(conn.execute(sql, params).fetchone())


def count_pending_campaign_jobs():
    with get_db() as conn:
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM sms_campaign_jobs WHERE status IN ('pending', 'claimed')"
            ).fetchone()
            return int(row["count"] if row else 0)
        except Exception:
            return 0


def is_campaign_worker_available(*, minutes=30):
    """
    Best-effort worker presence check.
    Explicit SMS_CAMPAIGN_WORKER_AVAILABLE overrides auto-detection.
    """
    health = get_campaign_worker_health(stale_seconds=max(60, int(minutes) * 60))
    return health.get("state") == "running"


def get_campaign_worker_health(*, stale_seconds=120):
    """
    Return {state, last_seen_at, worker_id, message} where state is one of:
    running | stale | stopped | unknown
    """
    import config

    raw = (getattr(config, "SMS_CAMPAIGN_WORKER_AVAILABLE", None) or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return {
            "state": "stopped",
            "last_seen_at": None,
            "worker_id": None,
            "message": "Campaign processing worker is currently unavailable.",
        }
    if raw in {"1", "true", "yes", "on"}:
        # Explicit override still prefers a fresh heartbeat when present.
        pass

    with get_db() as conn:
        try:
            row = conn.execute(
                """
                SELECT worker_id, status, last_seen_at
                FROM sms_worker_heartbeats
                ORDER BY last_seen_at DESC
                LIMIT 1
                """
            ).fetchone()
        except Exception:
            row = None

    if not row:
        # Fall back to recent job claim activity for older deploys.
        if has_recent_worker_activity(minutes=max(1, int(stale_seconds / 60))):
            return {
                "state": "running",
                "last_seen_at": None,
                "worker_id": None,
                "message": None,
            }
        if raw in {"1", "true", "yes", "on"}:
            return {
                "state": "unknown",
                "last_seen_at": None,
                "worker_id": None,
                "message": "Campaign worker is marked available but has not reported a heartbeat yet.",
            }
        return {
            "state": "unknown",
            "last_seen_at": None,
            "worker_id": None,
            "message": "Campaign processing worker is currently unavailable.",
        }

    last_seen = row["last_seen_at"]
    worker_id = row["worker_id"]
    try:
        seen_dt = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
        if seen_dt.tzinfo is None:
            seen_dt = seen_dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - seen_dt).total_seconds()
    except Exception:
        age = None

    if age is not None and age <= float(stale_seconds):
        return {
            "state": "running",
            "last_seen_at": last_seen,
            "worker_id": worker_id,
            "message": None,
        }
    if age is not None:
        return {
            "state": "stale",
            "last_seen_at": last_seen,
            "worker_id": worker_id,
            "message": "Campaign processing worker heartbeat is stale.",
        }
    return {
        "state": "unknown",
        "last_seen_at": last_seen,
        "worker_id": worker_id,
        "message": "Campaign processing worker status is unknown.",
    }


def touch_worker_heartbeat(worker_id, *, status="running", metadata=None):
    import logging

    now = _now()
    meta = json.dumps(metadata) if isinstance(metadata, dict) else metadata
    log = logging.getLogger(__name__)
    with get_db() as conn:
        try:
            existing = conn.execute(
                "SELECT worker_id FROM sms_worker_heartbeats WHERE worker_id = ? LIMIT 1",
                (worker_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE sms_worker_heartbeats
                    SET status = ?, last_seen_at = ?, metadata_json = ?
                    WHERE worker_id = ?
                    """,
                    (status, now, meta, worker_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO sms_worker_heartbeats
                        (worker_id, status, last_seen_at, metadata_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (worker_id, status, now, meta),
                )
            return True
        except Exception:
            log.exception("Failed to write sms_worker_heartbeat worker_id=%s", worker_id)
            return False


def has_recent_worker_activity(*, minutes=30):
    """True if any campaign job was claimed within the lookback window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=int(minutes))).isoformat()
    with get_db() as conn:
        try:
            row = conn.execute(
                """
                SELECT 1 AS ok FROM sms_campaign_jobs
                WHERE claimed_at IS NOT NULL AND claimed_at >= ?
                LIMIT 1
                """,
                (cutoff,),
            ).fetchone()
            return bool(row)
        except Exception:
            return False
