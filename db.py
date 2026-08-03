from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import config
from db_backend import bind_bool, connect as backend_connect


def _connect():
    return backend_connect()


@contextmanager
def get_db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Apply forward-only migrations and idempotent product defaults.

    Never rebuilds schema from scratch, never drops tables, never runs demo seeds
    in production/staging.
    """
    from migrations.runner import apply_pending_migrations

    apply_pending_migrations()
    with get_db() as conn:
        _ensure_default_voice_personas(conn)
        if config.RUN_DEMO_SEED_ON_STARTUP:
            if config.APP_ENV in {"production", "staging"}:
                raise RuntimeError("Demo seed refused in production/staging.")
            _run_demo_seed(conn)


def _run_demo_seed(conn):
    """Development-only sample data. Never called in production."""
    # Intentionally empty: do not insert fake paid-user records automatically.
    return


def _ensure_default_voice_personas(conn):
    existing = conn.execute("SELECT COUNT(*) AS count FROM voice_personas WHERE is_default = 1").fetchone()
    if existing and existing["count"]:
        return
    now = datetime.now(timezone.utc).isoformat()
    personas = [
        (
            "ISA / New Lead Follow-up",
            "isa",
            "You are an AI calling assistant for a real estate agent. Your job is to quickly respond to a new lead, qualify their needs, answer basic objections, and ask for a short appointment with the agent.",
            "friendly, confident, and concise",
            "Qualify the lead and request an appointment with the agent.",
            "If the lead is hesitant, acknowledge the concern, ask one helpful question, and offer a low-pressure next step.",
        ),
        (
            "Open House Follow-up",
            "open_house",
            "You are an AI calling assistant following up after an open house. Your job is to thank the visitor, learn what they thought of the property, ask where they are in their home search, and offer to schedule a showing or buyer consultation.",
            "warm, helpful, and conversational",
            "Understand buyer interest and ask for a showing or consultation.",
            "If the lead is just browsing, offer to send similar listings and ask what criteria matter most.",
        ),
    ]
    for name, persona_type, prompt, tone, goal, objection_notes in personas:
        conn.execute(
            """
            INSERT INTO voice_personas
                (user_id, name, persona_type, prompt, tone, goal, objection_handling_notes, is_default, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?)
            """,
            (None, name, persona_type, prompt, tone, goal, objection_notes, now),
        )


def create_user(email, password_hash, password_set=True):
    now = datetime.now(timezone.utc).isoformat()
    import config

    email_n = email.lower().strip()
    with get_db() as conn:
        # Idempotent: do not create a second row for the same email.
        existing = conn.execute(
            "SELECT id FROM users WHERE email = ?", (email_n,)
        ).fetchone()
        if existing:
            return existing["id"] if hasattr(existing, "keys") else existing[0]
        if config.DB_ENGINE == "postgres":
            pw_set = bool(password_set)
            cur = conn.execute(
                """
                INSERT INTO users (email, password_hash, created_at, password_set, session_version)
                VALUES (?, ?, ?, ?, 1)
                """,
                (email_n, password_hash, now, pw_set),
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO users (email, password_hash, created_at, password_set, session_version)
                VALUES (?, ?, ?, ?, 1)
                """,
                (email_n, password_hash, now, 1 if password_set else 0),
            )
        return cur.lastrowid


def get_user_by_email(email):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def update_user_password(user_id, password_hash, password_set=True):
    import config

    with get_db() as conn:
        if config.DB_ENGINE == "postgres":
            conn.execute(
                """
                UPDATE users
                SET password_hash = ?, password_set = ?
                WHERE id = ?
                """,
                (password_hash, bool(password_set), user_id),
            )
        else:
            conn.execute(
                """
                UPDATE users
                SET password_hash = ?, password_set = ?
                WHERE id = ?
                """,
                (password_hash, 1 if password_set else 0, user_id),
            )


def bump_user_session_version(user_id):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE users
            SET session_version = COALESCE(session_version, 1) + 1
            WHERE id = ?
            """,
            (user_id,),
        )


def create_password_reset_token(*, email, token_hash, user_id, expires_at, created_at):
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO password_reset_tokens
                (email, user_id, token_hash, expires_at, used_at, created_at)
            VALUES (?, ?, ?, ?, NULL, ?)
            """,
            (email.lower().strip(), user_id, token_hash, expires_at, created_at),
        )
        return cur.lastrowid


def get_password_reset_token_by_hash(token_hash):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM password_reset_tokens WHERE token_hash = ? LIMIT 1",
            (token_hash,),
        ).fetchone()
        return dict(row) if row else None


def mark_password_reset_token_used(token_id):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE password_reset_tokens
            SET used_at = ?
            WHERE id = ? AND used_at IS NULL
            """,
            (now, token_id),
        )


def invalidate_password_reset_tokens_for_email(email):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE password_reset_tokens
            SET used_at = COALESCE(used_at, ?)
            WHERE email = ? AND used_at IS NULL
            """,
            (now, email.lower().strip()),
        )


def get_user_by_stripe_customer(customer_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,)
        ).fetchone()
        return dict(row) if row else None


def update_user_subscription(user_id, status, subscription_id=None, stripe_customer_id=None):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE users
            SET subscription_status = ?,
                subscription_id = COALESCE(?, subscription_id),
                stripe_customer_id = COALESCE(?, stripe_customer_id)
            WHERE id = ?
            """,
            (status, subscription_id, stripe_customer_id, user_id),
        )


def set_stripe_customer(user_id, stripe_customer_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET stripe_customer_id = ? WHERE id = ?",
            (stripe_customer_id, user_id),
        )


def claim_stripe_webhook_event(event_id, event_type):
    """Record a Stripe event for idempotent processing.

    Returns True if this is the first time seeing the event (caller should process),
    False if it was already processed.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        existing = conn.execute(
            "SELECT event_id FROM stripe_webhook_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if existing:
            return False
        try:
            conn.execute(
                """
                INSERT INTO stripe_webhook_events (event_id, event_type, processed_at)
                VALUES (?, ?, ?)
                """,
                (event_id, event_type, now),
            )
        except Exception:
            # Concurrent insert race — treat as already processed.
            return False
        return True


def get_business_profile(user_id):
    with get_db() as conn:
        try:
            row = conn.execute(
                """
                SELECT agent_name, brokerage_name, company_name, timezone
                FROM users WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
        except Exception:
            row = conn.execute(
                """
                SELECT agent_name, brokerage_name, company_name
                FROM users WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        return {
            "agent_name": (data.get("agent_name") or "").strip(),
            "brokerage_name": (data.get("brokerage_name") or "").strip(),
            "company_name": (data.get("company_name") or "").strip(),
            "timezone": (data.get("timezone") or "").strip() or "America/Denver",
        }


def update_business_profile(
    user_id, agent_name=None, brokerage_name=None, company_name=None, timezone=None
):
    tz = (timezone or "").strip()[:80] or None
    with get_db() as conn:
        # timezone column is additive (migration 008); keep write resilient.
        try:
            conn.execute(
                """
                UPDATE users
                SET agent_name = ?,
                    brokerage_name = ?,
                    company_name = ?,
                    timezone = COALESCE(?, timezone)
                WHERE id = ?
                """,
                (
                    (agent_name or "").strip()[:120] or None,
                    (brokerage_name or "").strip()[:200] or None,
                    (company_name or "").strip()[:200] or None,
                    tz,
                    user_id,
                ),
            )
        except Exception:
            conn.execute(
                """
                UPDATE users
                SET agent_name = ?,
                    brokerage_name = ?,
                    company_name = ?
                WHERE id = ?
                """,
                (
                    (agent_name or "").strip()[:120] or None,
                    (brokerage_name or "").strip()[:200] or None,
                    (company_name or "").strip()[:200] or None,
                    user_id,
                ),
            )
    return get_business_profile(user_id)


def get_user_timezone(user_id):
    profile = get_business_profile(user_id) or {}
    return profile.get("timezone") or "America/Denver"


def list_voice_personas(user_id=None):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM voice_personas
            WHERE active = 1 AND (is_default = 1 OR user_id = ?)
            ORDER BY is_default DESC, name ASC
            """,
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_voice_persona(persona_id, user_id=None):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM voice_personas
            WHERE id = ? AND active = 1 AND (is_default = 1 OR user_id = ?)
            """,
            (persona_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def create_voice_persona(user_id, data):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO voice_personas
                (user_id, name, persona_type, prompt, tone, goal, objection_handling_notes, is_default, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?)
            """,
            (
                user_id,
                data.get("name"),
                data.get("persona_type"),
                data.get("prompt"),
                data.get("tone"),
                data.get("goal"),
                data.get("objection_handling_notes"),
                now,
            ),
        )
        return cur.lastrowid


def create_voice_call(user_id, persona_id, provider, direction, data):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO voice_calls
                (user_id, persona_id, lead_id, provider, direction, lead_name, phone_number, lead_type,
                 property_interest, desired_outcome, notes, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
            """,
            (
                user_id,
                persona_id,
                data.get("lead_id"),
                provider,
                direction,
                data.get("lead_name"),
                data.get("phone_number"),
                data.get("lead_type"),
                data.get("property_interest"),
                data.get("desired_outcome"),
                data.get("notes") or data.get("lead_context"),
                now,
            ),
        )
        return cur.lastrowid


def set_voice_call_lead_id(call_id, lead_id, user_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE voice_calls SET lead_id = ? WHERE id = ? AND user_id = ?",
            (lead_id, call_id, user_id),
        )


def get_voice_call_by_provider_id(provider_call_id):
    if not provider_call_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM voice_calls WHERE provider_call_id = ? LIMIT 1",
            (provider_call_id,),
        ).fetchone()
        return dict(row) if row else None


def update_voice_call_provider(call_id, provider_call_id, status):
    with get_db() as conn:
        conn.execute(
            "UPDATE voice_calls SET provider_call_id = ?, status = ? WHERE id = ?",
            (provider_call_id, status, call_id),
        )


def update_voice_call_from_webhook(
    call_id=None,
    provider_call_id=None,
    status=None,
    outcome=None,
    transcript=None,
    summary=None,
    recording_url=None,
    stereo_recording_url=None,
    recording_duration_seconds=None,
    recording_status=None,
    transcript_url=None,
    appointment_requested=False,
):
    completed_at = datetime.now(timezone.utc).isoformat()
    appointment_flag = 1 if appointment_requested else 0
    update_sql = """
        UPDATE voice_calls
        SET provider_call_id = COALESCE(?, provider_call_id),
            status = COALESCE(?, status),
            outcome = COALESCE(?, outcome),
            transcript = COALESCE(?, transcript),
            summary = COALESCE(?, summary),
            recording_url = COALESCE(?, recording_url),
            stereo_recording_url = COALESCE(?, stereo_recording_url),
            recording_duration_seconds = COALESCE(?, recording_duration_seconds),
            recording_status = CASE
                WHEN COALESCE(?, recording_url) IS NOT NULL
                  OR COALESCE(?, stereo_recording_url) IS NOT NULL
                  THEN 'available'
                WHEN recording_status = 'available' THEN recording_status
                WHEN ? IS NOT NULL THEN ?
                ELSE recording_status
            END,
            transcript_url = COALESCE(?, transcript_url),
            appointment_requested = CASE WHEN ? = 1 THEN 1 ELSE appointment_requested END,
            completed_at = CASE WHEN ? = 'completed' THEN COALESCE(completed_at, ?) ELSE completed_at END
        WHERE {where_clause}
        """

    params = (
        provider_call_id,
        status,
        outcome,
        transcript,
        summary,
        recording_url,
        stereo_recording_url,
        recording_duration_seconds,
        recording_url,
        stereo_recording_url,
        recording_status,
        recording_status,
        transcript_url,
        appointment_flag,
        status,
        completed_at,
    )

    with get_db() as conn:
        if provider_call_id:
            cur = conn.execute(
                update_sql.format(where_clause="provider_call_id = ?"),
                params + (provider_call_id,),
            )
            if cur.rowcount:
                return cur.rowcount

        if call_id:
            cur = conn.execute(
                update_sql.format(where_clause="id = ?"),
                params + (call_id,),
            )
            return cur.rowcount

        return 0


def voice_call_has_recording(call_row):
    if not call_row:
        return False
    return bool(
        call_row.get("recording_url")
        or call_row.get("stereo_recording_url")
        or call_row.get("recording_status") == "available"
    )


def list_voice_calls_for_lead(user_id, lead_id, limit=50):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT vc.*, vp.name AS persona_name
            FROM voice_calls vc
            LEFT JOIN voice_personas vp ON vp.id = vc.persona_id
            WHERE vc.user_id = ? AND vc.lead_id = ?
            ORDER BY vc.created_at DESC
            LIMIT ?
            """,
            (user_id, lead_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def get_voice_call(call_id, user_id):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT vc.*, vp.name AS persona_name
            FROM voice_calls vc
            LEFT JOIN voice_personas vp ON vp.id = vc.persona_id
            WHERE vc.id = ? AND vc.user_id = ?
            """,
            (call_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def list_voice_calls(user_id, limit=20):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT vc.*, vp.name AS persona_name
            FROM voice_calls vc
            LEFT JOIN voice_personas vp ON vp.id = vc.persona_id
            WHERE vc.user_id = ?
            ORDER BY vc.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def upsert_lead(user_id, phone_number, data=None, source="sms"):
    """Backward-compatible wrapper — prefer lead_service.upsert_crm_lead."""
    import lead_service

    lead_id, _created, _lead = lead_service.upsert_crm_lead(
        user_id,
        phone_number,
        data,
        source=source,
        touch_sms=(source == "sms"),
    )
    return lead_id


def create_lead_record(
    user_id,
    phone_number,
    *,
    name,
    lead_type=None,
    property_interest=None,
    status="new",
    source="sms",
    notes=None,
    assigned_user_id=None,
    touch_call=False,
    touch_sms=False,
):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO leads
                (user_id, name, phone_number, lead_type, property_interest, status, source,
                 notes, assigned_user_id, created_at, updated_at,
                 last_contacted_at, latest_call_at, last_outbound_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                name or "Lead",
                phone_number,
                lead_type,
                property_interest,
                status or "new",
                source,
                notes,
                assigned_user_id or user_id,
                now,
                now,
                now if (touch_call or touch_sms) else None,
                now if touch_call else None,
                now if (touch_call or touch_sms) else None,
            ),
        )
        return cur.lastrowid


def update_lead_contact_fields(
    lead_id,
    user_id,
    *,
    name=None,
    lead_type=None,
    property_interest=None,
    notes=None,
    touch_call=False,
    touch_sms=False,
    bump_status_from_new_to=None,
):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        lead = conn.execute(
            "SELECT * FROM leads WHERE id = ? AND user_id = ?",
            (lead_id, user_id),
        ).fetchone()
        if not lead:
            return
        new_status = lead["status"]
        if bump_status_from_new_to and (lead["status"] or "new") == "new":
            new_status = bump_status_from_new_to
        # Postgres CASE WHEN requires a boolean expression. Binding SQLite-style
        # integers (1/0) raises: "argument of CASE/WHEN must be type boolean".
        touch_contact = bind_bool(touch_call or touch_sms)
        touch_call_flag = bind_bool(touch_call)
        # CAST notes binds as TEXT: Postgres cannot infer the type of a bare
        # NULL parameter in `? IS NOT NULL` (IndeterminateDatatype). SQLite
        # accepts either form; SMS upsert often passes notes=NULL.
        conn.execute(
            """
            UPDATE leads
            SET name = COALESCE(?, name),
                lead_type = COALESCE(?, lead_type),
                property_interest = COALESCE(?, property_interest),
                notes = CASE
                    WHEN CAST(? AS TEXT) IS NOT NULL
                         AND (notes IS NULL OR notes = '') THEN CAST(? AS TEXT)
                    ELSE notes
                END,
                status = ?,
                last_contacted_at = CASE WHEN ? THEN ? ELSE last_contacted_at END,
                latest_call_at = CASE WHEN ? THEN ? ELSE latest_call_at END,
                last_outbound_at = CASE WHEN ? THEN ? ELSE last_outbound_at END,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                name,
                lead_type,
                property_interest,
                notes,
                notes,
                new_status,
                touch_contact,
                now,
                touch_call_flag,
                now,
                touch_contact,
                now,
                now,
                lead_id,
                user_id,
            ),
        )


def touch_lead_call_timestamps(lead_id, user_id):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET last_contacted_at = ?,
                latest_call_at = ?,
                last_outbound_at = COALESCE(last_outbound_at, ?),
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, now, now, now, lead_id, user_id),
        )


def merge_lead_call_outcome_notes(
    lead_id, user_id, *, summary=None, outcome=None, next_action=None, follow_up_at=None
):
    now = datetime.now(timezone.utc).isoformat()
    bits = []
    if summary:
        bits.append(f"Call summary: {summary}")
    if outcome:
        bits.append(f"Call outcome: {outcome}")
    note_add = " | ".join(bits)[:1500] if bits else None
    with get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET notes = CASE
                    WHEN ? IS NULL THEN notes
                    WHEN notes IS NULL OR notes = '' THEN ?
                    ELSE notes || ' | ' || ?
                END,
                next_action = COALESCE(?, next_action),
                follow_up_at = COALESCE(?, follow_up_at),
                next_follow_up_at = COALESCE(?, next_follow_up_at),
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                note_add,
                note_add,
                note_add,
                next_action,
                follow_up_at,
                follow_up_at,
                now,
                lead_id,
                user_id,
            ),
        )


def get_lead(lead_id, user_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM leads WHERE id = ? AND user_id = ?",
            (lead_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def get_lead_by_phone(user_id, phone_number):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM leads WHERE user_id = ? AND phone_number = ?",
            (user_id, phone_number),
        ).fetchone()
        return dict(row) if row else None


def list_leads(user_id, limit=50):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT l.*,
                   (SELECT COUNT(*) FROM sms_messages sm WHERE sm.lead_id = l.id) AS message_count
            FROM leads l
            WHERE l.user_id = ?
            ORDER BY l.updated_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def update_lead_from_analysis(lead_id, user_id, *, status=None, notes=None, next_action=None, follow_up_at=None, last_inbound_at=None):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET status = COALESCE(?, status),
                notes = COALESCE(?, notes),
                next_action = COALESCE(?, next_action),
                follow_up_at = COALESCE(?, follow_up_at),
                last_inbound_at = COALESCE(?, last_inbound_at),
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (status, notes, next_action, follow_up_at, last_inbound_at, now, lead_id, user_id),
        )


def touch_lead_outbound(lead_id, user_id):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET last_outbound_at = ?,
                status = CASE WHEN status = 'new' THEN 'contacted' ELSE status END,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, now, lead_id, user_id),
        )


def create_follow_up(lead_id, user_id, due_at, reason):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO lead_follow_ups (lead_id, user_id, due_at, reason, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (lead_id, user_id, due_at, reason, now),
        )
        return cur.lastrowid


def list_due_follow_ups(user_id, limit=20):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT f.*, l.name AS lead_name, l.phone_number, l.status AS lead_status
            FROM lead_follow_ups f
            JOIN leads l ON l.id = f.lead_id
            WHERE f.user_id = ? AND f.status = 'pending' AND f.due_at <= ?
            ORDER BY f.due_at ASC
            LIMIT ?
            """,
            (user_id, now, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def create_lead_insight(lead_id, user_id, inbound_message_id, analysis, suggested_message_id=None, model="claude"):
    now = datetime.now(timezone.utc).isoformat()
    topics = analysis.get("escalation_topics") or []
    if isinstance(topics, list):
        topics_value = ",".join(topics)
    else:
        topics_value = str(topics or "")
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO lead_insights
                (lead_id, user_id, inbound_message_id, suggested_message_id, summary, intent, next_best_step,
                 recommended_action, suggested_reply, home_value_pitch, confidence_score,
                 requires_manual_review, escalation_topics, model, status, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                lead_id,
                user_id,
                inbound_message_id,
                suggested_message_id,
                analysis.get("summary"),
                analysis.get("intent"),
                analysis.get("next_best_step"),
                analysis.get("recommended_action"),
                analysis.get("suggested_reply"),
                analysis.get("home_value_pitch"),
                analysis.get("confidence_score"),
                1 if analysis.get("requires_manual_review") else 0,
                topics_value,
                model,
                analysis.get("raw_json"),
                now,
            ),
        )
        return cur.lastrowid


def list_pending_insights(user_id, limit=20):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT i.*, l.name AS lead_name, l.phone_number, l.status AS lead_status,
                   sm.message_body AS inbound_body
            FROM lead_insights i
            JOIN leads l ON l.id = i.lead_id
            LEFT JOIN sms_messages sm ON sm.id = i.inbound_message_id
            WHERE i.user_id = ? AND i.status = 'pending'
            ORDER BY i.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def get_insight(insight_id, user_id):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT i.*, l.name AS lead_name, l.phone_number, l.lead_type, l.property_interest
            FROM lead_insights i
            JOIN leads l ON l.id = i.lead_id
            WHERE i.id = ? AND i.user_id = ?
            """,
            (insight_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def update_insight_status(insight_id, user_id, status):
    with get_db() as conn:
        conn.execute(
            "UPDATE lead_insights SET status = ? WHERE id = ? AND user_id = ?",
            (status, insight_id, user_id),
        )


def create_sms_message(
    user_id,
    persona_id,
    provider,
    data,
    status="draft",
    lead_id=None,
    direction="outbound",
    consent_status="unknown",
    opt_out_status="active",
):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sms_messages
                (user_id, persona_id, lead_id, provider, direction, lead_name, phone_number, lead_type,
                 property_interest, desired_outcome, notes, message_body, status, consent_status,
                 opt_out_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                persona_id,
                lead_id,
                provider,
                direction,
                data.get("lead_name"),
                data.get("phone_number"),
                data.get("lead_type"),
                data.get("property_interest"),
                data.get("desired_outcome"),
                data.get("notes"),
                data.get("message_body"),
                status,
                consent_status,
                opt_out_status,
                now,
            ),
        )
        return cur.lastrowid


def mark_lead_opt_out(lead_id, user_id):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET opt_out_status = 'opted_out',
                status = 'do_not_contact',
                next_action = 'Do not contact — lead opted out',
                next_follow_up_at = NULL,
                follow_up_reason = NULL,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, lead_id, user_id),
        )
        conn.execute(
            """
            UPDATE sms_messages
            SET status = 'cancelled',
                error_message = 'Cancelled — lead opted out',
                direction = 'suggested'
            WHERE lead_id = ? AND user_id = ? AND status = 'suggested'
            """,
            (lead_id, user_id),
        )
        conn.execute(
            """
            UPDATE lead_insights
            SET status = 'dismissed'
            WHERE lead_id = ? AND user_id = ? AND status = 'pending'
            """,
            (lead_id, user_id),
        )


def clear_lead_sms_opt_out(lead_id, user_id):
    """Clear STOP flag after START. Does not auto-verify consent or unblock SMS."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET opt_out_status = 'active',
                consent_status = CASE
                    WHEN consent_status IN ('revoked', 'opted_out') THEN 'unknown'
                    ELSE consent_status
                END,
                sms_consent_status = 'unverified',
                sms_sending_blocked = ?,
                status = CASE
                    WHEN status = 'do_not_contact' THEN 'contacted'
                    ELSE status
                END,
                next_action = 'Lead requested to resume SMS — review consent before sending',
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (bind_bool(True), now, lead_id, user_id),
        )


def cancel_suggested_sms_for_lead(lead_id, user_id, reason="Cancelled"):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE sms_messages
            SET status = 'cancelled', error_message = ?
            WHERE lead_id = ? AND user_id = ? AND status = 'suggested'
            """,
            (reason[:500], lead_id, user_id),
        )


def get_sms_message_by_provider_id(provider_message_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sms_messages WHERE provider_message_id = ? LIMIT 1",
            (provider_message_id,),
        ).fetchone()
        return dict(row) if row else None


def create_inbound_sms_message(
    user_id,
    phone_number,
    message_body,
    provider_message_id=None,
    lead_id=None,
    lead_name=None,
    opt_out_status="active",
    to_number=None,
    media_urls=None,
    num_media=0,
    status_meta=None,
):
    """Insert inbound SMS. Idempotent on provider_message_id. Returns (id, is_duplicate)."""
    import json

    if provider_message_id:
        existing = get_sms_message_by_provider_id(provider_message_id)
        if existing:
            return existing["id"], True

    now = datetime.now(timezone.utc).isoformat()
    media_json = None
    if media_urls:
        media_json = json.dumps(list(media_urls)[:10])
    notes = None
    if status_meta:
        notes = f"twilio_status={status_meta}"[:200]

    with get_db() as conn:
        if provider_message_id:
            row = conn.execute(
                "SELECT id FROM sms_messages WHERE provider_message_id = ? LIMIT 1",
                (provider_message_id,),
            ).fetchone()
            if row:
                return row["id"], True
        try:
            cur = conn.execute(
                """
                INSERT INTO sms_messages
                    (user_id, persona_id, lead_id, provider, provider_message_id, direction, lead_name,
                     phone_number, message_body, status, consent_status, opt_out_status, created_at,
                     to_number, media_urls, num_media, notes)
                VALUES (?, NULL, ?, 'twilio', ?, 'inbound', ?, ?, ?, 'received', 'unknown', ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    lead_id,
                    provider_message_id,
                    lead_name,
                    phone_number,
                    message_body,
                    opt_out_status,
                    now,
                    to_number,
                    media_json,
                    int(num_media or 0),
                    notes,
                ),
            )
        except Exception:
            cur = conn.execute(
                """
                INSERT INTO sms_messages
                    (user_id, persona_id, lead_id, provider, provider_message_id, direction, lead_name,
                     phone_number, message_body, status, consent_status, opt_out_status, created_at, notes)
                VALUES (?, NULL, ?, 'twilio', ?, 'inbound', ?, ?, ?, 'received', 'unknown', ?, ?, ?)
                """,
                (
                    user_id,
                    lead_id,
                    provider_message_id,
                    lead_name,
                    phone_number,
                    message_body,
                    opt_out_status,
                    now,
                    notes,
                ),
            )
        return cur.lastrowid, False


def set_lead_consent(lead_id, user_id, consent_status="confirmed"):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET consent_status = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (consent_status, now, lead_id, user_id),
        )


def create_sms_consent_inquiry(
    *,
    name,
    phone_number,
    message,
    sms_consent,
    consent_at=None,
    source_url=None,
    disclosure_version=None,
    ip_address=None,
    user_agent=None,
    created_at=None,
    first_name=None,
    last_name=None,
    email=None,
    campaign_source=None,
):
    """Insert a public SMS consent / inquiry row. Returns id.

    sms_consent must already be engine-safe (bool for Postgres, 0/1 for SQLite).
    """
    import config

    created = created_at or datetime.now(timezone.utc).isoformat()
    # Normalize boolean for Postgres (int 1/0 causes: expected bool, got int)
    if isinstance(sms_consent, bool):
        consent_val = sms_consent if config.DB_ENGINE == "postgres" else (1 if sms_consent else 0)
    elif sms_consent in (1, 0, "1", "0"):
        flag = bool(int(sms_consent))
        consent_val = flag if config.DB_ENGINE == "postgres" else (1 if flag else 0)
    else:
        flag = bool(sms_consent)
        consent_val = flag if config.DB_ENGINE == "postgres" else (1 if flag else 0)

    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sms_consent_inquiries
                (name, first_name, last_name, email, phone_number, message, sms_consent,
                 consent_at, source_url, disclosure_version, ip_address, user_agent,
                 campaign_source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                first_name,
                last_name,
                email,
                phone_number,
                message,
                consent_val,
                consent_at,
                source_url,
                disclosure_version,
                ip_address,
                user_agent,
                campaign_source,
                created,
            ),
        )
        return cur.lastrowid


def find_sms_consent_inquiry_duplicate(phone_number, *, disclosure_version, require_consent=True):
    if not phone_number:
        return None
    import config

    with get_db() as conn:
        sql = """
            SELECT * FROM sms_consent_inquiries
            WHERE phone_number = ?
              AND disclosure_version = ?
        """
        params = [phone_number, disclosure_version]
        if require_consent:
            if config.DB_ENGINE == "postgres":
                sql += " AND sms_consent IS TRUE"
            else:
                sql += " AND sms_consent = 1"
        sql += " ORDER BY created_at DESC, id DESC LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        data = dict(row)
        data["sms_consent"] = bool(data.get("sms_consent"))
        return data


def touch_sms_consent_inquiry(
    inquiry_id,
    *,
    name=None,
    first_name=None,
    last_name=None,
    email=None,
    message=None,
    source_url=None,
    campaign_source=None,
    ip_address=None,
    user_agent=None,
):
    """Update metadata on an existing consent row (dedupe path). Does not clear consent."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE sms_consent_inquiries
            SET name = COALESCE(?, name),
                first_name = COALESCE(?, first_name),
                last_name = COALESCE(?, last_name),
                email = COALESCE(?, email),
                message = COALESCE(?, message),
                source_url = COALESCE(?, source_url),
                campaign_source = COALESCE(?, campaign_source),
                ip_address = COALESCE(?, ip_address),
                user_agent = COALESCE(?, user_agent),
                consent_at = COALESCE(consent_at, ?)
            WHERE id = ?
            """,
            (
                name,
                first_name,
                last_name,
                email,
                message,
                source_url,
                campaign_source,
                ip_address,
                user_agent,
                now,
                inquiry_id,
            ),
        )


def get_sms_consent_inquiry(inquiry_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sms_consent_inquiries WHERE id = ? LIMIT 1",
            (inquiry_id,),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["sms_consent"] = bool(data.get("sms_consent"))
        return data


def latest_sms_consent_inquiry_for_phone(phone_number):
    if not phone_number:
        return None
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM sms_consent_inquiries
            WHERE phone_number = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (phone_number,),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["sms_consent"] = bool(data.get("sms_consent"))
        return data


def update_sms_message_body(message_id, user_id, message_body, direction="outbound"):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE sms_messages
            SET message_body = ?, direction = ?
            WHERE id = ? AND user_id = ?
            """,
            (message_body, direction, message_id, user_id),
        )


def update_sms_compliance(message_id, user_id, consent_status=None, opt_out_status=None):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE sms_messages
            SET consent_status = COALESCE(?, consent_status),
                opt_out_status = COALESCE(?, opt_out_status)
            WHERE id = ? AND user_id = ?
            """,
            (consent_status, opt_out_status, message_id, user_id),
        )


def update_sms_message_send_result(
    message_id,
    provider_message_id=None,
    status="sent",
    error_message=None,
    *,
    from_number=None,
    to_number=None,
    segments=None,
    failure_code=None,
    correlation_id=None,
    raw_provider_status=None,
    provider_cost=None,
):
    """Persist outbound send / delivery result fields (no secrets)."""
    now = datetime.now(timezone.utc).isoformat()
    status_l = (status or "").strip().lower() or "sent"
    # preparing = local attempt started; submitted_at only after provider accepts.
    submitted_at = now if status_l in ("queued", "sent", "delivered", "submitted") else None
    sent_at = now if status_l in ("sent", "delivered", "queued", "submitted") else None
    delivered_at = now if status_l == "delivered" else None
    failed_at = (
        now
        if status_l
        in (
            "failed",
            "delivery_failed",
            "rejected",
            "provider_error",
            "database_error",
            "expired",
        )
        else None
    )
    # Preserve prior error_message on non-failure updates unless explicitly provided.
    clear_error = status_l in (
        "queued",
        "sent",
        "delivered",
        "submitted",
        "preparing",
    )
    err_value = error_message
    if err_value is None and not clear_error:
        # Leave existing error_message untouched for ambiguous updates.
        err_sql = "error_message = COALESCE(?, error_message)"
    else:
        err_sql = "error_message = ?"
        if clear_error and error_message is None:
            err_value = None
    with get_db() as conn:
        conn.execute(
            f"""
            UPDATE sms_messages
            SET provider_message_id = COALESCE(?, provider_message_id),
                status = ?,
                {err_sql},
                sent_at = COALESCE(?, sent_at),
                submitted_at = COALESCE(?, submitted_at),
                delivered_at = COALESCE(?, delivered_at),
                failed_at = COALESCE(?, failed_at),
                from_number = COALESCE(?, from_number),
                to_number = COALESCE(?, to_number),
                segments = COALESCE(?, segments),
                failure_code = COALESCE(?, failure_code),
                correlation_id = COALESCE(?, correlation_id),
                raw_provider_status = COALESCE(?, raw_provider_status),
                provider_cost = COALESCE(?, provider_cost)
            WHERE id = ?
            """,
            (
                provider_message_id,
                status_l,
                err_value,
                sent_at,
                submitted_at,
                delivered_at,
                failed_at,
                from_number,
                to_number,
                segments,
                str(failure_code) if failure_code is not None else None,
                correlation_id,
                raw_provider_status,
                str(provider_cost) if provider_cost is not None else None,
                message_id,
            ),
        )


def update_sms_message_by_provider_id(
    provider_message_id,
    status=None,
    error_message=None,
    *,
    failure_code=None,
    raw_provider_status=None,
):
    if not provider_message_id:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    status_l = (status or "").strip().lower() if status else None
    sent_at = now if status_l in ("sent", "delivered", "queued", "submitted") else None
    submitted_at = now if status_l in ("queued", "sent", "delivered", "submitted") else None
    delivered_at = now if status_l == "delivered" else None
    failed_at = (
        now
        if status_l
        in (
            "failed",
            "delivery_failed",
            "rejected",
            "provider_error",
            "database_error",
            "expired",
        )
        else None
    )
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE sms_messages
            SET status = COALESCE(?, status),
                error_message = COALESCE(?, error_message),
                sent_at = COALESCE(?, sent_at),
                submitted_at = COALESCE(?, submitted_at),
                delivered_at = COALESCE(?, delivered_at),
                failed_at = COALESCE(?, failed_at),
                failure_code = COALESCE(?, failure_code),
                raw_provider_status = COALESCE(?, raw_provider_status)
            WHERE provider_message_id = ?
            """,
            (
                status_l,
                error_message,
                sent_at,
                submitted_at,
                delivered_at,
                failed_at,
                str(failure_code) if failure_code is not None else None,
                raw_provider_status,
                provider_message_id,
            ),
        )
        return cur.rowcount


def get_latest_outbound_sms(user_id, *, lead_id=None, phone_number=None):
    """Latest outbound SMS attempt for the authenticated tenant (never cross-tenant)."""
    if not user_id:
        return None
    clauses = [
        "user_id = ?",
        "COALESCE(direction, 'outbound') = 'outbound'",
        # Exclude drafts / inbox helpers — only real send attempts and outcomes.
        "status NOT IN ('received', 'suggested', 'cancelled', 'draft')",
    ]
    params = [user_id]
    if lead_id is not None:
        clauses.append("lead_id = ?")
        params.append(lead_id)
    if phone_number:
        clauses.append("(phone_number = ? OR to_number = ?)")
        params.extend([phone_number, phone_number])
    where = " AND ".join(clauses)
    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT *
            FROM sms_messages
            WHERE {where}
            ORDER BY COALESCE(submitted_at, sent_at, created_at) DESC, id DESC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        return dict(row) if row else None


def find_sms_user_by_phone(phone_number):
    if not phone_number:
        return None
    with get_db() as conn:
        lead = conn.execute(
            """
            SELECT user_id FROM leads
            WHERE phone_number = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (phone_number,),
        ).fetchone()
        if lead:
            return lead["user_id"]
        row = conn.execute(
            """
            SELECT user_id
            FROM sms_messages
            WHERE phone_number = ? AND status != 'received' AND user_id IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (phone_number,),
        ).fetchone()
        return row["user_id"] if row else None


def list_sms_messages(user_id, limit=20):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT sm.*, vp.name AS persona_name
            FROM sms_messages sm
            LEFT JOIN voice_personas vp ON vp.id = sm.persona_id
            WHERE sm.user_id = ?
            ORDER BY sm.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def latest_failed_sms_error(user_id):
    """Return the most recent failed outbound SMS error for this account (no secrets)."""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT error_message, status, created_at, sent_at
            FROM sms_messages
            WHERE user_id = ?
              AND status = 'failed'
              AND error_message IS NOT NULL
              AND TRIM(error_message) != ''
            ORDER BY COALESCE(sent_at, created_at) DESC, id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def list_lead_messages(user_id, lead_id, limit=100):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT sm.*, vp.name AS persona_name
            FROM sms_messages sm
            LEFT JOIN voice_personas vp ON vp.id = sm.persona_id
            WHERE sm.user_id = ? AND sm.lead_id = ?
            ORDER BY sm.created_at ASC
            LIMIT ?
            """,
            (user_id, lead_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def get_sms_message(message_id, user_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sms_messages WHERE id = ? AND user_id = ?",
            (message_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def last_outbound_seed_for_phone(user_id, phone_number):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT lead_name, lead_type, property_interest, notes, desired_outcome
            FROM sms_messages
            WHERE user_id = ? AND phone_number = ? AND status != 'received'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id, phone_number),
        ).fetchone()
        return dict(row) if row else {}


def record_tool_usage(user_id, tool_key, event_type="generated", metadata=None):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO tool_usage (user_id, tool_key, event_type, metadata, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, tool_key, event_type, metadata, now),
        )
        return cur.lastrowid


def _count_usage_since(conn, user_id, tool_key, since_iso=None):
    if since_iso:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM tool_usage
            WHERE user_id = ? AND tool_key = ? AND created_at >= ?
            """,
            (user_id, tool_key, since_iso),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM tool_usage
            WHERE user_id = ? AND tool_key = ?
            """,
            (user_id, tool_key),
        ).fetchone()
    return int(row["count"] if row else 0)


def _last_usage_at(conn, user_id, tool_key):
    row = conn.execute(
        """
        SELECT created_at
        FROM tool_usage
        WHERE user_id = ? AND tool_key = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (user_id, tool_key),
    ).fetchone()
    return row["created_at"] if row else None


def get_dashboard_metrics(user_id):
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    since_7d = (now - timedelta(days=7)).isoformat()
    since_30d = (now - timedelta(days=30)).isoformat()

    with get_db() as conn:
        listing_total = _count_usage_since(conn, user_id, "listing_generator")
        listing_7d = _count_usage_since(conn, user_id, "listing_generator", since_7d)
        listing_30d = _count_usage_since(conn, user_id, "listing_generator", since_30d)
        listing_last = _last_usage_at(conn, user_id, "listing_generator")

        scripts_total = _count_usage_since(conn, user_id, "cold_call_scripts")
        scripts_7d = _count_usage_since(conn, user_id, "cold_call_scripts", since_7d)
        scripts_30d = _count_usage_since(conn, user_id, "cold_call_scripts", since_30d)
        scripts_last = _last_usage_at(conn, user_id, "cold_call_scripts")

        calls_total = conn.execute(
            "SELECT COUNT(*) AS count FROM voice_calls WHERE user_id = ?",
            (user_id,),
        ).fetchone()["count"]
        calls_7d = conn.execute(
            "SELECT COUNT(*) AS count FROM voice_calls WHERE user_id = ? AND created_at >= ?",
            (user_id, since_7d),
        ).fetchone()["count"]
        calls_30d = conn.execute(
            "SELECT COUNT(*) AS count FROM voice_calls WHERE user_id = ? AND created_at >= ?",
            (user_id, since_30d),
        ).fetchone()["count"]
        calls_completed = conn.execute(
            "SELECT COUNT(*) AS count FROM voice_calls WHERE user_id = ? AND status = 'completed'",
            (user_id,),
        ).fetchone()["count"]
        calls_appointments = conn.execute(
            "SELECT COUNT(*) AS count FROM voice_calls WHERE user_id = ? AND appointment_requested = 1",
            (user_id,),
        ).fetchone()["count"]
        calls_with_recording = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM voice_calls
            WHERE user_id = ? AND recording_url IS NOT NULL AND recording_url != ''
            """,
            (user_id,),
        ).fetchone()["count"]
        calls_last = conn.execute(
            """
            SELECT created_at FROM voice_calls
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        calls_last = calls_last["created_at"] if calls_last else None

        recent_usage = conn.execute(
            """
            SELECT tool_key, event_type, created_at
            FROM tool_usage
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 8
            """,
            (user_id,),
        ).fetchall()
        recent_calls = conn.execute(
            """
            SELECT lead_name, status, appointment_requested, created_at, summary
            FROM voice_calls
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (user_id,),
        ).fetchall()

        sms_total = conn.execute(
            "SELECT COUNT(*) AS count FROM sms_messages WHERE user_id = ?",
            (user_id,),
        ).fetchone()["count"]
        sms_7d = conn.execute(
            "SELECT COUNT(*) AS count FROM sms_messages WHERE user_id = ? AND created_at >= ?",
            (user_id, since_7d),
        ).fetchone()["count"]
        sms_30d = conn.execute(
            "SELECT COUNT(*) AS count FROM sms_messages WHERE user_id = ? AND created_at >= ?",
            (user_id, since_30d),
        ).fetchone()["count"]
        sms_sent = conn.execute(
            "SELECT COUNT(*) AS count FROM sms_messages WHERE user_id = ? AND status = 'sent'",
            (user_id,),
        ).fetchone()["count"]
        sms_draft = conn.execute(
            "SELECT COUNT(*) AS count FROM sms_messages WHERE user_id = ? AND status = 'draft'",
            (user_id,),
        ).fetchone()["count"]
        sms_last = conn.execute(
            """
            SELECT created_at FROM sms_messages
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        sms_last = sms_last["created_at"] if sms_last else None
        recent_sms = conn.execute(
            """
            SELECT lead_name, phone_number, status, message_body, created_at
            FROM sms_messages
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (user_id,),
        ).fetchall()

        leads_total = conn.execute(
            "SELECT COUNT(*) AS count FROM leads WHERE user_id = ?",
            (user_id,),
        ).fetchone()["count"]
        leads_replied = conn.execute(
            "SELECT COUNT(*) AS count FROM leads WHERE user_id = ? AND status = 'replied'",
            (user_id,),
        ).fetchone()["count"]
        leads_nurture = conn.execute(
            "SELECT COUNT(*) AS count FROM leads WHERE user_id = ? AND status = 'nurture'",
            (user_id,),
        ).fetchone()["count"]
        leads_hot = conn.execute(
            "SELECT COUNT(*) AS count FROM leads WHERE user_id = ? AND status = 'hot'",
            (user_id,),
        ).fetchone()["count"]
        pending_suggestions = conn.execute(
            "SELECT COUNT(*) AS count FROM lead_insights WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        ).fetchone()["count"]
        # Shared account-timezone classification (same as Follow-ups / Pipeline).
        import crm_db

        fu_counts = crm_db.follow_up_dashboard_counts(user_id)
        follow_ups_due_today = fu_counts["follow_ups_due_today"]
        follow_ups_overdue = fu_counts["follow_ups_overdue"]
        follow_ups_due_this_week = fu_counts["follow_ups_due_this_week"]
        follow_ups_due = follow_ups_due_today + follow_ups_overdue
        recent_leads = conn.execute(
            """
            SELECT id, name, phone_number, status, next_action, follow_up_at, next_follow_up_at, updated_at
            FROM leads
            WHERE user_id = ?
            ORDER BY updated_at DESC
            LIMIT 8
            """,
            (user_id,),
        ).fetchall()
        due_follow_ups = conn.execute(
            """
            SELECT f.id, f.due_at, f.reason, f.priority, f.status,
                   l.name AS lead_name, l.phone_number, l.id AS lead_id
            FROM lead_follow_ups f
            JOIN leads l ON l.id = f.lead_id
            WHERE f.user_id = ? AND f.status = 'pending' AND f.due_at <= ?
            ORDER BY f.due_at ASC
            LIMIT 8
            """,
            (user_id, now.isoformat()),
        ).fetchall()

    content_pieces = (listing_total * 3) + (scripts_total * 3)
    return {
        "overview": {
            "listing_generations": listing_total,
            "script_generations": scripts_total,
            "ai_calls": calls_total,
            "sms_messages": sms_total,
            "leads_total": leads_total,
            "appointments_requested": calls_appointments,
            "content_pieces": content_pieces,
            "activity_7d": listing_7d + scripts_7d + calls_7d + sms_7d,
            "activity_30d": listing_30d + scripts_30d + calls_30d + sms_30d,
        },
        "crm": {
            "leads_total": leads_total,
            "leads_replied": leads_replied,
            "leads_nurture": leads_nurture,
            "leads_hot": leads_hot,
            "pending_suggestions": pending_suggestions,
            "follow_ups_due": follow_ups_due,
            "follow_ups_due_today": follow_ups_due_today,
            "follow_ups_overdue": follow_ups_overdue,
            "follow_ups_due_this_week": follow_ups_due_this_week,
            "recent_leads": [dict(row) for row in recent_leads],
            "due_follow_ups": [dict(row) for row in due_follow_ups],
        },
        "tools": {
            "listing_generator": {
                "label": "Listing Generator",
                "total": listing_total,
                "last_7_days": listing_7d,
                "last_30_days": listing_30d,
                "last_used_at": listing_last,
                "outputs_per_run": 3,
                "outputs_label": "listing + social + email drafts",
            },
            "cold_call_scripts": {
                "label": "Cold Call Scripts",
                "total": scripts_total,
                "last_7_days": scripts_7d,
                "last_30_days": scripts_30d,
                "last_used_at": scripts_last,
                "outputs_per_run": 3,
                "outputs_label": "opening + objections + voicemail",
            },
            "ai_calling": {
                "label": "AI Calling Assistant",
                "total": calls_total,
                "last_7_days": calls_7d,
                "last_30_days": calls_30d,
                "last_used_at": calls_last,
                "completed": calls_completed,
                "appointments_requested": calls_appointments,
                "recordings_available": calls_with_recording,
            },
            "ai_sms": {
                "label": "AI SMS Assistant",
                "total": sms_total,
                "last_7_days": sms_7d,
                "last_30_days": sms_30d,
                "last_used_at": sms_last,
                "sent": sms_sent,
                "drafts": sms_draft,
            },
        },
        "recent_usage": [dict(row) for row in recent_usage],
        "recent_calls": [dict(row) for row in recent_calls],
        "recent_sms": [dict(row) for row in recent_sms],
    }
