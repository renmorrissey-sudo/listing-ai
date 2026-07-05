import json
import urllib.error
import urllib.request

import config


class VoiceProviderError(Exception):
    pass


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


def get_voice_provider():
    if config.VOICE_PROVIDER != "vapi":
        raise VoiceProviderError(f"Unsupported voice provider: {config.VOICE_PROVIDER}")
    return VapiVoiceProvider()


def normalize_voice_webhook(payload):
    message = payload.get("message", payload)
    call = message.get("call") or payload.get("call") or {}
    artifact = message.get("artifact") or payload.get("artifact") or {}

    provider_call_id = (
        call.get("id")
        or message.get("callId")
        or payload.get("call_id")
        or payload.get("id")
    )
    event_type = message.get("type") or payload.get("event") or payload.get("type")

    transcript = artifact.get("transcript") or payload.get("transcript")
    if not transcript and isinstance(message.get("transcript"), str):
        transcript = message.get("transcript")

    recording = artifact.get("recording") or {}
    recording_url = (
        recording.get("url")
        or recording.get("stereoUrl")
        or payload.get("recording_url")
        or call.get("recordingUrl")
    )

    summary = (
        message.get("summary")
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

    status = "completed" if event_type in ("end-of-call-report", "call_ended", "call_analyzed") else (outcome or "updated")

    return {
        "provider_call_id": provider_call_id,
        "status": status,
        "outcome": outcome,
        "transcript": transcript,
        "summary": summary,
        "recording_url": recording_url,
        "appointment_requested": appointment_requested,
    }
