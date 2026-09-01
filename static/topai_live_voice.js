import Vapi from "https://cdn.jsdelivr.net/npm/@vapi-ai/web@2.6.1/+esm";

const button = document.getElementById("topai-live-button");
const panel = document.getElementById("topai-live-panel");
const endButton = document.getElementById("topai-live-end");
const status = document.getElementById("topai-live-status");
const transcript = document.getElementById("topai-live-transcript");
const configElement = document.getElementById("topai-live-config");

if (button && panel && endButton && status && transcript && configElement) {
  const config = JSON.parse(configElement.textContent || "{}");
  const vapi = new Vapi(config.publicKey);
  let callState = "idle";

  function setState(nextState, label) {
    callState = nextState;
    panel.dataset.state = nextState;
    status.textContent = label;
    button.dataset.callState = nextState === "idle" ? "idle" : "active";
    button.textContent = nextState === "idle" ? "Ask TopAI" : "End TopAI";
    button.disabled = nextState === "connecting";
  }

  function addTranscript(role, text, replacePartial = false) {
    if (!text) return;
    const partial = transcript.querySelector(`[data-partial="${role}"]`);
    if (replacePartial && partial) {
      partial.lastChild.textContent = text;
      transcript.scrollTop = transcript.scrollHeight;
      return;
    }
    if (!replacePartial && partial) partial.removeAttribute("data-partial");
    const line = document.createElement("p");
    line.className = "topai-live-line";
    if (replacePartial) line.dataset.partial = role;
    const name = document.createElement("strong");
    name.textContent = role === "user" ? "You: " : "TopAI: ";
    line.append(name, document.createTextNode(text));
    transcript.appendChild(line);
    transcript.scrollTop = transcript.scrollHeight;
  }

  function showError(error) {
    const raw = error?.error?.message || error?.message || "The live conversation could not start.";
    panel.hidden = false;
    transcript.replaceChildren();
    const line = document.createElement("p");
    line.className = "topai-live-line topai-live-error";
    line.textContent = /microphone/i.test(raw)
      ? "Allow microphone access in your browser, then click Ask TopAI again."
      : "TopAI could not connect. Please try again.";
    transcript.appendChild(line);
    setState("idle", "Disconnected");
  }

  async function startCall() {
    panel.hidden = false;
    transcript.innerHTML = '<p class="topai-live-line">Starting live conversation...</p>';
    setState("connecting", "Connecting...");
    try {
      await vapi.start(config.assistantId, config.assistantOverrides);
    } catch (error) {
      showError(error);
    }
  }

  function endCall() {
    if (callState === "idle") return;
    vapi.stop();
    setState("idle", "Conversation ended");
  }

  vapi.on("call-start", () => {
    transcript.replaceChildren();
    setState("listening", "Listening");
  });
  vapi.on("call-end", () => setState("idle", "Conversation ended"));
  vapi.on("speech-start", () => setState("speaking", "TopAI is speaking"));
  vapi.on("speech-end", () => setState("listening", "Listening"));
  vapi.on("message", (message) => {
    if (message?.type !== "transcript") return;
    const isPartial = message.transcriptType === "partial";
    addTranscript(message.role, message.transcript, isPartial);
  });
  vapi.on("error", showError);

  button.addEventListener("click", () => {
    if (callState === "idle") startCall();
    else endCall();
  });
  endButton.addEventListener("click", endCall);
  window.addEventListener("pagehide", () => {
    if (callState !== "idle") vapi.stop();
  });

  button.disabled = false;
}
