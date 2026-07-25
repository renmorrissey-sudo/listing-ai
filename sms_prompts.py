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
