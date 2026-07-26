def build_inbound_reply_analysis_prompt(lead, conversation, inbound_text):
    history_lines = []
    for msg in conversation[-12:]:
        direction = msg.get("direction") or ("inbound" if msg.get("status") == "received" else "outbound")
        who = "Lead" if direction == "inbound" else "Agent"
        history_lines.append(f"{who}: {msg.get('message_body') or ''}")
    history = "\n".join(history_lines) if history_lines else "(no prior messages)"

    return f"""Analyze this real estate lead SMS reply for the agent.

Return ONLY JSON with these keys:
- summary: concise summary of the lead's message
- intent: inferred intent in a short phrase
- recommended_next_action: concrete next action for the agent
- draft_reply: one SMS draft for agent approval (under 420 chars). If escalation is required, draft a brief acknowledgment that the agent will follow up personally.
- confidence: number from 0 to 1
- sensitive_topic: true if legal/financing/negotiation/fair housing/complaint/uncertain facts apply
- suggested_lead_status: one of new, attempting_contact, contacted, qualified, appointment_scheduled, appointment_completed, nurture, under_contract, closed_won, closed_lost, do_not_contact (suggestion only — never applied automatically)
- suggested_follow_up_at: ISO-8601 UTC datetime string or null
- suggested_follow_up_reason: short reason for the follow-up
- suggested_tasks: array of up to 5 objects {{title, task_type, due_at}} (suggestions only)
- appointment_requested: true if the lead asked to meet/call/show
- appointment_details: object with type/time hints or null
- needs_attention_reasons: array of reason codes if the agent should review urgently
- home_value_pitch: optional SMS pitching a home-value / seller consultation if relevant, else empty string
- escalation_topics: array subset of [legal, financing, negotiation, fair_housing, complaint, uncertain_property_fact]
- requires_manual_review: true if any escalation topic applies, confidence is low, or the reply is ambiguous/sensitive

RULES:
- Do not auto-send anything; this is advice for agent approval only.
- Do not assume status, follow-up, tasks, or appointments will be applied — agent must approve.
- Be compliant and professional.
- Escalate legal, financing, negotiation, fair-housing, complaint, and uncertain property-fact topics for manual handling.
- If the lead is months away or must sell first, prefer nurture + schedule follow-up and consider a home-value pitch.
- If they ask to stop/unsubscribe, suggest do_not_contact and leave draft_reply empty.

LEAD:
- Name: {lead.get("name") or "Lead"}
- Phone: {lead.get("phone_number")}
- Type: {lead.get("lead_type") or "N/A"}
- Property interest: {lead.get("property_interest") or "N/A"}
- Current status: {lead.get("status") or "N/A"}
- Opt-out status: {lead.get("opt_out_status") or "active"}
- Notes: {lead.get("notes") or "N/A"}

CONVERSATION HISTORY:
{history}

LATEST LEAD REPLY:
{inbound_text}
"""


def build_sms_prompt(persona, sms_data):
    return f"""You write short, compliant real estate SMS messages for agents.

RULES:
- Write ONE SMS only, under 320 characters if possible, never over 450.
- Sound human, clear, and professional. No emojis unless naturally helpful.
- Identify that this is on behalf of a real estate professional.
- Do not claim to be a licensed agent unless notes say the sender is the agent.
- Do not give legal, mortgage, tax, or investment advice.
- Include a clear next step (reply, call, or book a time).
- Do not include links unless provided in the notes.
- Do not wrap the message in quotes or labels.

PERSONA:
- Name: {persona.get("name")}
- Type: {persona.get("persona_type")}
- Tone: {persona.get("tone")}
- Goal: {persona.get("goal")}
- Instructions: {persona.get("prompt")}

SMS DETAILS:
- Agent / sender name: {sms_data.get("agent_name") or "the agent"}
- Lead name: {sms_data.get("lead_name") or "there"}
- Lead type: {sms_data.get("lead_type") or "N/A"}
- Property interest: {sms_data.get("property_interest") or "N/A"}
- Desired outcome: {sms_data.get("desired_outcome") or "N/A"}
- Agent notes: {sms_data.get("notes") or "N/A"}

Return only the SMS body text."""
