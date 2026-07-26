from sms_coach import validate_coach_response


def test_validate_coach_response_phase2_schema():
    payload = """
    {
      "summary": "Lead wants a showing this weekend",
      "intent": "schedule showing",
      "recommended_next_action": "Propose two showing times",
      "draft_reply": "Happy to help — does Sat 11am or Sun 2pm work?",
      "confidence": 0.82,
      "sensitive_topic": false,
      "suggested_lead_status": "qualified",
      "suggested_follow_up_at": "2026-08-01T15:00:00+00:00",
      "suggested_follow_up_reason": "Confirm showing",
      "suggested_tasks": [{"title": "Send listing flyer", "task_type": "send_email"}],
      "appointment_requested": true,
      "appointment_details": {"type": "property_showing"},
      "needs_attention_reasons": ["appointment_requested"],
      "home_value_pitch": ""
    }
    """
    result = validate_coach_response(payload)
    assert result["draft_reply"].startswith("Happy to help")
    assert result["suggested_reply"] == result["draft_reply"]
    assert result["suggested_lead_status"] == "qualified"
    assert result["appointment_requested"] is True
    assert result["confidence"] == 0.82
    assert result["requires_manual_review"] is False
    assert len(result["suggested_tasks"]) == 1


def test_validate_coach_low_confidence_requires_review():
    result = validate_coach_response(
        '{"summary":"unclear","intent":"unknown","draft_reply":"Thanks","confidence":0.2}'
    )
    assert result["requires_manual_review"] is True


def test_validate_coach_maps_legacy_status():
    result = validate_coach_response(
        '{"summary":"x","intent":"y","draft_reply":"hi","confidence":0.9,"suggested_lead_status":"hot"}'
    )
    assert result["suggested_lead_status"] == "qualified"


def test_validate_coach_sensitive_escalation():
    result = validate_coach_response(
        '{"summary":"legal","intent":"legal","draft_reply":"I will check","confidence":0.9,'
        '"escalation_topics":["legal"]}'
    )
    assert result["sensitive_topic"] is True
    assert result["requires_manual_review"] is True
    assert "legal" in result["escalation_topics"]
