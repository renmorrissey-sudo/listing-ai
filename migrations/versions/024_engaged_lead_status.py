"""Backfill actively responding open leads to the explicit Engaged status."""

VERSION = "024_engaged_lead_status"

BACKFILL_SQL = """
UPDATE leads
SET status = 'engaged'
WHERE (
        status = 'replied'
        OR (
            status IN ('new', 'attempting_contact', 'contacted', 'nurture')
            AND (
                EXISTS (
                    SELECT 1
                    FROM sms_messages
                    WHERE sms_messages.lead_id = leads.id
                      AND sms_messages.user_id = leads.user_id
                      AND (
                          sms_messages.direction = 'inbound'
                          OR sms_messages.status = 'received'
                      )
                      AND UPPER(TRIM(COALESCE(sms_messages.message_body, ''))) NOT IN (
                          'STOP', 'STOPALL', 'UNSUBSCRIBE', 'CANCEL', 'END', 'QUIT',
                          'START', 'UNSTOP', 'YES', 'HELP'
                      )
                )
                OR EXISTS (
                    SELECT 1
                    FROM voice_calls
                    WHERE voice_calls.lead_id = leads.id
                      AND voice_calls.user_id = leads.user_id
                      AND voice_calls.status = 'completed'
                )
            )
        )
    )
  AND COALESCE(opt_out_status, '') <> 'opted_out'
  AND COALESCE(sms_consent_status, '') NOT IN ('opted_out', 'revoked')
"""


def _backfill(conn):
    conn.execute(BACKFILL_SQL)


def upgrade_sqlite(conn):
    _backfill(conn)


def upgrade_postgres(conn):
    _backfill(conn)
