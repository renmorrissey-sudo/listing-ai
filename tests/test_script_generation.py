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


class _ThinkingBlock:
    """Stand-in for an Anthropic ThinkingBlock: no .text attribute."""

    def __init__(self):
        self.thinking = "thinking..."


class _TextBlock:
    def __init__(self, text):
        self.text = text


class _Message:
    def __init__(self, content):
        self.content = content


def test_message_text_ignores_thinking_block():
    """Regression: Nov 3-4, 2026 /generate-script 500s.

    The API returned 200 OK, but content[0] was a ThinkingBlock with no
    .text, so indexing it raised AttributeError. _message_text must skip
    it and return the TextBlock text instead.
    """
    from app import _message_text

    message = _Message([_ThinkingBlock(), _TextBlock("Hello")])
    assert _message_text(message) == "Hello"


def test_message_text_joins_multiple_text_blocks():
    from app import _message_text

    message = _Message([_TextBlock("One"), _TextBlock("Two")])
    assert _message_text(message) == "One\nTwo"


def test_message_text_empty_when_no_text_blocks():
    from app import _message_text

    assert _message_text(_Message([_ThinkingBlock()])) == ""
    assert _message_text(_Message([])) == ""
