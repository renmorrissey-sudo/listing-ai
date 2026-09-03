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
  const PLAYBACK_GUARD_MS = 650;
  const MIN_BARGE_IN_MS = 350;
  let assistantIsSpeaking = false;
  let assistantPlaybackStartedAt = 0;
  let currentResponseId = null;
  let potentialInterruptionStartedAt = 0;
  let interruptionWasIntentional = false;
  let lastInterruptionClassification = "none";
  let lastAssistantText = "";

  function setState(nextState, label) {
    callState = nextState;
    panel.dataset.state = nextState;
    status.textContent = label;
    button.dataset.callState = nextState === "idle" ? "idle" : "active";
    button.textContent = nextState === "idle" ? "Ask TopAI" : "End TopAI";
    button.disabled = nextState === "connecting";
  }

  function playbackAge(now = Date.now()) {
    return assistantPlaybackStartedAt ? now - assistantPlaybackStartedAt : null;
  }

  function logEvent(eventName, details = {}) {
    const now = Date.now();
    const entry = {
      at: new Date(now).toISOString(),
      event: eventName,
      responseId: details.responseId || currentResponseId || null,
      assistantIsSpeaking,
      playbackAgeMs: playbackAge(now),
      interruptionDurationMs: potentialInterruptionStartedAt
        ? now - potentialInterruptionStartedAt
        : null,
      uiState: callState,
      interruptionClassification: lastInterruptionClassification,
      ...details,
    };
    console.debug("[TopAI Realtime]", entry);
  }

  function responseIdFromMessage(message) {
    return (
      message?.response?.id ||
      message?.responseId ||
      message?.turnId ||
      (message?.turn !== undefined ? `turn-${message.turn}` : null) ||
      currentResponseId
    );
  }

  function markAssistantPlaybackStarted(responseId, source) {
    const wasSpeaking = assistantIsSpeaking;
    assistantIsSpeaking = true;
    if (!assistantPlaybackStartedAt) assistantPlaybackStartedAt = Date.now();
    currentResponseId = responseId || currentResponseId || `local-${assistantPlaybackStartedAt}`;
    if (!wasSpeaking) {
      potentialInterruptionStartedAt = 0;
      interruptionWasIntentional = false;
    }
    lastInterruptionClassification = "assistant_playback";
    setState("speaking", "TopAI is speaking");
    logEvent(source || "response.created", {responseId: currentResponseId});
  }

  function markAssistantPlaybackDone(source, details = {}) {
    logEvent(source, details);
    assistantIsSpeaking = false;
    assistantPlaybackStartedAt = 0;
    potentialInterruptionStartedAt = 0;
    currentResponseId = null;
    if (callState !== "idle") setState("listening", "Listening");
  }

  function clearPotentialInterruption(classification) {
    lastInterruptionClassification = classification;
    potentialInterruptionStartedAt = 0;
    logEvent(classification);
  }

  function startPotentialInterruptionTimer(source) {
    if (!potentialInterruptionStartedAt) {
      potentialInterruptionStartedAt = Date.now();
    }
    lastInterruptionClassification = "potential_user_interruption";
    logEvent(source || "speech_started_potential_interruption");
  }

  function confirmUserInterruption(source) {
    interruptionWasIntentional = true;
    lastInterruptionClassification = "validated_user_interruption";
    logEvent(source || "user_interruption_validated");
  }

  function requestAssistantContinuation(reason) {
    if (callState === "idle") return;
    const prompt = [
      "The previous assistant response appears to have been cancelled by echo or transient noise, not an intentional user interruption.",
      "Continue the incomplete TopAI response naturally from the conversation context.",
      lastAssistantText ? `Most recent assistant text before cancellation: "${lastAssistantText}"` : "",
      "Do not apologize for a technical issue unless the user asks what happened.",
    ].filter(Boolean).join(" ");
    logEvent("assistant_continuation_requested", {reason});
    if (typeof vapi.send === "function") {
      vapi.send({
        type: "add-message",
        message: {role: "system", content: prompt},
      });
      return;
    }
    if (typeof vapi.addMessage === "function") {
      vapi.addMessage({role: "system", content: prompt});
      return;
    }
    logEvent("assistant_continuation_unavailable");
  }

  function handleUserSpeechStarted(source) {
    logEvent(source || "input_audio_buffer.speech_started");
    if (!assistantIsSpeaking) {
      lastInterruptionClassification = "user_turn_started";
      setState("listening", "Listening");
      return;
    }
    if ((playbackAge() || 0) < PLAYBACK_GUARD_MS) {
      clearPotentialInterruption("speech_started_ignored_playback_guard");
      return;
    }
    startPotentialInterruptionTimer("speech_started_after_playback_guard");
  }

  function handleUserSpeechStopped(source) {
    logEvent(source || "input_audio_buffer.speech_stopped");
    if (!assistantIsSpeaking || !potentialInterruptionStartedAt) {
      lastInterruptionClassification = "user_turn_stopped";
      return;
    }
    const interruptionDuration = Date.now() - potentialInterruptionStartedAt;
    if (interruptionDuration < MIN_BARGE_IN_MS) {
      clearPotentialInterruption("speech_stopped_ignored_short_barge_in");
      return;
    }
    confirmUserInterruption("speech_stopped_validated_barge_in");
  }

  function handleResponseCancelled(source, message = {}) {
    const intentional = interruptionWasIntentional;
    logEvent(source || "response.cancelled", {
      intentional,
      responseId: responseIdFromMessage(message),
    });
    assistantIsSpeaking = false;
    assistantPlaybackStartedAt = 0;
    potentialInterruptionStartedAt = 0;
    currentResponseId = null;
    if (intentional) {
      if (callState !== "idle") setState("listening", "Listening");
    } else {
      lastInterruptionClassification = "accidental_cancellation";
      requestAssistantContinuation(source || "response.cancelled");
    }
    interruptionWasIntentional = false;
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
    logEvent("call_stop_requested");
    vapi.stop();
    setState("idle", "Conversation ended");
  }

  vapi.on("call-start", () => {
    transcript.replaceChildren();
    logEvent("call-start");
    setState("listening", "Listening");
  });
  vapi.on("call-end", () => {
    logEvent("call-end");
    assistantIsSpeaking = false;
    assistantPlaybackStartedAt = 0;
    currentResponseId = null;
    potentialInterruptionStartedAt = 0;
    setState("idle", "Conversation ended");
  });
  vapi.on("speech-start", () => markAssistantPlaybackStarted(null, "speech-start"));
  vapi.on("speech-end", () => markAssistantPlaybackDone("speech-end"));
  vapi.on("message", (message) => {
    const type = message?.type;
    if (!type) return;
    if (type !== "transcript") logEvent(`message.${type}`, {responseId: responseIdFromMessage(message)});

    if (type === "transcript") {
      const isPartial = message.transcriptType === "partial";
      addTranscript(message.role, message.transcript, isPartial);
      if (message.role === "assistant" && message.transcriptType === "final") {
        lastAssistantText = message.transcript || lastAssistantText;
      }
      return;
    }

    if (type === "speech-update") {
      if (message.role === "assistant" && message.status === "started") {
        markAssistantPlaybackStarted(responseIdFromMessage(message), "response.created");
      } else if (message.role === "assistant" && message.status === "stopped") {
        markAssistantPlaybackDone("response.audio.done", {responseId: responseIdFromMessage(message)});
      } else if (message.role === "user" && message.status === "started") {
        handleUserSpeechStarted("input_audio_buffer.speech_started");
      } else if (message.role === "user" && message.status === "stopped") {
        handleUserSpeechStopped("input_audio_buffer.speech_stopped");
      }
      return;
    }

    if (type === "assistant.speechStarted") {
      if (message.text) lastAssistantText = message.text;
      markAssistantPlaybackStarted(responseIdFromMessage(message), "assistant.speechStarted");
      return;
    }

    if (type === "user-interrupted") {
      const duration = potentialInterruptionStartedAt
        ? Date.now() - potentialInterruptionStartedAt
        : null;
      if (
        assistantIsSpeaking &&
        (playbackAge() || 0) >= PLAYBACK_GUARD_MS &&
        duration !== null &&
        duration >= MIN_BARGE_IN_MS
      ) {
        confirmUserInterruption("response.cancel_observed_after_validated_interruption");
      } else {
        lastInterruptionClassification = "unvalidated_user_interruption";
      }
      handleResponseCancelled("response.cancelled", message);
      return;
    }

    if (type === "status-update" && message.status === "ended") {
      markAssistantPlaybackDone("response.done", {responseId: responseIdFromMessage(message)});
    }
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
