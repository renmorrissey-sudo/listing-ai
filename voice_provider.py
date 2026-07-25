import json
import urllib.error
import urllib.request

import config


class VoiceProviderError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class VapiVoiceProvider:
    api_url = "https://api.vapi.ai/call"

    def __init__(self):
        self.api_key = config.VOICE_PROVIDER_API_KEY
        self.assistant_id = config.VOICE_DEFAULT_ASSISTANT_ID
        self.phone_number_id = config.VOICE_PHONE_NUMBER_ID

    def is_configured(self):
        return bool(self.api_key and self.phone_number_id)

    def start_outbound_call(self, call_id, call_data, persona, prompt):
        if not self.is_configured():
            raise VoiceProviderError("AI calling is not configured yet. Add your voice provider API key and phone number in Railway.")

        payload = {
            "phoneNumberId": self.phone_number_id,
            "customer": {
                "number": call_data["phone_number"],
                "name": call_data.get("lead_name") or "Lead",
                "numberE164CheckEnabled": True,
            },
            "metadata": {
                "topai_call_id": str(call_id),
                "persona_id": str(persona["id"]),
                "lead_type": call_data.get("lead_type", ""),
            },
        }

        if self.assistant_id:
            payload["assistantId"] = self.assistant_id
        else:
            payload["assistant"] = {
                "name": f"TopAI {persona['name']}",
                "firstMessage": f"Hi, this is an AI assistant calling on behalf of the real estate team. Is this {call_data.get('lead_name') or 'the person I am trying to reach'}?",
                "model": {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "system", "content": prompt}],
                },
                "artifactPlan": {
                    "recordingEnabled": True,
                    "transcriptPlan": {"enabled": True},
                },
            }

        request = urllib.request.Request(
            self.api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "TopAI-Real-Estate-Tools/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise VoiceProviderError(f"Voice provider rejected the call request: {detail}") from exc
        except urllib.error.URLError as exc:
            raise VoiceProviderError("Could not reach the voice provider. Please try again.") from exc

        provider_call_id = result.get("id") or result.get("callId")
        if not provider_call_id:
            raise VoiceProviderError("Voice provider did not return a call ID.")
        return {
            "provider_call_id": provider_call_id,
            "raw": result,
        }

    def get_recording_download_url(self, provider_call_id, kind="mono"):
        if not self.api_key:
            raise VoiceProviderError("AI calling is not configured yet.")
        if not provider_call_id:
            raise VoiceProviderError("Missing provider call ID for recording.")

        kinds = [kind] if kind != "mono" else ["mono", "stereo"]
        last_error = None
        for candidate in kinds:
            try:
                return self._fetch_recording_redirect(provider_call_id, candidate)
            except VoiceProviderError as exc:
                last_error = exc
        raise last_error or VoiceProviderError("Could not retrieve recording.")

    def _fetch_recording_redirect(self, provider_call_id, kind):
        artifact = "mono-recording" if kind == "mono" else f"{kind}-recording"
        url = f"{self.api_url}/{provider_call_id}/{artifact}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "*/*",
                "User-Agent": "TopAI-Real-Estate-Tools/1.0",
            },
            method="GET",
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=20) as response:
                location = response.headers.get("Location")
                if response.status in (301, 302, 303, 307, 308) and location:
                    return location
                if 200 <= response.status < 300 and response.geturl():
                    return response.geturl()
                raise VoiceProviderError("Voice provider did not return a recording URL.")
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            if exc.code in (301, 302, 303, 307, 308) and location:
                return location
            detail = exc.read().decode("utf-8", errors="ignore")
            raise VoiceProviderError(f"Could not retrieve recording: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise VoiceProviderError("Could not reach the voice provider for the recording.") from exc


def get_voice_provider():
    if config.VOICE_PROVIDER != "vapi":
        raise VoiceProviderError(f"Unsupported voice provider: {config.VOICE_PROVIDER}")
    return VapiVoiceProvider()


def _extract_recording_url(message, payload, call, artifact):
    recording = artifact.get("recording") or {}
    if isinstance(recording, str):
        recording_url = recording
    elif isinstance(recording, dict):
        mono = recording.get("mono")
        stereo = recording.get("stereo")
        recording_url = (
            recording.get("url")
            or recording.get("stereoUrl")
            or recording.get("monoUrl")
            or (mono if isinstance(mono, str) else None)
            or (stereo if isinstance(stereo, str) else None)
            or (mono.get("combinedUrl") if isinstance(mono, dict) else None)
            or (stereo.get("combinedUrl") if isinstance(stereo, dict) else None)
        )
    else:
        recording_url = None

    return (
        recording_url
        or artifact.get("recordingUrl")
        or artifact.get("stereoRecordingUrl")
        or message.get("recordingUrl")
        or message.get("stereoRecordingUrl")
        or message.get("recording_url")
        or payload.get("recording_url")
        or call.get("recordingUrl")
    )


def normalize_voice_webhook(payload):
    message = payload.get("message", payload)
    call = message.get("call") or payload.get("call") or {}
    artifact = message.get("artifact") or payload.get("artifact") or {}
    metadata = message.get("metadata") or call.get("metadata") or payload.get("metadata") or {}

    provider_call_id = (
        call.get("id")
        or message.get("callId")
        or payload.get("call_id")
        or payload.get("id")
    )
    internal_call_id = metadata.get("topai_call_id") or metadata.get("call_id")
    event_type = message.get("type") or payload.get("event") or payload.get("type")

    transcript = artifact.get("transcript") or payload.get("transcript")
    if not transcript and isinstance(message.get("transcript"), str):
        transcript = message.get("transcript")

    recording_url = _extract_recording_url(message, payload, call, artifact)

    summary = (
        (message.get("analysis") or {}).get("summary")
        or message.get("summary")
        or payload.get("summary")
        or call.get("summary")
        or (call.get("analysis") or {}).get("summary")
    )

    outcome = (
        message.get("endedReason")
        or payload.get("status")
        or call.get("status")
        or event_type
    )

    appointment_requested = False
    summary_text = " ".join(str(x or "").lower() for x in [summary, transcript, outcome])
    for marker in ("appointment", "booked", "scheduled", "meeting", "showing"):
        if marker in summary_text:
            appointment_requested = True
            break

    status = "completed" if event_type in ("end-of-call-report", "call_ended", "call_analyzed") else None

    return {
        "call_id": internal_call_id,
        "provider_call_id": provider_call_id,
        "status": status,
        "outcome": outcome,
        "transcript": transcript,
        "summary": summary,
        "recording_url": recording_url,
        "appointment_requested": appointment_requested,
    }
