import { validateFileParam, validateKeyterms, normalizeLanguageCode } from "./validation.js";

const ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text";

function appendOptional(form, key, value) {
  if (value !== undefined && value !== null && value !== "") form.append(key, String(value));
}

export function buildScribeForm(input) {
  validateFileParam(input.file);
  const keyterms = validateKeyterms(input.keyterms || []);
  const languageCode = normalizeLanguageCode(input.language_code);

  const form = new FormData();
  form.append("model_id", "scribe_v2");
  form.append("source_url", input.file.download_url);
  form.append("diarize", "true");
  form.append("timestamps_granularity", "word");
  form.append("tag_audio_events", String(Boolean(input.tag_audio_events)));
  form.append("no_verbatim", String(Boolean(input.no_verbatim)));
  appendOptional(form, "language_code", languageCode);
  appendOptional(form, "num_speakers", input.num_speakers);
  for (const term of keyterms) form.append("keyterms", term);
  return form;
}

function providerMessage(payload, status) {
  const detail = payload?.detail ?? payload?.message ?? payload?.error;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return `ElevenLabs returned HTTP ${status}.`;
}

export async function transcribeWithElevenLabs(input, { fetchImpl = fetch } = {}) {
  const apiKey = process.env.ELEVENLABS_API_KEY;
  if (!apiKey) throw new Error("ELEVENLABS_API_KEY is not configured on the MCP server.");

  const form = buildScribeForm(input);
  const controller = new AbortController();
  const timeoutMs = Number(process.env.ELEVENLABS_TIMEOUT_MS || 3300000);
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetchImpl(ELEVENLABS_STT_URL, {
      method: "POST",
      headers: { "xi-api-key": apiKey },
      body: form,
      signal: controller.signal,
    });

    let payload;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }

    if (!response.ok) {
      const error = new Error(providerMessage(payload, response.status));
      error.status = response.status;
      throw error;
    }
    return payload;
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new Error("ElevenLabs transcription timed out. Increase ELEVENLABS_TIMEOUT_MS for very long recordings.");
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}
