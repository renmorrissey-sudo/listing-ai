from app import _strip_markdown, build_script_prompt


def test_strip_markdown_removes_bold_markers():
    raw = "**1. 'I'm not interested'**\n**Response:** I understand."
    assert _strip_markdown(raw) == "1. 'I'm not interested'\nResponse: I understand."
    assert "**" not in _strip_markdown(raw)


def test_strip_markdown_removes_headings_and_bullets():
    raw = "## Objections\n- First point\n- Second point"
    assert _strip_markdown(raw) == "Objections\nFirst point\nSecond point"


def test_script_prompt_requests_plain_text():
    prompt = build_script_prompt({"area": "Testville"})
    assert "Plain text only" in prompt
    assert "markdown" in prompt.lower()


def test_script_model_is_not_opus_by_default():
    import config

    assert config.SCRIPT_CLAUDE_MODEL != config.CLAUDE_MODEL
    assert config.SCRIPT_CLAUDE_MODEL == "claude-sonnet-5"
