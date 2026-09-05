"""Small dependency-free HTTP helpers for customer email providers."""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from email_campaign_providers.base import EmailCampaignProviderError

logger = logging.getLogger(__name__)


def query_url(base, params):
    return f"{base}?{urllib.parse.urlencode(params)}"


def request_json(
    method,
    url,
    *,
    bearer=None,
    basic=None,
    json_body=None,
    form_body=None,
    provider="Email provider",
    operation="create or validate this draft",
    timeout=25,
):
    headers = {
        "Accept": "application/json",
        "User-Agent": "TopAI-Real-Estate-Tools/1.0",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if basic:
        encoded = base64.b64encode(
            f"{basic[0]}:{basic[1]}".encode("utf-8")
        ).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    body = None
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(json_body).encode("utf-8")
    elif form_body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urllib.parse.urlencode(form_body).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        logger.warning(
            "%s API request failed method=%s host=%s status=%s body=%s",
            provider,
            method,
            urllib.parse.urlparse(url).netloc,
            exc.code,
            raw[:500],
        )
        reconnect = exc.code in (401, 403)
        message = (
            f"{provider} authorization expired. Reconnect this account."
            if reconnect
            else f"{provider} could not {operation}."
        )
        raise EmailCampaignProviderError(
            message,
            error_code=(
                "authentication_failed" if reconnect else f"http_{exc.code}"
            ),
            uncertain=method not in ("GET", "HEAD") and exc.code >= 500,
            reconnect_required=reconnect,
        ) from None
    except urllib.error.URLError:
        logger.warning(
            "%s API network failure method=%s host=%s",
            provider,
            method,
            urllib.parse.urlparse(url).netloc,
        )
        raise EmailCampaignProviderError(
            f"TopAI could not confirm the {provider} result. Check the "
            "provider before trying again.",
            error_code="network_error",
            uncertain=method not in ("GET", "HEAD"),
        ) from None
    except (ValueError, json.JSONDecodeError):
        raise EmailCampaignProviderError(
            f"{provider} returned an unexpected response.",
            error_code="invalid_response",
            uncertain=method not in ("GET", "HEAD"),
        ) from None
