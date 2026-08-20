import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { transcribeWithElevenLabs } from "./elevenlabs.js";
import { buildSegments, formatReadableTranscript, summarizeTranscriptMetadata } from "./transcript.js";

const OpenAIFile = z.object({
  download_url: z.string(),
  file_id: z.string(),
  mime_type: z.string().optional(),
  file_name: z.string().optional(),
}).strict();

const SegmentSchema = z.object({
  speaker_id: z.string(),
  speaker_label: z.string(),
  start: z.number(),
  end: z.number(),
  timestamp: z.string(),
  text: z.string(),
});

export function createServer() {
  const server = new McpServer(
    { name: "meeting-audio-notes", version: "0.1.0" },
    {
      instructions:
        "Transcribe user-attached meeting audio/video with transcribe_meeting_audio before creating meeting notes. Never infer speaker identities. Omit keyterms unless they are genuinely needed because provider keyterm prompting adds cost.",
    },
  );

  server.registerTool(
    "transcribe_meeting_audio",
    {
      title: "Transcribe meeting audio",
      description:
        "Transcribe a user-attached meeting audio or video file with ElevenLabs Scribe v2. Use for full meeting transcription before summaries or notes. Returns speaker-separated segments, timestamps, detected language, and a readable complete transcript. Does not identify speaker names.",
      inputSchema: {
        file: OpenAIFile,
        language_code: z.string().regex(/^[A-Za-z]{2,3}$/).optional().describe("Optional language code. Omit to auto-detect."),
        num_speakers: z.number().int().min(1).max(32).optional().describe("Known speaker count, if reliably known."),
        no_verbatim: z.boolean().optional().default(false).describe("Remove filler words/false starts. Keep false for a faithful transcript."),
        tag_audio_events: z.boolean().optional().default(false).describe("Include audio-event tags such as laughter. Usually false for meeting notes."),
        keyterms: z.array(z.string()).max(1000).optional().describe("Optional terminology hints. Adds provider surcharge; omit by default."),
      },
      outputSchema: {
        source_file_id: z.string(),
        file_name: z.string().nullable(),
        provider: z.literal("elevenlabs"),
        model: z.literal("scribe_v2"),
        language_code: z.string().nullable(),
        language_probability: z.number().nullable(),
        duration_seconds: z.number(),
        speaker_count: z.number(),
        transcript: z.string(),
        segments: z.array(SegmentSchema),
        warnings: z.array(z.string()),
      },
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        openWorldHint: true,
      },
      _meta: {
        "openai/fileParams": ["file"],
        "openai/toolInvocation/invoking": "Transcribing meeting…",
        "openai/toolInvocation/invoked": "Meeting transcribed",
      },
    },
    async (input) => {
      try {
        const response = await transcribeWithElevenLabs(input);
        const segments = buildSegments(response?.words || []);
        const meta = summarizeTranscriptMetadata(response, segments);
        const transcript = formatReadableTranscript(segments) || String(response?.text || "").trim();
        const warnings = [];
        if (!segments.length && transcript) warnings.push("Speaker/timestamp segments were not available; plain transcript text was returned.");
        if (!meta.language_code) warnings.push("The transcription provider did not return a detected language code.");

        const result = {
          source_file_id: input.file.file_id,
          file_name: input.file.file_name || null,
          provider: "elevenlabs",
          model: "scribe_v2",
          ...meta,
          transcript,
          segments,
          warnings,
        };
        return {
          structuredContent: result,
          content: [{ type: "text", text: transcript || "Transcription completed, but no transcript text was returned." }],
        };
      } catch (error) {
        const status = Number(error?.status || 0);
        let hint = "";
        if (status === 401) hint = " Check the ElevenLabs API key configured on the server.";
        else if (status === 402) hint = " Check ElevenLabs API billing/credits.";
        else if (status === 429) hint = " ElevenLabs rate-limited the request; retry later or reduce concurrency.";
        const message = `Meeting transcription failed: ${error?.message || "Unknown error"}.${hint}`;
        return { isError: true, content: [{ type: "text", text: message }] };
      }
    },
  );

  return server;
}
