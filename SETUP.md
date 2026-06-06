# ListingAI — Setup Guide (Windows)

## What you need
- Python 3.10+ ([download here](https://www.python.org/downloads/))
- An Anthropic API key ([get one here](https://console.anthropic.com/))

---

## Step 1 — Get your API key

1. Go to https://console.anthropic.com/
2. Sign up or log in
3. Click **API Keys** → **Create Key**
4. Copy the key (starts with `sk-ant-...`)

---

## Step 2 — Create your .env file

In the `real-estate-ai` folder, create a file named `.env` (no extension) containing:

```
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

Replace `sk-ant-your-key-here` with your actual key.

---

## Step 3 — Install dependencies

Open **Command Prompt** or **PowerShell** in the `real-estate-ai` folder and run:

```
pip install -r requirements.txt
```

---

## Step 4 — Run the app

```
python app.py
```

You should see:
```
✅ ListingAI is running → http://localhost:5000
```

Open your browser and go to **http://localhost:5000**

---

## Cost estimate

Each listing generation costs roughly **$0.01–0.02** with the Claude API.
At $99/month per agent, you break even at **5 agents**. Everything above that is profit.

---

## Showing it to prospects

1. Run the app on your machine
2. Open http://localhost:5000 in your browser
3. Fill in a real local property and generate
4. Show them the output — listing description + 3 social posts + email in ~10 seconds

That's your demo. Most agents will want this immediately.

---

## Next steps after first paying customers

- Move from `claude-opus-4-6` to a local model (Ollama + Llama 3) to eliminate per-use costs
- Add a simple login/password so each client gets their own account
- Add a "history" page showing past generated listings
- Raise the price to $149/month
