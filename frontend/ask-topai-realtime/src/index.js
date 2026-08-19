/**
 * Ask TopAI Live Conversation — OpenAI Agents SDK WebRTC transport.
 * The permanent OPENAI_API_KEY never enters this bundle as a value.
 */
import {
  OpenAIRealtimeWebRTC,
  RealtimeAgent,
  RealtimeSession,
  tool,
} from "@openai/agents/realtime";
import { z } from "zod";

function jsonSchemaToZod(schema) {
  if (!schema || typeof schema !== "object") {
    return z.object({});
  }
  const type = schema.type;
  if (type === "string") {
    const inner = Array.isArray(schema.enum) && schema.enum.length
      ? z.enum(schema.enum)
      : z.string();
    return schema.description ? inner.describe(schema.description) : inner;
  }
  if (type === "integer" || type === "number") {
    const inner = z.number();
    return schema.description ? inner.describe(schema.description) : inner;
  }
  if (type === "boolean") {
    return z.boolean();
  }
  if (type === "array") {
    return z.array(jsonSchemaToZod(schema.items || { type: "string" }));
  }
  const props = schema.properties || {};
  const required = new Set(schema.required || []);
  const shape = {};
  Object.keys(props).forEach((key) => {
    let inner = jsonSchemaToZod(props[key]);
    if (!required.has(key)) inner = inner.optional();
    shape[key] = inner;
  });
  return z.object(shape);
}

function toolsFromSpecs(specs, executeTool) {
  return (specs || [])
    .filter((spec) => spec && spec.name)
    .map((spec) =>
      tool({
        name: spec.name,
        description: spec.description || "",
        parameters: jsonSchemaToZod(spec.parameters || { type: "object", properties: {} }),
        execute: async (input, details) => executeTool(spec.name, input || {}, details || {}),
      }),
    );
}

function transcriptFromContent(content) {
  if (!Array.isArray(content)) return "";
  const parts = [];
  content.forEach((item) => {
    if (!item || typeof item !== "object") return;
    if (typeof item.transcript === "string" && item.transcript.trim()) {
      parts.push(item.transcript.trim());
    } else if (typeof item.text === "string" && item.text.trim()) {
      parts.push(item.text.trim());
    }
  });
  return parts.join(" ").trim();
}

function historyToTurns(history) {
  const turns = [];
  (history || []).forEach((item) => {
    if (!item || item.type !== "message") return;
    const text = transcriptFromContent(item.content);
    if (!text) return;
    const role = item.role === "user" ? "user" : "assistant";
    const last = turns[turns.length - 1];
    if (last && last.role === role) last.text = text;
    else turns.push({ role, text });
  });
  return turns;
}

function waitForIce(pc, timeoutMs) {
  if (!pc || pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const finish = () => {
      try {
        pc.removeEventListener("icegatheringstatechange", onChange);
      } catch (err) {
        /* ignore */
      }
      resolve();
    };
    const onChange = () => {
      if (pc.iceGatheringState === "complete") finish();
    };
    pc.addEventListener("icegatheringstatechange", onChange);
    setTimeout(finish, timeoutMs || 3000);
  });
}

function errorForOpenAIStatus(status, data) {
  const err = new Error(
    status === 401 || status === 403
      ? "OpenAI authentication failed."
      : status === 429
        ? "OpenAI API quota is unavailable."
        : "Could not establish the realtime audio connection.",
  );
  err.status = status;
  err.stage =
    status === 401 || status === 403
      ? "openai_auth"
      : status === 429
        ? "openai_quota"
        : status === 404
          ? "model_access"
          : "realtime_connect";
  err.code = "openai_" + status;
  err.openaiType = data && data.error && data.error.type;
  err.openaiCode = data && data.error && data.error.code;
  return err;
}

class AskTopAIWebRTC extends OpenAIRealtimeWebRTC {
  async connect(options) {
    const origFetch = globalThis.fetch.bind(globalThis);
    globalThis.fetch = async (input, init) => {
      const url = String(input && input.url ? input.url : input);
      if (url.indexOf("/v1/realtime/calls") === -1) {
        return origFetch(input, init);
      }
      await waitForIce(this.connectionState && this.connectionState.peerConnection, 3000);
      const res = await origFetch(input, init);
      const text = await res.text();
      if (!res.ok) {
        let data = {};
        try {
          data = JSON.parse(text);
        } catch (err) {
          data = {};
        }
        throw errorForOpenAIStatus(res.status, data);
      }
      const sdp = String(text || "").replace(/^\uFEFF/, "").trimStart();
      if (sdp.indexOf("v=0") !== 0) {
        const err = new Error("Could not establish the realtime audio connection.");
        err.stage = "realtime_connect";
        err.code = "invalid_sdp";
        err.status = res.status;
        throw err;
      }
      return new Response(sdp, { status: res.status, headers: res.headers });
    };
    try {
      return await super.connect(options);
    } finally {
      globalThis.fetch = origFetch;
    }
  }
}

async function connectAskTopAI(options) {
  const apiKey = options && options.apiKey;
  if (!apiKey || String(apiKey).indexOf("ek_") !== 0) {
    const err = new Error("Could not establish the realtime audio connection.");
    err.stage = "client_secret";
    err.code = "malformed";
    throw err;
  }
  const executeTool = options.executeTool || (async () => JSON.stringify({ ok: false }));
  const agent = new RealtimeAgent({
    name: "Ask TopAI",
    instructions: (options && options.instructions) || "You are Ask TopAI, a realtime voice CRM assistant.",
    tools: toolsFromSpecs(options && options.toolSpecs, executeTool),
  });
  const transportOptions = {};
  if (options && options.mediaStream) transportOptions.mediaStream = options.mediaStream;
  const transport = new AskTopAIWebRTC(transportOptions);
  const session = new RealtimeSession(agent, {
    model: (options && options.model) || "gpt-realtime-2.1",
    transport,
    config: {
      outputModalities: ["audio"],
      audio: {
        input: {
          turnDetection: {
            type: "semantic_vad",
            createResponse: true,
            interruptResponse: true,
          },
        },
        output: { voice: (options && options.voice) || "marin" },
      },
    },
  });
  if (typeof options.onHistory === "function") {
    session.on("history_updated", (history) => {
      options.onHistory(historyToTurns(history), history);
    });
  }
  if (typeof options.onEvent === "function") {
    session.on("agent_start", () => options.onEvent("agent_start"));
    session.on("agent_end", () => options.onEvent("agent_end"));
    session.on("audio_interrupted", () => options.onEvent("audio_interrupted"));
    if (session.transport && typeof session.transport.on === "function") {
      session.transport.on("connection_change", (status) => options.onEvent("connection_change", status));
      session.transport.on("*", (event) => options.onEvent("transport", event));
    }
  }
  if (typeof options.onError === "function") {
    session.on("error", (error) => options.onError(error));
  }
  await session.connect({ apiKey });
  return session;
}

globalThis.AskTopAIRealtime = {
  loaded: true,
  connect: connectAskTopAI,
  historyToTurns,
  sdk: "RealtimeAgent/RealtimeSession",
};
