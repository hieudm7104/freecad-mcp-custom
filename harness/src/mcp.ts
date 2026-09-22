import type { AgentTool, AgentToolResult } from "@earendil-works/pi-agent-core";
import type { TSchema } from "@earendil-works/pi-ai";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { McpError } from "@modelcontextprotocol/sdk/types.js";
import type { RequestOptions } from "@modelcontextprotocol/sdk/shared/protocol.js";

// The SDK's per-request default is 60 s (DEFAULT_REQUEST_TIMEOUT_MSEC), which
// a Cycles render, a CalculiX solve, or anything queued behind one on
// Blender's single addon command queue blows through; the model then gets
// "Request timed out" and retries on top of a call that is still running. No
// resetTimeoutOnProgress: it only extends the deadline while the server sends
// progress notifications, and neither of these servers ever sends one.
// Typed, so a misspelt option is a compile error rather than a silent default.
const CALL_OPTIONS: RequestOptions = { timeout: 600_000 };

/** What `Client.listTools()` gives back, narrowed to the fields used here. */
export type McpTool = { name: string; description?: string; inputSchema: unknown };
type Block = { type: string; text?: string; data?: string; mimeType?: string };
type CallResult = { content?: unknown; isError?: boolean };
type CallTool = (name: string, args: Record<string, unknown>) => Promise<CallResult>;
type Content = AgentToolResult<null>["content"];

/**
 * MCP tools -> pi-agent-core tools. Split from the connection so the mapping
 * (the only part with logic in it) is testable without a server.
 *
 * The prefix is not cosmetic: mcp_freecad and mcp_blender both register the
 * MinIO storage layer, so `list_storage_files`, `upload_file_to_storage` and
 * `download_file_from_storage` genuinely collide. The remote server has never
 * heard of the prefix, hence the original name in the closure.
 */
export function toAgentTools(prefix: string, tools: McpTool[], call: CallTool): AgentTool<TSchema>[] {
  return tools.map((t) => ({
    name: `${prefix}_${t.name}`,
    label: t.name,
    description: t.description ?? "",
    // A raw MCP JSON Schema, not TypeBox: pi-ai's validator detects the
    // missing TypeBox.Kind symbol and takes its plain-JSON-Schema path.
    parameters: t.inputSchema as TSchema,
    execute: async (_id, params): Promise<AgentToolResult<null>> => {
      const result = await call(t.name, (params ?? {}) as Record<string, unknown>);
      // pi-ai only carries text and image blocks; `resource`/`resource_link`/
      // `audio` would end up in the next provider request unsupported.
      const content = ((result.content ?? []) as Block[]).filter(
        (c) => c.type === "text" || c.type === "image",
      ) as Content;
      // MCP reports failure in-band; pi-agent-core wants a throw, and turns
      // that back into isError for the model. Returning it as content instead
      // reads to the model as a successful call.
      if (result.isError) {
        throw new Error(content.map((c) => ("text" in c ? c.text : "")).join("\n") || `${t.name} failed`);
      }
      return { content, details: null };
    },
  }));
}

export async function mcpTools(url: string, apiKey: string, prefix: string): Promise<AgentTool<TSchema>[]> {
  const connect = async (): Promise<Client> => {
    const client = new Client({ name: "freecad-mcp-harness", version: "0.1.0" });
    await client.connect(
      new StreamableHTTPClientTransport(new URL(url), {
        requestInit: { headers: { "X-API-Key": apiKey } },
      }),
    );
    return client;
  };

  let session = connect();
  const { tools } = await (await session).listTools();
  // Cast: callTool's return type unions in the legacy `{toolResult}` shape,
  // which no server here speaks.
  const call = async (s: Promise<Client>, name: string, args: Record<string, unknown>) =>
    (await (await s).callTool({ name, arguments: args }, undefined, CALL_OPTIONS)) as CallResult;

  return toAgentTools(prefix, tools, async (name, args) => {
    const used = session;
    try {
      return await call(used, name, args);
    } catch (e) {
      // Both servers are stateful streamable-HTTP: the mcp-session-id dies
      // with the container and the SDK has no re-initialize path, so once
      // either is restarted (`docker compose up -d mcp-freecad` is in the
      // runbook) every later call fails until the harness itself restarts.
      // Reconnect and retry once — at the price of re-running a call whose
      // reply was lost, which beats the whole session staying dead. Only for
      // transport failures: an McpError means the server answered (a timeout,
      // a bad argument), and retrying a 10-minute render on that is worse
      // than the error.
      if (e instanceof McpError) throw e;
      if (session === used) session = connect();
      return call(session, name, args);
    }
  });
}
