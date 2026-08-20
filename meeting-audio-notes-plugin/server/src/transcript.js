export function formatTimestamp(seconds = 0) {
  const safe = Number.isFinite(Number(seconds)) ? Math.max(0, Number(seconds)) : 0;
  const total = Math.floor(safe);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return [h, m, s].map((n) => String(n).padStart(2, "0")).join(":");
}

function normalizePiece(piece) { return String(piece ?? ""); }
function appendPiece(current, piece, type) {
  if (!piece) return current;
  if (!current) return piece.trimStart();
  if (type === "spacing") return current + piece;
  if (/^[,.;:!?%)\]}]/.test(piece)) return current + piece;
  if (/[\s([{\-—/]$/.test(current)) return current + piece;
  return current + " " + piece;
}

export function buildSegments(words = [], { maxSeconds = 35, maxChars = 700 } = {}) {
  const segments = [];
  let current = null;
  const flush = () => {
    if (!current) return;
    current.text = current.text.trim();
    if (current.text) segments.push(current);
    current = null;
  };

  for (const item of words || []) {
    if (!item || item.text == null) continue;
    const type = item.type || "word";
    if (type === "audio_event") continue;
    const start = Number.isFinite(Number(item.start)) ? Number(item.start) : (current?.end ?? 0);
    const end = Number.isFinite(Number(item.end)) ? Number(item.end) : start;
    const speakerId = item.speaker_id || current?.speaker_id || "speaker_unknown";
    const piece = normalizePiece(item.text);
    const speakerChanged = current && speakerId !== current.speaker_id;
    const tooLong = current && ((end - current.start) >= maxSeconds || current.text.length >= maxChars);
    if (speakerChanged || tooLong) flush();
    if (!current) current = { speaker_id: speakerId, start, end, text: "" };
    current.end = Math.max(current.end, end);
    current.text = appendPiece(current.text, piece, type);
  }
  flush();

  const speakerMap = new Map();
  let nextSpeaker = 1;
  for (const segment of segments) {
    if (!speakerMap.has(segment.speaker_id)) speakerMap.set(segment.speaker_id, nextSpeaker++);
    segment.speaker_label = `Speaker ${speakerMap.get(segment.speaker_id)}`;
    segment.timestamp = formatTimestamp(segment.start);
  }
  return segments;
}

export function formatReadableTranscript(segments = []) {
  return segments.map((s) => `[${s.timestamp || formatTimestamp(s.start)}] ${s.speaker_label || s.speaker_id}: ${s.text}`).join("\n\n");
}

export function summarizeTranscriptMetadata(response, segments) {
  const allEnds = segments.map((s) => Number(s.end) || 0);
  const durationSeconds = allEnds.length ? Math.max(...allEnds) : 0;
  const speakers = new Set(segments.map((s) => s.speaker_id));
  return {
    language_code: response?.language_code || null,
    language_probability: Number.isFinite(Number(response?.language_probability)) ? Number(response.language_probability) : null,
    duration_seconds: durationSeconds,
    speaker_count: speakers.size,
  };
}
