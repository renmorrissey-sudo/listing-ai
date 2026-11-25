def _slot_label(start_at_iso, timezone_name):
    """Human slot label, falling back to the raw ISO string."""
    try:
        from harness.appointment_requests import format_slot_label

        return format_slot_label(start_at_iso, timezone_name or "America/Denver")
    except Exception:
        return str(start_at_iso or "")


def build_verified_facts_block(open_requests=None, confirmed_requests=None,
                               property_interest=None, timezone_name=None):
    """Server-assembled VERIFIED FACTS block for the draft-only prompt.

    Only facts the app actually holds may be stated. There is no live
    listing-availability source in the app (the only listing store is
    ``listing_generations`` marketing content), so home availability is
    always "NOT in the app's data" — the draft must say the agent will
    check instead of stating availability.
    """
    confirmed_line = "none"
    for req in confirmed_requests or []:
        slot = (req or {}).get("requested_start_at")
        if slot:
            kind = (req or {}).get("appointment_type") or "showing"
            confirmed_line = f"{_slot_label(slot, timezone_name)} ({kind})"
            break
    open_line = "none"
    for req in open_requests or []:
        slot = (req or {}).get("requested_start_at")
        if slot:
            open_line = (
                f"{_slot_label(slot, timezone_name)} (awaiting agent confirmation)"
            )
        else:
            open_line = "requested, no time parsed"
        break
    return (
        "VERIFIED FACTS (the only facts you may state — anything not listed here is unknown):\n"
        f"- Confirmed appointment: {confirmed_line}\n"
        f"- Open appointment request: {open_line}\n"
        f"- Property interest (lead's own words, not verified): {property_interest or 'none stated'}\n"
        "- Home availability: NOT in the app's data — never state a home is available or unavailable.\n"
    )


def build_inbound_reply_analysis_prompt(lead, conversation, inbound_text,
                                         approved_templates=None,
                                         recall_context=None,
                                         draft_only=False,
                                         verified_facts=None):
    """Inbound coach prompt (Phase III-5 adds the approved-templates
    list and the customer-context recall block).

    ``approved_templates`` is the tenant's approved SMS templates
    (already capped at 20, most recently approved first) as row
    dicts. ``recall_context`` is the read-only per-lead recall block
    from ``harness.memory.build_recall_context``. ``draft_only`` is the
    custom-reply-draft variant: the draft is for the agent to review,
    edit, and send — TopAI never sends it — and ``verified_facts`` is
    the server-assembled VERIFIED FACTS block (see
    :func:`build_verified_facts_block`).
    """
    history_lines = []
    for msg in conversation[-12:]:
        direction = msg.get("direction") or ("inbound" if msg.get("status") == "received" else "outbound")
        who = "Lead" if direction == "inbound" else "Agent"
        history_lines.append(f"{who}: {msg.get('message_body') or ''}")
    history = "\n".join(history_lines) if history_lines else "(no prior messages)"

    templates_section = ""
    templates = list(approved_templates or [])[:20]
    if templates:
        template_lines = []
        for tpl in templates:
            template_lines.append(
                f"- #{tpl.get('id')} \"{tpl.get('name')}\" "
                f"(purpose: {tpl.get('purpose') or 'general'}): "
                f"{tpl.get('body')}"
            )
        templates_section = (
            "\nAPPROVED TEMPLATES (agent-approved only — use one of these "
            "verbatim or with its slots filled when one fits; never invent "
            "a template or present invented text as approved):\n"
            + "\n".join(template_lines)
            + "\nFill a template's {slots} from the lead record "
            "(lead_name, property_interest, qualification_budget, "
            "qualification_timeline), the agent profile (agent_name, "
            "brokerage_name, company_name), or the conversation context "
            "(appointment_time, follow_up_reason). If a slot has no "
            "value, do not use that template.\n"
        )

    recall_section = ""
    if recall_context and str(recall_context).strip():
        recall_section = (
            "\nCUSTOMER CONTEXT (read-only recall from the lead's CRM "
            "record — summaries only, never raw message bodies; use it to "
            "personalise the reply, never to change the lead):\n"
            f"{recall_context}\n"
        )

    if draft_only:
        intro_line = "Draft a reply for the agent to review, edit, and send."
        draft_reply_line = (
            "- draft_reply: one SMS reply for the agent to review, edit, and send "
            "(under 420 chars). If escalation is required, draft a brief "
            "acknowledgment that the agent will follow up personally."
        )
        send_rule_line = (
            "- TopAI will NOT send this draft. The agent reviews, edits, and sends it "
            "— write the reply, not a suggestion for the agent."
        )
        draft_only_rules = (
            "- Share verified availability and ask what time works ONLY when VERIFIED "
            "FACTS lists it; confirm a showing ONLY when VERIFIED FACTS lists a "
            "confirmed appointment (never on a requested/lead_confirmed slot — the "
            "agent confirms it first). When availability (or price/features/terms) "
            "is not in VERIFIED FACTS, draft a reply that says the agent will check "
            "— and do NOT state the fact.\n"
            "- Set escalation_topics when the lead's message or the draft touches: "
            "price/term negotiation → negotiation; mortgage/rates/affordability "
            "advice → financing; neighbourhood demographics/composition "
            "(\"what kind of people live here\", schools-by-demographics, "
            "crime-by-demographics) → fair_housing.\n"
        )
    else:
        intro_line = (
            "Analyze this real estate lead SMS reply and produce the next autonomous action."
        )
        draft_reply_line = (
            "- draft_reply: one SMS reply TopAI will send automatically (under 420 chars). "
            "If escalation is required, draft a brief acknowledgment that the agent will "
            "follow up personally."
        )
        send_rule_line = (
            "- TopAI will send draft_reply automatically when compliance allows. Write the "
            "actual reply, not a suggestion for the agent."
        )
        draft_only_rules = ""

    verified_facts_section = ""
    if verified_facts and str(verified_facts).strip():
        verified_facts_section = f"\n{verified_facts}\n"

    return f"""{intro_line}

Return ONLY JSON with these keys:
- summary: concise summary of the lead's message
- intent: inferred intent in a short phrase
- recommended_next_action: concrete next action TopAI should take or already took
{draft_reply_line}
- template_id: integer id of the approved template draft_reply uses, or null when no approved template was used (free compose). Only ever an id from the APPROVED TEMPLATES list below.
- confidence: number from 0 to 1
- sensitive_topic: true if legal/financing/negotiation/fair housing/complaint/uncertain facts apply
- suggested_lead_status: one of new, attempting_contact, contacted, qualified, appointment_scheduled, nurture (never closed_won, closed_lost, or do_not_contact unless the lead opted out)
- suggested_follow_up_at: ISO-8601 UTC datetime string or null
- suggested_follow_up_reason: short reason for the follow-up
- suggested_tasks: array of up to 5 objects {{title, task_type, due_at}}
- appointment_requested: true if the lead asked to meet/call/show or accepted a time
- appointment_details: object with day/time/window/type (type: one of phone_call, in_person, video_call) or null
- captured_note: useful CRM context from the lead to save, or empty string
- property_criteria_updates: object of fields the lead clearly changed (price_min, price_max, bedrooms, bathrooms, city, neighborhood, property_type) or empty object
- needs_attention_reasons: array of reason codes only if a human must handle an exception
- home_value_pitch: optional SMS pitching a home-value / seller consultation if relevant, else empty string
- escalation_topics: array subset of [legal, financing, negotiation, fair_housing, complaint, uncertain_property_fact]
- requires_manual_review: true ONLY if an escalation topic applies or the situation is sensitive

RULES:
{send_rule_line}
- Carry the conversation toward the lead's desired outcome (showing, call, nurture, or stop pursuing).
- If the lead names a time, put it in appointment_details so TopAI can hold the request for the agent to confirm. Never claim a booking in the reply — the agent confirms it first.
- If the lead accepts an offered alternative time, set appointment_requested true and include that time.
- If the lead is no longer interested, respond politely, set suggested_lead_status to nurture, and do not keep pitching.
- Capture useful property/context facts in captured_note and property_criteria_updates. Do not clear unspecified criteria fields.
- Escalate legal, financing, negotiation, fair-housing, complaint, and uncertain property-fact topics. Do not auto-negotiate legal/financial terms.
- uncertain_property_fact applies ONLY when the draft states a property fact (price, availability, features, terms) that cannot be verified from the lead record and conversation. A reply that only acknowledges the question and promises the agent will verify and follow up — without stating any property fact — is NOT an escalation; leave escalation_topics empty for it. Flagging the topic must reflect what the draft asserts, not merely what the lead asked about.
{draft_only_rules}- If they ask to stop/unsubscribe, suggest do_not_contact and leave draft_reply empty.
- Be compliant and professional.
- When an approved template fits the reply, use it verbatim or with its slots filled and set template_id to its id. When no approved template fits, compose freely and set template_id to null. Never claim a template id that is not in the APPROVED TEMPLATES list.

LEAD:
- Name: {lead.get("name") or "Lead"}
- Phone: {lead.get("phone_number")}
- Type: {lead.get("lead_type") or "N/A"}
- Property interest: {lead.get("property_interest") or "N/A"}
- Current status: {lead.get("status") or "N/A"}
- Opt-out status: {lead.get("opt_out_status") or "active"}
- Notes: {lead.get("notes") or "N/A"}
{templates_section}{recall_section}{verified_facts_section}CONVERSATION HISTORY:
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
