const button = document.getElementById("topai-live-button");
const panel = document.getElementById("topai-live-panel");
const endButton = document.getElementById("topai-live-end");
const status = document.getElementById("topai-live-status");
const transcript = document.getElementById("topai-live-transcript");
const configElement = document.getElementById("topai-live-config");
const historyToggle = document.getElementById("topai-live-history-toggle");
const historyPanel = document.getElementById("topai-live-history");
const historyList = document.getElementById("topai-live-history-list");

if (button && panel && endButton && status && transcript && configElement) {
  const config = JSON.parse(configElement.textContent || "{}");
  const isCallWindow = config.mode === "window" || document.body.classList.contains("topai-live-window");
  const voiceConfigured = Boolean(config.configured && config.publicKey && config.assistantConfig);
  let vapi = null;
  let vapiHandlersAttached = false;
  const channel = "BroadcastChannel" in window ? new BroadcastChannel("topai-live") : null;
  const ACTIVE_KEY = "topai-live-active-session";
  const WINDOW_NAME = "topaiAskLive";
  const ACTIVE_SESSION_TTL_MS = 10000;
  const OPEN_WINDOW_TIMEOUT_MS = 5000;
  const SDK_LOAD_TIMEOUT_MS = 10000;
  const START_TIMEOUT_MS = 15000;
  const PLAYBACK_GUARD_MS = 650;
  const MIN_BARGE_IN_MS = 350;
  let callState = "idle";
  let callWindowRef = null;
  let openWindowTimer = null;
  let heartbeatTimer = null;
  let sessionId = config.sessionId || "";
  let turns = [];
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
    button.textContent = nextState === "idle" ? "Ask TopAI" : "TopAI Live";
    button.disabled = nextState === "connecting" && !isCallWindow;
  }

  function activeSession() {
    try {
      const data = JSON.parse(localStorage.getItem(ACTIVE_KEY) || "null");
      if (!data) return null;
      if (Date.now() - Number(data.updatedAt || 0) > ACTIVE_SESSION_TTL_MS) {
        localStorage.removeItem(ACTIVE_KEY);
        return null;
      }
      return data;
    } catch (error) {
      return null;
    }
  }

  function setActiveSession(data) {
    try {
      if (data) localStorage.setItem(ACTIVE_KEY, JSON.stringify(data));
      else localStorage.removeItem(ACTIVE_KEY);
    } catch (error) {}
  }

  function ensureSessionId() {
    if (!sessionId) {
      sessionId = crypto?.randomUUID ? crypto.randomUUID() : `topai-${Date.now()}`;
    }
    return sessionId;
  }

  function broadcast(type, payload = {}) {
    const message = {type, sessionId, state: callState, turns, updatedAt: Date.now(), ...payload};
    if (channel) channel.postMessage(message);
    try {
      localStorage.setItem(`${ACTIVE_KEY}-ping`, JSON.stringify({...message, at: Date.now()}));
    } catch (error) {}
  }

  function startHeartbeat() {
    if (!isCallWindow || heartbeatTimer) return;
    heartbeatTimer = window.setInterval(() => {
      if (callState === "idle") return;
      setActiveSession({sessionId, state: callState, updatedAt: Date.now()});
      broadcast("heartbeat");
    }, 2000);
  }

  function stopHeartbeat() {
    if (!heartbeatTimer) return;
    window.clearInterval(heartbeatTimer);
    heartbeatTimer = null;
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
      sessionId,
      windowRole: isCallWindow ? "call-window" : "site-page",
      ...details,
    };
    console.debug("[TopAI Realtime]", JSON.stringify(entry));
  }

  function redactedDiagnosticValue(value) {
    if (value === undefined || value === null) return null;
    if (typeof value === "object") {
      return JSON.stringify(value, (key, nestedValue) => {
        if (/key|token|secret|authorization|bearer/i.test(key)) return "[redacted]";
        if (typeof nestedValue === "string" && /key|token|secret|authorization|bearer/i.test(nestedValue)) return "[redacted]";
        return nestedValue;
      }).slice(0, 480);
    }
    const text = String(value);
    if (!text) return "";
    if (/key|token|secret|authorization|bearer/i.test(text)) return "[redacted]";
    return text.slice(0, 240);
  }

  function firstDiagnosticString(...values) {
    for (const value of values) {
      if (value === undefined || value === null || value === "") continue;
      if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
        return String(value);
      }
      if (typeof value === "object") {
        const nested = firstDiagnosticString(
          value.message,
          value.msg,
          value.reason,
          value.detail,
          value.details,
          value.error,
          value.errors?.[0],
        );
        if (nested) return nested;
        return redactedDiagnosticValue(value);
      }
    }
    return "";
  }

  function errorDiagnostic(error, stage) {
    const source = error?.error || error || {};
    const message = firstDiagnosticString(
      source.message,
      error?.message,
      source.error,
      source.details,
      source.detail,
      source.errors?.[0],
      error,
    ) || "Unknown voice startup error";
    return {
      at: new Date().toISOString(),
      stage,
      name: redactedDiagnosticValue(source.name || error?.name),
      message: redactedDiagnosticValue(message),
      code: redactedDiagnosticValue(source.code || error?.code),
      status: redactedDiagnosticValue(source.status || error?.status),
      type: redactedDiagnosticValue(source.type || error?.type),
      raw: redactedDiagnosticValue(error),
      configured: Boolean(config.configured),
      publicKeyPresent: Boolean(config.publicKey),
      assistantIdPresent: Boolean(config.assistantId),
      assistantConfigPresent: Boolean(config.assistantConfig),
      assistantIsSpeaking,
      uiState: callState,
      sessionId,
    };
  }

  function browserAssistantConfig() {
    const assistantConfig = config.assistantConfig;
    if (!assistantConfig || typeof assistantConfig !== "object") return null;
    return assistantConfig;
  }

  function resolveVapiConstructor(module) {
    const candidates = [
      module?.default,
      module?.default?.default,
      module?.Vapi,
      module?.default?.Vapi,
    ];
    return candidates.find((candidate) => typeof candidate === "function") || null;
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

  function rememberTurn(role, text) {
    const cleaned = String(text || "").trim();
    if (!cleaned || !["assistant", "user"].includes(role)) return;
    const last = turns[turns.length - 1];
    if (last && last.role === role) last.text = cleaned;
    else turns.push({role, text: cleaned});
    if (role === "assistant") lastAssistantText = cleaned;
    saveConversation("active");
    broadcast("transcript");
  }

  function addTranscript(role, text, replacePartial = false, remember = true) {
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
    if (!replacePartial && remember) rememberTurn(role, text);
  }

  function renderTurns(nextTurns, emptyText = "No transcript yet.") {
    transcript.replaceChildren();
    if (!nextTurns.length) {
      const line = document.createElement("p");
      line.className = "topai-live-line";
      line.textContent = emptyText;
      transcript.appendChild(line);
      return;
    }
    nextTurns.forEach((turn) => addTranscript(turn.role, turn.text, false, false));
  }

  function saveConversation(nextStatus) {
    if (!sessionId || !turns.length) return Promise.resolve();
    return fetch("/api/live-voice/conversations", {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        session_id: sessionId,
        status: nextStatus,
        transcript: turns,
      }),
    }).catch((error) => logEvent("conversation_save_failed", {message: error?.message}));
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
    broadcast("state");
    logEvent(source || "response.created", {responseId: currentResponseId});
  }

  function markAssistantPlaybackDone(source, details = {}) {
    logEvent(source, details);
    assistantIsSpeaking = false;
    assistantPlaybackStartedAt = 0;
    potentialInterruptionStartedAt = 0;
    currentResponseId = null;
    if (callState !== "idle") setState("listening", "Listening");
    broadcast("state");
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
    if (!vapi || callState === "idle") return;
    const prompt = [
      "The previous assistant response appears to have been cancelled by echo or transient noise, not an intentional user interruption.",
      "Continue the incomplete TopAI response naturally from the conversation context.",
      lastAssistantText ? `Most recent assistant text before cancellation: \"${lastAssistantText}\"` : "",
      "Do not apologize for a technical issue unless the user asks what happened.",
    ].filter(Boolean).join(" ");
    logEvent("assistant_continuation_requested", {reason});
    if (typeof vapi.send === "function") {
      vapi.send({type: "add-message", message: {role: "system", content: prompt}});
    } else if (typeof vapi.addMessage === "function") {
      vapi.addMessage({role: "system", content: prompt});
    } else {
      logEvent("assistant_continuation_unavailable");
    }
  }

  async function loadVapi() {
    if (vapi) return vapi;
    if (!isCallWindow || !voiceConfigured) return null;
    logEvent("vapi_sdk_loading");
    const module = await Promise.race([
      import("https://cdn.jsdelivr.net/npm/@vapi-ai/web@2.6.1/+esm"),
      new Promise((_, reject) => window.setTimeout(
        () => reject(new Error("Ask TopAI voice library took too long to load.")),
        SDK_LOAD_TIMEOUT_MS
      )),
    ]);
    const Vapi = resolveVapiConstructor(module);
    if (!Vapi) {
      throw new Error(`Ask TopAI voice library loaded with an unsupported export shape: ${Object.keys(module || {}).join(", ") || "empty"}.`);
    }
    vapi = new Vapi(config.publicKey);
    attachVapiHandlers();
    logEvent("vapi_sdk_loaded");
    return vapi;
  }

  function handleUserSpeechStarted(source) {
    logEvent(source || "input_audio_buffer.speech_started");
    if (!assistantIsSpeaking) {
      lastInterruptionClassification = "user_turn_started";
      setState("listening", "Listening");
      broadcast("state");
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
    broadcast("state");
  }

  function showError(error, stage = "start") {
    const diagnostic = errorDiagnostic(error, stage);
    window.__topaiLastError = diagnostic;
    console.error("[TopAI Realtime Error]", JSON.stringify(diagnostic));
    logEvent("voice_error", diagnostic);
    const raw = diagnostic.message || "The live conversation could not start.";
    panel.hidden = false;
    transcript.replaceChildren();
    const line = document.createElement("p");
    line.className = "topai-live-line topai-live-error";
    line.textContent = /microphone|permission|notallowed|device|media/i.test(raw)
      ? "Allow microphone access in Chrome for this site, then click Ask TopAI again."
      : /auth|api key|apikey|forbidden|unauthorized|401|403/i.test(raw)
        ? "TopAI voice was rejected by the voice service configuration. Please try again after the update finishes."
        : `TopAI could not connect. ${raw.slice(0, 160)}`;
    transcript.appendChild(line);
    setState("idle", "Disconnected");
    stopHeartbeat();
    setActiveSession(null);
    broadcast("ended");
  }

  function showUnavailable() {
    panel.hidden = false;
    transcript.replaceChildren();
    const line = document.createElement("p");
    line.className = "topai-live-line topai-live-error";
    line.textContent = config.configured === false
      ? "Ask TopAI is visible, but live voice is not fully configured yet."
      : "Ask TopAI could not load its voice configuration. Please refresh and try again.";
    transcript.appendChild(line);
    setState("idle", "Unavailable");
  }

  function openLiveWindow() {
    ensureSessionId();
    if (openWindowTimer) window.clearTimeout(openWindowTimer);
    const url = `/ask-topai-live?session_id=${encodeURIComponent(sessionId)}`;
    callWindowRef = window.open(url, WINDOW_NAME, "popup,width=430,height=620");
    if (!callWindowRef) {
      panel.hidden = false;
      renderTurns([], "Your browser blocked the live conversation window. Allow popups for this site, then click Ask TopAI again.");
      setState("idle", "Popup blocked");
      return;
    }
    panel.hidden = false;
    renderTurns(turns, "Opening the Ask TopAI live conversation window...");
    setState("connecting", "Opening live conversation");
    openWindowTimer = window.setTimeout(() => {
      if (activeSession()?.sessionId === sessionId) return;
      setState("idle", "Ready");
      renderTurns([], "The live conversation window did not finish opening. Click Ask TopAI again and allow popups if Chrome asks.");
      logEvent("live_window_open_timeout");
    }, OPEN_WINDOW_TIMEOUT_MS);
  }

  async function startCall() {
    if (!isCallWindow) {
      openLiveWindow();
      return;
    }
    panel.hidden = false;
    renderTurns([], "Preparing live conversation...");
    setState("connecting", "Preparing...");
    let activeVapi = null;
    try {
      activeVapi = await loadVapi();
    } catch (error) {
      showError(error);
      return;
    }
    if (!activeVapi) {
      logEvent("voice_config_unavailable", {
        configured: config.configured,
        publicKeyPresent: Boolean(config.publicKey),
        assistantIdPresent: Boolean(config.assistantId),
        assistantConfigPresent: Boolean(config.assistantConfig),
      });
      showUnavailable();
      return;
    }
    renderTurns([], "Starting live conversation...");
    setState("connecting", "Connecting...");
    ensureSessionId();
    setActiveSession({sessionId, state: "connecting", updatedAt: Date.now()});
    startHeartbeat();
    broadcast("state");
    try {
      const assistantConfig = browserAssistantConfig();
      if (!assistantConfig) throw new Error("Ask TopAI live copilot configuration is missing.");
      logEvent("vapi_start_requested", {
        assistantIdPresent: Boolean(config.assistantId),
        assistantConfigKeys: Object.keys(assistantConfig),
        hasVariableValues: Boolean(assistantConfig.variableValues),
      });
      await Promise.race([
        activeVapi.start(assistantConfig),
        new Promise((_, reject) => window.setTimeout(
          () => reject(new Error("Ask TopAI took too long to connect.")),
          START_TIMEOUT_MS
        )),
      ]);
    } catch (error) {
      showError(error, "vapi.start");
    }
  }

  async function endCall() {
    if (!isCallWindow) {
      const active = activeSession();
      if (active?.sessionId) {
        sessionId = active.sessionId;
        broadcast("end-requested");
        const win = window.open("", WINDOW_NAME);
        if (win) win.focus();
        return;
      }
      setState("idle", "Conversation ended");
      return;
    }
    if (callState === "idle") return;
    logEvent("call_stop_requested");
    if (vapi) vapi.stop();
    await saveConversation("ended");
    stopHeartbeat();
    setActiveSession(null);
    setState("idle", "Conversation ended");
    broadcast("ended");
  }

  async function loadHistory() {
    if (!historyPanel || !historyList) return;
    historyPanel.hidden = !historyPanel.hidden;
    if (historyPanel.hidden) return;
    historyList.replaceChildren();
    const loading = document.createElement("p");
    loading.className = "topai-live-line";
    loading.textContent = "Loading prior chats...";
    historyList.appendChild(loading);
    try {
      const res = await fetch("/api/live-voice/conversations", {credentials: "same-origin"});
      const data = await res.json();
      historyList.replaceChildren();
      const conversations = data.conversations || [];
      if (!conversations.length) {
        const empty = document.createElement("p");
        empty.className = "topai-live-line";
        empty.textContent = "No prior live chats yet.";
        historyList.appendChild(empty);
        return;
      }
      conversations.forEach((item) => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "topai-live-history-item";
        const title = document.createElement("strong");
        title.textContent = item.summary || "Live conversation";
        const meta = document.createElement("span");
        const when = item.ended_at || item.updated_at || item.created_at || "";
        meta.textContent = `${when ? new Date(when).toLocaleString() : "Saved chat"}${item.preview ? ` - ${item.preview}` : ""}`;
        row.append(title, meta);
        row.addEventListener("click", () => loadConversation(item.session_id));
        historyList.appendChild(row);
      });
    } catch (error) {
      historyList.replaceChildren();
      const line = document.createElement("p");
      line.className = "topai-live-line topai-live-error";
      line.textContent = "Could not load prior live chats.";
      historyList.appendChild(line);
    }
  }

  async function loadConversation(priorSessionId) {
    try {
      const res = await fetch(`/api/live-voice/conversations/${encodeURIComponent(priorSessionId)}`, {
        credentials: "same-origin",
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Not found");
      panel.hidden = false;
      renderTurns(data.conversation?.transcript || [], "This saved chat has no transcript.");
      if (historyPanel) historyPanel.hidden = true;
      if (callState === "idle") setState("idle", "Prior live chat");
    } catch (error) {
      logEvent("conversation_load_failed", {message: error?.message});
    }
  }

  function applyRemoteState(message) {
    if (!message || !message.sessionId) return;
    if (openWindowTimer) {
      window.clearTimeout(openWindowTimer);
      openWindowTimer = null;
    }
    sessionId = message.sessionId;
    if (Array.isArray(message.turns)) {
      turns = message.turns;
      renderTurns(turns, message.type === "ended" ? "Conversation ended." : "Live conversation is active.");
    }
    panel.hidden = false;
    if (message.type === "ended") {
      setState("idle", "Conversation ended");
      setActiveSession(null);
      return;
    }
    const label = message.state === "speaking"
      ? "TopAI is speaking"
      : message.type === "window-ready"
        ? "Live window ready"
        : "Live conversation active";
    setState(message.state || "listening", label);
    setActiveSession({sessionId, state: message.state || "listening", updatedAt: message.updatedAt || Date.now()});
  }

  if (channel) {
    channel.addEventListener("message", (event) => {
      const message = event.data || {};
      if (isCallWindow && message.type === "end-requested") {
        endCall();
      } else if (!isCallWindow) {
        applyRemoteState(message);
      }
    });
  }

  window.addEventListener("storage", (event) => {
    if (isCallWindow || event.key !== `${ACTIVE_KEY}-ping` || !event.newValue) return;
    try {
      applyRemoteState(JSON.parse(event.newValue));
    } catch (error) {}
  });

  function attachVapiHandlers() {
    if (!vapi || vapiHandlersAttached) return;
    vapiHandlersAttached = true;
    vapi.on("call-start", () => {
      turns = [];
      renderTurns([], "Listening...");
      logEvent("call-start");
      setState("listening", "Listening");
      setActiveSession({sessionId, state: "listening", updatedAt: Date.now()});
      startHeartbeat();
      broadcast("state");
    });
    vapi.on("call-end", async () => {
      logEvent("call-end");
      assistantIsSpeaking = false;
      assistantPlaybackStartedAt = 0;
      currentResponseId = null;
      potentialInterruptionStartedAt = 0;
      await saveConversation("ended");
      stopHeartbeat();
      setActiveSession(null);
      setState("idle", "Conversation ended");
      broadcast("ended");
    });
    vapi.on("speech-start", () => markAssistantPlaybackStarted(null, "speech-start"));
    vapi.on("speech-end", () => markAssistantPlaybackDone("speech-end"));
    vapi.on("message", (message) => {
      const type = message?.type;
      if (!type) return;
      if (type !== "transcript") logEvent(`message.${type}`, {responseId: responseIdFromMessage(message)});

      if (type === "transcript") {
        const isPartial = message.transcriptType === "partial";
        addTranscript(message.role, message.transcript, isPartial, !isPartial);
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
  }

  button.addEventListener("click", () => {
    const active = activeSession();
    if (isCallWindow && callState === "idle") {
      startCall();
    } else if (callState === "idle" && !active) startCall();
    else if (!isCallWindow) {
      panel.hidden = false;
      if (active?.sessionId) {
        sessionId = active.sessionId;
        const win = window.open("", WINDOW_NAME);
        if (win) win.focus();
      }
    } else {
      endCall();
    }
  });
  endButton.addEventListener("click", endCall);
  historyToggle?.addEventListener("click", loadHistory);

  if (isCallWindow) {
    panel.hidden = false;
    button.disabled = false;
    if (!sessionId) ensureSessionId();
    window.addEventListener("pagehide", () => {
      if (callState !== "idle") {
        saveConversation("ended");
        if (vapi) vapi.stop();
        stopHeartbeat();
      }
      setActiveSession(null);
      broadcast("ended");
    });
    renderTurns([], "Click Ask TopAI to start the live conversation.");
    setState("idle", "Ready");
    setActiveSession({sessionId, state: "ready", updatedAt: Date.now()});
    broadcast("window-ready", {state: "ready"});
  } else {
    const active = activeSession();
    if (active?.sessionId) {
      sessionId = active.sessionId;
      setState(
        active.state || "listening",
        active.state === "ready" ? "Live window ready" : "Live conversation active"
      );
    } else {
      setState("idle", "Ready");
    }
    button.disabled = false;
  }
}
