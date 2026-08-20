import express from "express";
import { randomUUID } from "node:crypto";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createServer } from "./server.js";

const app = express();
const port = Number(process.env.PORT || 3000);
app.disable("x-powered-by");
app.use(express.json({ limit: "2mb" }));

app.get("/healthz", (_req, res) => {
  res.json({ ok: true, service: "meeting-audio-notes", version: "0.1.0" });
});

app.all("/mcp", async (req, res) => {
  const server = createServer();
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: undefined,
    onsessioninitialized: () => {},
  });
  res.on("close", () => {
    transport.close().catch(() => {});
    server.close().catch(() => {});
  });
  try {
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
  } catch (error) {
    console.error(JSON.stringify({ event: "mcp_request_error", request_id: randomUUID(), message: error?.message || String(error) }));
    if (!res.headersSent) res.status(500).json({ error: "MCP request failed" });
  }
});

app.listen(port, "0.0.0.0", () => {
  console.log(`Meeting Audio Notes MCP listening on :${port}/mcp`);
});
