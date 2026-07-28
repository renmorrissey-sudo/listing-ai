"""Generate static/sms-opt-in-proof.png for Telnyx Toll-Free Verification."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 1750


def _font(size, bold=False):
    candidates = [
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main():
    img = Image.new("RGB", (W, H), "#eef3f9")
    draw = ImageDraw.Draw(img)

    f_logo = _font(28, True)
    f_small = _font(14)
    f_h1 = _font(34, True)
    f_label = _font(13, True)
    f_input = _font(16)
    f_consent = _font(15)
    f_btn = _font(18, True)
    f_link = _font(14)
    f_muted = _font(15)

    draw.rectangle([0, 0, W, 78], fill="#10243f")
    draw.text((36, 18), "TopAI ", font=f_logo, fill="white")
    bbox = draw.textbbox((36, 18), "TopAI ", font=f_logo)
    draw.text((bbox[2], 18), "RE Tools", font=f_logo, fill="#6ea8ff")
    draw.text((36, 50), "Operated by Sky Blue Holdings LLC", font=f_small, fill="#aabbcc")
    draw.text(
        (560, 28),
        "Home    How It Works    Privacy    Terms    Contact",
        font=f_small,
        fill="#dce6ff",
    )

    card_x, card_y = 90, 110
    card_w, card_h = 1020, 1520
    draw.rounded_rectangle(
        [card_x, card_y, card_x + card_w, card_y + card_h],
        radius=16,
        fill="white",
        outline="#e4ebf4",
    )

    x = card_x + 40
    y = card_y + 36
    draw.text((x, y), "Real estate inquiry & SMS consent", font=f_h1, fill="#10243f")
    y += 56

    intro = (
        "Use this public form to send a real estate inquiry to TopAI RE Tools\n"
        "(operated by Sky Blue Holdings LLC). Tell us what you are looking for —\n"
        "property questions, scheduling, or follow-up on a listing conversation."
    )
    draw.multiline_text((x, y), intro, font=f_muted, fill="#556677", spacing=4)
    y += 90

    draw.rounded_rectangle([x, y, x + 940, y + 130], radius=10, fill="#eef5ff", outline="#cfe0ff")
    support = (
        "SMS support number: (720) 903-2519\n"
        "Conversational SMS (when you opt in) may include property information, answers to your\n"
        "questions, appointment scheduling, reminders, and follow-up. Message frequency varies.\n"
        "Message and data rates may apply. Reply STOP to opt out or HELP for help.\n"
        "Consent is not a condition of purchasing goods or services."
    )
    draw.multiline_text((x + 14, y + 12), support, font=f_small, fill="#1a1a2e", spacing=3)
    y += 150

    draw.text((x, y), "FIRST NAME *", font=f_label, fill="#556677")
    draw.text((x + 490, y), "LAST NAME *", font=f_label, fill="#556677")
    y += 22
    draw.rounded_rectangle([x, y, x + 450, y + 44], radius=8, fill="#fafbfc", outline="#e0e4ed")
    draw.rounded_rectangle([x + 490, y, x + 940, y + 44], radius=8, fill="#fafbfc", outline="#e0e4ed")
    # Neutral sample placeholders only — not a real customer
    draw.text((x + 12, y + 12), "Jordan", font=f_input, fill="#1a1a2e")
    draw.text((x + 502, y + 12), "Sample", font=f_input, fill="#1a1a2e")
    y += 62

    draw.text((x, y), "MOBILE TELEPHONE NUMBER *", font=f_label, fill="#556677")
    y += 22
    draw.rounded_rectangle([x, y, x + 940, y + 44], radius=8, fill="#fafbfc", outline="#e0e4ed")
    draw.text((x + 12, y + 12), "+1XXXXXXXXXX", font=f_input, fill="#1a1a2e")
    y += 62

    draw.text((x, y), "EMAIL (OPTIONAL)", font=f_label, fill="#556677")
    y += 22
    draw.rounded_rectangle([x, y, x + 940, y + 44], radius=8, fill="#fafbfc", outline="#e0e4ed")
    draw.text((x + 12, y + 12), "optional@example.com", font=f_input, fill="#8899aa")
    y += 62

    draw.text((x, y), "REAL ESTATE INQUIRY / MESSAGE *", font=f_label, fill="#556677")
    y += 22
    draw.rounded_rectangle([x, y, x + 940, y + 110], radius=8, fill="#fafbfc", outline="#e0e4ed")
    draw.multiline_text(
        (x + 12, y + 12),
        "Sample inquiry only — interested in general\nproperty information (not a real customer).",
        font=f_input,
        fill="#1a1a2e",
        spacing=4,
    )
    y += 130

    draw.rounded_rectangle([x, y, x + 940, y + 280], radius=10, fill="#f8fafc", outline="#dde4f0")
    # Unchecked checkbox (empty white square)
    draw.rectangle([x + 16, y + 18, x + 34, y + 36], outline="#334155", width=2, fill="white")
    consent = (
        "I agree to receive conversational SMS messages from TopAI RE Tools regarding my\n"
        "real estate inquiry, including requested property information, responses to questions,\n"
        "appointment scheduling, reminders, and follow-up. Message frequency varies. Message and\n"
        "data rates may apply. Reply STOP to opt out or HELP for help. Consent is not a condition\n"
        "of purchasing goods or services."
    )
    draw.multiline_text((x + 46, y + 14), consent, font=f_consent, fill="#2a2a3e", spacing=3)
    links_y = y + 160
    draw.text((x + 16, links_y), "Privacy Policy:", font=f_link, fill="#556677")
    draw.text((x + 130, links_y), "https://topairealestatetools.com/privacy", font=f_link, fill="#2f6fed")
    draw.text((x + 16, links_y + 28), "Terms and Conditions:", font=f_link, fill="#556677")
    draw.text(
        (x + 190, links_y + 28),
        "https://topairealestatetools.com/terms",
        font=f_link,
        fill="#2f6fed",
    )
    draw.text(
        (x + 16, links_y + 64),
        "SMS consent checkbox is unchecked by default and must be checked to submit.",
        font=f_small,
        fill="#667788",
    )
    y += 300

    draw.rounded_rectangle([x, y, x + 940, y + 52], radius=9, fill="#2f6fed")
    btn = "Submit inquiry"
    bb = draw.textbbox((0, 0), btn, font=f_btn)
    bw = bb[2] - bb[0]
    draw.text((x + (940 - bw) // 2, y + 14), btn, font=f_btn, fill="white")
    y += 70
    note = (
        "The SMS consent checkbox is required and is unchecked by default.\n"
        "Submitting this form does not send an automated SMS.\n"
        "Message frequency varies. Message and data rates may apply.\n"
        "Reply STOP to opt out or HELP for help. Consent is not a condition of purchase."
    )
    draw.multiline_text((x, y), note, font=f_small, fill="#667788", spacing=3)

    draw.text(
        (90, H - 48),
        "TopAI RE Tools  ·  Opt-In Workflow proof image for Telnyx Toll-Free Verification",
        font=f_small,
        fill="#667788",
    )

    out = Path("static/sms-opt-in-proof.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG", optimize=True)
    print(f"Wrote {out} size={out.stat().st_size} {img.size}")


if __name__ == "__main__":
    main()
