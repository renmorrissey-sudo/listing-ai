"""Spend alerts for the AI Harness (D2).

No external service in this step — no Slack, PagerDuty, or Sentry. An
alert is a row in the append-only ``harness_audit`` log plus a structured
log line, so it is visible in the same place as every other harness
decision. Callers (currently :func:`harness.policy.decide`) invoke
:func:`record_spend_alert` when a tenant's month-to-date spend crosses
80% of the monthly cap (``level="warning"``) or reaches 100%
(``level="limit"``).

Dedup: at most one alert per tenant per level per UTC month is written.
The gate calls ``decide()`` on every proposed action, so without this the
audit log would gain one row per gated action while a tenant sits over a
threshold. The level and period are embedded in ``gate_reason`` so the
dedup query can match on them; the spend/cap figures in the reason are
the values observed when the alert first fired that month.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import db

logger = logging.getLogger(__name__)

# Levels record_spend_alert understands.
LEVELS = ("warning", "limit")

# gate_decision recorded on the audit row for each level.
_LEVEL_GATE_DECISION = {"warning": "DRAFT", "limit": "BLOCK"}


def _current_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def record_spend_alert(user_id, spend_cents, cap_cents, level) -> int | None:
    """Record a spend alert and return its ``harness_audit`` row id.

    ``level`` is ``"warning"`` (spend crossed 80% of the cap) or
    ``"limit"`` (spend reached 100%). Writes one audit row
    (``actor="system"``, ``action="spend_alert"``,
    ``gate_decision="DRAFT"`` for a warning / ``"BLOCK"`` for the limit)
    and a structured log line. Returns ``None`` without raising when the
    level is unknown, ``user_id`` is None, the audit table is missing, or
    an identical alert for this tenant/level/month already exists — an
    alert failure must never break the policy gate.
    """
    if level not in LEVELS:
        logger.warning("spend alert ignored: unknown level %r", level)
        return None
    if user_id is None:
        return None
    period = _current_period()
    gate_reason = (
        f"spend_alert level={level} period={period} "
        f"spend_cents={int(spend_cents or 0)} cap_cents={int(cap_cents or 0)}"
    )
    try:
        from harness.audit import record_audit

        with db.get_db() as conn:
            existing = conn.execute(
                """
                SELECT id FROM harness_audit
                WHERE user_id = ? AND action = 'spend_alert'
                  AND gate_reason LIKE ? AND created_at LIKE ?
                LIMIT 1
                """,
                (user_id, f"%level={level}%", f"{period}%"),
            ).fetchone()
        if existing:
            return int(dict(existing)["id"])
        audit_id = record_audit(
            user_id,
            actor="system",
            action="spend_alert",
            tool="spend_alert",
            mode=None,
            gate_decision=_LEVEL_GATE_DECISION[level],
            gate_reason=gate_reason,
            cost_cents=int(spend_cents or 0),
        )
    except Exception as exc:  # audit table missing, DB down, etc.
        logger.warning("spend alert could not be recorded: %s", exc)
        return None
    logger.warning(
        "harness spend_alert user_id=%s level=%s period=%s spend_cents=%s cap_cents=%s",
        user_id, level, period, int(spend_cents or 0), int(cap_cents or 0),
    )
    return audit_id


# --- Operational failure alerts (M4) ----------------------------------------
# Dead-letter jobs, repeated provider failures, and worker failures must
# surface to an operator instead of sitting silently in harness_jobs.
#
# Configuration (all via environment, disabled by default):
#   HARNESS_ALERT_WEBHOOK_URL -- POST the alert JSON to this URL. When
#     unset (the default) nothing is sent anywhere external: the alert is
#     still written to the harness_audit log (action="ops_alert") and to
#     the structured log, so it is visible in the same place as every
#     other harness decision.
#   HARNESS_ALERTS_ENABLED -- set to "false"/"0"/"no" to disable external
#     sending even when a webhook URL is configured.
#   HARNESS_ALERT_PROVIDER_FAILURE_THRESHOLD -- consecutive retryable
#     failures for one provider before alerting (default 3).
#
# Notifier interface: any object with ``send(alert: dict) -> None``.
# :class:`WebhookNotifier` is the built-in one (stdlib urllib, no new
# dependency). Tests inject a mock notifier via :func:`set_notifier` or
# pass one directly, so delivery can be proven without real providers.
#
# Dedup: an event key is alerted at most once per process (in-memory
# set) and at most once ever across restarts (an ``ops_alert`` audit row
# carrying the key). Provider/worker-failure keys are scoped to the
# current UTC hour so a still-broken provider re-alerts hourly, not on
# every failure.
#
# Non-blocking: every public function here catches its own failures
# (notifier raising, audit/DB errors, redaction errors) and returns
# False / None instead of raising, so a failing notifier can never break
# job processing or the worker loop. Callers additionally wrap calls in
# try/except as belt and braces.

import hashlib
import json
import os
import re as _re
import urllib.request

try:
    from harness.jobs import redact_payload as _redact_payload
    from harness.jobs import redact_text as _redact_text
except Exception:  # pragma: no cover - jobs module unavailable
    _redact_payload = None
    _redact_text = None

PROVIDER_FAILURE_THRESHOLD_DEFAULT = 3
_KNOWN_PROVIDERS = (
    "telnyx", "twilio", "simpletexting", "sendgrid", "vapi",
    "anthropic", "openai", "stripe",
)

# In-memory dedup state (per process). Reset by _reset_alert_state(),
# which exists for tests; production never calls it.
_sent_keys: set = set()
_provider_failure_counts: dict = {}

# Test/operator override for the notifier; None means "resolve from env".
_NOTIFIER_UNSET = object()
_notifier_override = _NOTIFIER_UNSET


def _current_hour() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


def _redact_value(value):
    """Redact secrets (everywhere) plus phone/email from a string value.

    Reuses harness.jobs.redact_text (H3) with redact_pii=True so phone
    numbers and emails never appear verbatim in an alert, even when they
    sit in a non-body field. Falls back to a local secret-only pass if
    the jobs module is unavailable.
    """
    if not isinstance(value, str):
        return value
    if _redact_text is not None:
        try:
            return _redact_text(value, redact_pii=True)
        except Exception:
            pass
    return _re.sub(
        r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*\S+", r"\1=[REDACTED]",
        _re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "[REDACTED]", value),
    )


def _redact_detail(detail):
    """Recursively redact an alert detail dict (secrets + PII).

    Structured payload values are dropped to key names only by the
    callers; this is the belt-and-braces pass over whatever remains:
    every string is run through _redact_value.
    """
    if isinstance(detail, dict):
        return {k: _redact_detail(v) for k, v in detail.items()}
    if isinstance(detail, list):
        return [_redact_detail(v) for v in detail]
    if isinstance(detail, str):
        return _redact_value(detail)
    return detail


def _signature(text, limit=80):
    """Stable short signature of an error string for dedup keys.

    Redacted first (so the key itself never holds a secret), then
    lowercased with runs of non-alphanumerics collapsed, truncated.
    """
    clean = _re.sub(r"[^a-z0-9]+", "-", _redact_value(str(text or ""))[:300].lower())
    return clean.strip("-")[:limit] or "unknown"


def _like_escape(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _audit_has_key(key) -> bool:
    """True if an ops_alert audit row already carries this dedup key."""
    try:
        with db.get_db() as conn:
            row = conn.execute(
                """
                SELECT id FROM harness_audit
                WHERE action = 'ops_alert' AND gate_reason LIKE ? ESCAPE '\\'
                LIMIT 1
                """,
                (f"% key={_like_escape(key)} %",),
            ).fetchone()
        return row is not None
    except Exception:
        # Audit table missing etc: do not block alerting.
        return False


def _record_ops_audit(user_id, event_type, key, title):
    try:
        from harness.audit import record_audit

        return record_audit(
            user_id,
            actor="system",
            action="ops_alert",
            tool="ops_alert",
            mode=None,
            gate_decision=None,
            gate_reason=f"ops_alert event={event_type} key={key} {_redact_value(title)}",
            cost_cents=0,
        )
    except Exception as exc:
        logger.warning("ops alert audit row could not be recorded: %s", exc)
        return None


def _prune_hour_keys():
    """Drop hour-scoped dedup keys from earlier UTC hours."""
    hour = _current_hour()
    stale = [k for k in _sent_keys if "|hour=" in k and not k.endswith(f"|hour={hour}")]
    for k in stale:
        _sent_keys.discard(k)


def _reset_alert_state():
    """Clear in-memory dedup + provider-failure counters (tests only)."""
    _sent_keys.clear()
    _provider_failure_counts.clear()


class Notifier:
    """Alert sink interface: implement ``send(alert: dict) -> None``.

    ``alert`` has keys ``event``, ``key``, ``title``, ``detail`` (a
    redacted dict) and ``timestamp`` (UTC ISO). ``send`` raising means
    delivery failed; the caller logs and continues.
    """

    def send(self, alert):
        raise NotImplementedError


class WebhookNotifier(Notifier):
    """POST the alert JSON to a webhook URL (Slack-compatible shape:
    a plain JSON object). Stdlib only; non-2xx raises."""

    def __init__(self, url, timeout=5):
        self.url = url
        self.timeout = timeout

    def send(self, alert):
        body = json.dumps(alert).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"webhook alert failed: HTTP {resp.status}")


class LoggingNotifier(Notifier):
    """Log-only sink: no external send, for explicit opt-in debugging."""

    def send(self, alert):
        logger.warning(
            "harness ops_alert event=%s key=%s title=%s",
            alert.get("event"), alert.get("key"), alert.get("title"),
        )


def _alerts_enabled() -> bool:
    raw = os.environ.get("HARNESS_ALERTS_ENABLED", "").strip().lower()
    return raw not in ("false", "0", "no", "off")


def get_notifier():
    """Return the configured notifier, or None when alerts are disabled.

    Disabled by default: with no ``HARNESS_ALERT_WEBHOOK_URL`` set (and
    no test override) this returns None and notify_event only writes the
    audit row + log line. A test override set via set_notifier wins over
    the environment.
    """
    if _notifier_override is not _NOTIFIER_UNSET:
        return _notifier_override
    if not _alerts_enabled():
        return None
    url = (os.environ.get("HARNESS_ALERT_WEBHOOK_URL") or "").strip()
    if not url:
        return None
    return WebhookNotifier(url)


def set_notifier(notifier):
    """Override the notifier (tests / explicit wiring). None disables."""
    global _notifier_override
    _notifier_override = notifier


def reset_notifier():
    """Clear a set_notifier override; env config applies again."""
    global _notifier_override
    _notifier_override = _NOTIFIER_UNSET


def get_provider_failure_threshold() -> int:
    try:
        return max(1, int(os.environ.get(
            "HARNESS_ALERT_PROVIDER_FAILURE_THRESHOLD",
            PROVIDER_FAILURE_THRESHOLD_DEFAULT,
        )))
    except (TypeError, ValueError):
        return PROVIDER_FAILURE_THRESHOLD_DEFAULT


def notify_event(event_type, key, title, detail=None, user_id=None,
                 notifier=None):
    """Send one deduped, redacted ops alert. Returns True iff an
    external notification was actually sent.

    Returns False (never raises) when the event was already alerted,
    when no notifier is configured (audit row + log line still written),
    or when delivery failed. A failed delivery is deliberately NOT
    recorded as sent, so a later re-trigger of the same event retries.
    """
    _prune_hour_keys()
    full_key = f"{event_type}:{key}"
    if full_key in _sent_keys:
        return False
    try:
        alert = {
            "event": event_type,
            "key": key,
            "title": _redact_value(title),
            "detail": _redact_detail(detail or {}),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        logger.warning("ops alert could not be built event=%s: %s", event_type, exc)
        return False
    if _audit_has_key(key):
        _sent_keys.add(full_key)
        return False
    sink = notifier if notifier is not None else get_notifier()
    if sink is None:
        _record_ops_audit(user_id, event_type, key, title)
        _sent_keys.add(full_key)
        logger.warning(
            "harness ops_alert (no notifier configured) event=%s key=%s title=%s",
            event_type, key, alert["title"],
        )
        return False
    try:
        sink.send(alert)
    except Exception as exc:
        logger.warning(
            "harness ops_alert delivery failed event=%s key=%s: %s",
            event_type, key, exc,
        )
        return False
    _sent_keys.add(full_key)
    _record_ops_audit(user_id, event_type, key, title)
    logger.warning(
        "harness ops_alert sent event=%s key=%s title=%s",
        event_type, key, alert["title"],
    )
    return True


def _job_payload_summary(job):
    """Safe payload summary for an alert: key names only, never values.

    Payload values are excluded entirely -- a phone number or token in
    any field must not reach an alert -- while the key names still tell
    an operator what the job carried.
    """
    payload = (job or {}).get("payload")
    if payload is None:
        payload = (job or {}).get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {}
    if not isinstance(payload, dict):
        return {}
    return {"payload_keys": sorted(str(k) for k in payload.keys())}


def alert_dead_letter(job, error=None, notifier=None):
    """Alert once when a job lands in dead_letter. Returns True iff sent."""
    job_id = (job or {}).get("id")
    if job_id is None:
        return False
    kind = (job or {}).get("kind") or "unknown"
    err = error
    if err is None:
        try:
            result = (job or {}).get("result_json") or (job or {}).get("result")
            if isinstance(result, str):
                result = json.loads(result)
            if isinstance(result, dict):
                err = result.get("error")
        except (TypeError, ValueError):
            err = None
    detail = {
        "job_id": job_id,
        "kind": kind,
        "attempts": (job or {}).get("attempts"),
        "error": _redact_value(str(err)[:300]) if err is not None else None,
    }
    detail.update(_job_payload_summary(job))
    try:
        return notify_event(
            "dead_letter",
            f"dead_letter:{job_id}",
            f"Harness job {job_id} dead-lettered ({kind})",
            detail=detail,
            user_id=(job or {}).get("user_id"),
            notifier=notifier,
        )
    except Exception as exc:
        logger.warning("dead-letter alert failed job=%s: %s", job_id, exc)
        return False


def alert_provider_failure(provider, error, *, kind=None, user_id=None,
                           threshold=None, notifier=None):
    """Alert after ``threshold`` consecutive retryable failures for one
    provider (default 3, via HARNESS_ALERT_PROVIDER_FAILURE_THRESHOLD).
    Failures before the threshold return False silently; the alert fires
    once per provider/error-signature per UTC hour. Returns True iff
    an external notification was actually sent on this call.
    """
    threshold = threshold if threshold is not None else get_provider_failure_threshold()
    provider = (provider or "harness").strip().lower() or "harness"
    signature = _signature(error)
    count_key = f"{provider}|{kind or ''}|{signature}"
    count = int(_provider_failure_counts.get(count_key) or 0) + 1
    _provider_failure_counts[count_key] = count
    if count < threshold:
        return False
    _provider_failure_counts[count_key] = 0
    detail = {
        "provider": provider,
        "kind": kind,
        "error": _redact_value(str(error)[:300]),
        "consecutive_failures": count,
    }
    try:
        return notify_event(
            "provider_failure",
            f"provider_failure:{provider}:{signature}|hour={_current_hour()}",
            f"Harness provider {provider} failed {count}x in a row"
            + (f" ({kind})" if kind else ""),
            detail=detail,
            user_id=user_id,
            notifier=notifier,
        )
    except Exception as exc:
        logger.warning("provider-failure alert failed provider=%s: %s", provider, exc)
        return False


def alert_worker_failure(error, *, worker_id=None, context=None, notifier=None):
    """Alert once per error signature per UTC hour when the worker
    itself fails (dispatch crash, loop error). Returns True iff sent."""
    signature = _signature(error)
    detail = {
        "worker_id": worker_id,
        "error": _redact_value(str(error)[:300]),
    }
    if isinstance(context, dict):
        detail["context"] = _redact_detail(
            {k: (v if isinstance(v, (str, int, float, bool)) or v is None
                 else str(v)[:200])
             for k, v in context.items()}
        )
    try:
        return notify_event(
            "worker_failure",
            f"worker_failure:{signature}|hour={_current_hour()}",
            f"Harness worker failure: {_redact_value(str(error)[:120])}",
            detail=detail,
            user_id=None,
            notifier=notifier,
        )
    except Exception as exc:
        logger.warning("worker-failure alert failed: %s", exc)
        return False


def detect_provider(reason):
    """Best-effort provider name from a handler failure reason string,
    for the worker's provider-failure hook. 'harness' when no known
    provider name appears."""
    match = _re.search(
        r"(telnyx|twilio|simpletexting|sendgrid|vapi|anthropic|openai|stripe)",
        str(reason or ""), _re.IGNORECASE,
    )
    return match.group(1).lower() if match else "harness"
