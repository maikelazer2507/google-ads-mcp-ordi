const AUDIO_VIDEO_EXTENSIONS = new Set([".mp3", ".mp4", ".m4a", ".wav", ".webm", ".ogg", ".oga", ".flac", ".aac", ".mov", ".mkv", ".mpeg", ".mpg", ".3gp", ".amr", ".opus"]);

export function isSupportedMediaFile(file) {
  const mime = String(file?.mime_type || "").toLowerCase();
  if (mime.startsWith("audio/") || mime.startsWith("video/")) return true;
  const name = String(file?.file_name || "").toLowerCase();
  const dot = name.lastIndexOf(".");
  return dot >= 0 && AUDIO_VIDEO_EXTENSIONS.has(name.slice(dot));
}

export function validateFileParam(file) {
  if (!file || typeof file !== "object") throw new Error("A ChatGPT audio/video file is required.");
  if (!file.download_url || !file.file_id) throw new Error("The file parameter is missing download_url or file_id.");
  let url;
  try { url = new URL(file.download_url); } catch { throw new Error("The ChatGPT file download URL is invalid."); }
  if (url.protocol !== "https:") throw new Error("Only HTTPS ChatGPT file URLs are accepted.");
  if (!isSupportedMediaFile(file)) throw new Error(`Unsupported file type${file.file_name ? `: ${file.file_name}` : ""}. Upload an audio or video file.`);
  return true;
}

export function validateKeyterms(keyterms = []) {
  if (!Array.isArray(keyterms)) throw new Error("keyterms must be an array.");
  if (keyterms.length > 1000) throw new Error("ElevenLabs accepts at most 1000 keyterms.");
  const forbidden = /[<>{}\[\]\\]/;
  return keyterms.map((value, index) => {
    const term = String(value).trim();
    if (!term) throw new Error(`keyterms[${index}] is empty.`);
    if (term.length >= 50) throw new Error(`keyterms[${index}] must be shorter than 50 characters.`);
    if (term.split(/\s+/).length > 5) throw new Error(`keyterms[${index}] may contain at most 5 words.`);
    if (forbidden.test(term)) throw new Error(`keyterms[${index}] contains a character ElevenLabs does not support.`);
    return term;
  });
}

export function normalizeLanguageCode(value) {
  if (value == null || value === "") return undefined;
  const code = String(value).trim().toLowerCase();
  if (!/^[a-z]{2,3}$/.test(code)) throw new Error("language_code must be a 2- or 3-letter language code, or omitted for auto-detection.");
  return code;
}
