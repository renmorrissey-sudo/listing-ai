import os
from flask import Flask, render_template, request, jsonify
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


def build_prompt(data):
    features = data.get("features", "").strip()
    feature_line = f"\n- Additional features: {features}" if features else ""

    return f"""You are an expert real estate copywriter. Generate compelling, professional content for this property listing.

PROPERTY DETAILS:
- Address: {data.get("address", "N/A")}
- Price: ${data.get("price", "N/A")}
- Bedrooms: {data.get("beds", "N/A")}
- Bathrooms: {data.get("baths", "N/A")}
- Square footage: {data.get("sqft", "N/A")} sq ft
- Year built: {data.get("year_built", "N/A")}
- Garage: {data.get("garage", "None")}
- Pool: {data.get("pool", "No")}{feature_line}
- Neighborhood highlights: {data.get("neighborhood", "N/A")}

Generate exactly three sections, clearly labeled:

---LISTING DESCRIPTION---
Write a compelling MLS listing description (150-200 words). Start with a strong hook. Highlight the best features. End with a call to action. Do NOT use the word "nestled" or "stunning".

---SOCIAL POSTS---
Write 3 social media posts:
1. [INSTAGRAM] (~150 chars with 5 relevant hashtags)
2. [FACEBOOK] (2-3 sentences, conversational tone, include price)
3. [X/TWITTER] (~200 chars punchy and direct)

---PROSPECT EMAIL---
Write a short email (subject line + 3 paragraphs) to send to a prospect list announcing this listing. Professional but warm tone. Include a clear call to action to schedule a showing."""


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json()

    required = ["address", "price", "beds", "baths", "sqft"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    try:
        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": build_prompt(data)}],
        )
        raw = message.content[0].text

        listing = _extract_section(raw, "LISTING DESCRIPTION", "SOCIAL POSTS")
        social = _extract_section(raw, "SOCIAL POSTS", "PROSPECT EMAIL")
        email = _extract_section(raw, "PROSPECT EMAIL", None)

        return jsonify({
            "listing": listing.strip(),
            "social": social.strip(),
            "email": email.strip(),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _extract_section(text, start_marker, end_marker):
    start = text.find(f"---{start_marker}---")
    if start == -1:
        return ""
    start += len(f"---{start_marker}---")
    if end_marker:
        end = text.find(f"---{end_marker}---", start)
        return text[start:end] if end != -1 else text[start:]
    return text[start:]


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("\n⚠️  ERROR: ANTHROPIC_API_KEY not set.")
        print("Create a .env file with: ANTHROPIC_API_KEY=your-key-here\n")
    else:
        port = int(os.environ.get("PORT", 5000))
        print(f"\n✅ ListingAI is running → http://localhost:{port}\n")
        app.run(host="0.0.0.0", debug=False, port=port)
