import { readFile } from "node:fs/promises";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { join, normalize, sep, extname } from "node:path";
import { fileURLToPath } from "node:url";
import { Agent, type AgentTool } from "@earendil-works/pi-agent-core";
import {
  createModels,
  createProvider,
  envApiKeyAuth,
  type Model,
  type TSchema,
} from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";
import { mcpTools } from "./mcp.js";
import { type Frame, normalise } from "./wire.js";

// `||`, not `??`, throughout: docker-compose passes the optional ones as
// ${VAR:-}, i.e. an empty string rather than an absent variable.
const PORT = Number(process.env.PORT || 8000);
const PUBLIC = fileURLToPath(new URL("../public/", import.meta.url)).replace(/[/\\]$/, "");

// Both MCP servers listen on 8000 *inside* the compose network; 8001/8002 are
// host-side publishes only.
const BACKENDS = {
  freecad: {
    url: process.env.FREECAD_MCP_URL || "http://mcp-freecad:8000",
    key: process.env.FREECAD_MCP_API_KEY ?? "",
  },
  blender: {
    url: process.env.BLENDER_MCP_URL || "http://mcp-blender:8000",
    key: process.env.BLENDER_MCP_API_KEY ?? "",
  },
} as const;
type Backend = keyof typeof BACKENDS;

// The api-reference line is not decoration. Watching a real run: asked for a
// gear, the model went straight to freecad_execute_code and burned four
// attempts on `AttributeError: module 'Part' has no ...` and
// `TypeError: float() argument must be ...` before giving up and leaving a
// plain cylinder named "Test" behind — plus five stray Gear1..Gear5
// documents. freecad_get_freecad_api_reference exists precisely because a
// model guessing at the FreeCAD API is this project's oldest failure mode
// (see CLAUDE.md), and it was never called because nothing asked for it. The
// MCP *prompt* that used to carry this guidance never reaches a client like
// this one, so the system prompt is the only channel left.
const SYSTEM_PROMPT = `You drive a headless FreeCAD (freecad_* tools) and a live Blender instance (blender_* tools) for the user.
FreeCAD works in mm and Blender in m, so a 33 mm part imports into Blender as a 33-unit object.
The user watches both viewports live next to this chat, so prefer making the change in the app over describing it.

Before writing any non-trivial FreeCAD Python, call freecad_get_freecad_api_reference for the relevant topic (index, fundamentals, geometry, parametric, advanced). Guessing at the FreeCAD API wastes whole turns on AttributeError and TypeError; the reference is short and authoritative for the exact version running here.
Work inside one document: freecad_create_document once, then reuse it. FreeCAD.newDocument with a name that already exists silently creates Gear1, Gear2, ... instead of failing, so a retry loop litters the session.
Always recompute() after building geometry, and check the result (volume, bounding box, face count) before telling the user it is done.`;

// --- model ----------------------------------------------------------------
// Any OpenAI-compatible /v1/chat/completions endpoint. pi-ai's built-in
// provider factories each hardcode a vendor's baseUrl and model catalog, so
// the model is declared here instead and the provider wraps just it.
const model: Model<"openai-completions"> = {
  id: process.env.HARNESS_MODEL || "gpt-4.1",
  name: process.env.HARNESS_MODEL || "gpt-4.1",
  api: "openai-completions",
  provider: "openai",
  baseUrl: process.env.OPENAI_BASE_URL || "https://api.openai.com/v1",
  reasoning: false,
  input: ["text", "image"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 200_000,
  maxTokens: 16_384,
};

const models = createModels();
models.setProvider(
  createProvider({
    id: "openai",
    name: "OpenAI-compatible",
    baseUrl: model.baseUrl,
    auth: { apiKey: envApiKeyAuth("API key", ["OPENAI_API_KEY"]) },
    models: [model],
    api: openAICompletionsApi(),
  }),
);

// --- agent ----------------------------------------------------------------
// Connected on first chat, not at boot: a wrong key or an MCP server that is
// still starting throws out of connect(), which would crash-loop the
// container. Clearing the cache on failure makes the next request retry.
let toolsOnce: Promise<AgentTool<TSchema>[]> | undefined;
function getTools(): Promise<AgentTool<TSchema>[]> {
  return (toolsOnce ??= Promise.all([
    mcpTools(`${BACKENDS.freecad.url}/mcp`, BACKENDS.freecad.key, "freecad"),
    mcpTools(`${BACKENDS.blender.url}/mcp`, BACKENDS.blender.key, "blender"),
  ])
    .then((sets) => sets.flat())
    .catch((e: unknown) => {
      toolsOnce = undefined;
      throw e;
    }));
}

// One long-lived Agent = one conversation. Rebuilding it per request from the
// posted {role, content} history would lose the tool calls and results, which
// pi's AssistantMessage records and that shape cannot carry.
let agent: Agent | undefined;
async function getAgent(): Promise<Agent> {
  return (agent ??= new Agent({
    initialState: { systemPrompt: SYSTEM_PROMPT, model, tools: await getTools() },
    // pi-ai hardcodes maxRetries: 0 on the OpenAI client and only retries when
    // the caller asks (openai-completions.js passes options?.maxRetries to its
    // own retryProviderRequest). Passing streamSimple straight through meant a
    // single dropped connection ended the whole turn with
    // "Request timed out." — undici's 10 s connect timeout, surfaced by the
    // openai SDK as APIConnectionTimeoutError. A tool-driving turn is long and
    // expensive to redo; three tries is cheap.
    streamFn: (m, ctx, options) =>
      models.streamSimple(m, ctx, { ...options, maxRetries: 3 }),
  }));
}

async function chat(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const chunks: Buffer[] = [];
  for await (const c of req) chunks.push(c as Buffer);
  const body = JSON.parse(Buffer.concat(chunks).toString() || "{}") as {
    messages?: { role: string; content: string }[];
  };
  const messages = body.messages ?? [];
  const prompt = messages.filter((m) => m.role === "user").at(-1)?.content ?? "";

  let a: Agent;
  try {
    a = await getAgent();
  } catch (e) {
    res.writeHead(503, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: `MCP servers unreachable: ${e}` }));
    return;
  }

  // A one-message post is a fresh chat in the browser.
  if (messages.length <= 1) a.reset();
  if (a.state.isStreaming) {
    a.abort();
    await a.waitForIdle();
  }

  res.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-store",
    connection: "keep-alive",
  });
  const unsubscribe = a.subscribe((event) => {
    const frame = normalise(event);
    if (!frame) return;
    // Synchronous on purpose: subscribers are awaited by the agent loop, so
    // waiting for a drain here would stall the run.
    res.write(`data: ${JSON.stringify(frame)}\n\n`);
  });
  // The response's 'close', not the request's: reading the body above drains
  // and destroys the IncomingMessage, so its 'close' has already fired by here.
  const abort = () => a.abort();
  res.on("close", abort);

  try {
    await a.prompt(prompt);
  } catch (e) {
    res.write(`data: ${JSON.stringify({ t: "error", message: String(e) } satisfies Frame)}\n\n`);
  }
  unsubscribe();
  res.off("close", abort);
  res.end();
}

// --- preview proxy --------------------------------------------------------
// The /preview* routes are exempt from the API-key *header* middleware and
// check `?key=` instead, so the key goes in the query string — and last, so a
// browser-supplied one can never win.
function upstream(
  backend: Backend,
  path: string,
  params: URLSearchParams,
  method: string,
  timeoutMs = 30_000,
): Promise<Response> {
  const target = new URL(path, BACKENDS[backend].url);
  for (const [k, v] of params) target.searchParams.set(k, v);
  target.searchParams.set("key", BACKENDS[backend].key);
  // Node's fetch has no default timeout; the FreeCAD server uses 30 s for the
  // same hop.
  return fetch(target, { method, signal: AbortSignal.timeout(timeoutMs) });
}

async function proxy(
  res: ServerResponse,
  backend: Backend,
  path: string,
  params: URLSearchParams,
  method: string,
): Promise<void> {
  const upstreamRes = await upstream(backend, path, params, method);
  const body = Buffer.from(await upstreamRes.arrayBuffer());
  res.writeHead(upstreamRes.status, {
    "content-type": upstreamRes.headers.get("content-type") ?? "application/octet-stream",
    "cache-control": "no-store",
  });
  res.end(body);
}

async function connected(backend: Backend): Promise<boolean> {
  try {
    const r = await upstream(backend, "/preview/status", new URLSearchParams(), "GET", 10_000);
    // The status route answers 200 with {"connected": false} when the app is
    // down, so the field is the signal, not the HTTP code.
    return ((await r.json()) as { connected?: boolean }).connected === true;
  } catch {
    return false;
  }
}

// --- static frontend ------------------------------------------------------
const MIME: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".webmanifest": "application/manifest+json",
  ".woff2": "font/woff2",
};

async function serveStatic(res: ServerResponse, pathname: string): Promise<void> {
  const file = join(PUBLIC, normalize(pathname === "/" ? "/index.html" : pathname));
  // The path comes from the browser: never serve outside the build output.
  const safe = file.startsWith(PUBLIC + sep) ? file : join(PUBLIC, "index.html");
  let body: Buffer;
  let name = safe;
  try {
    body = await readFile(safe);
  } catch {
    // A missing *file* must 404. Falling back to index.html for, say, a stale
    // /assets/<hash>.js answers a `<script type="module">` with
    // `<!doctype html>`; the browser parses that as JavaScript, throws a
    // SyntaxError, and the app never mounts — a blank page that looks exactly
    // like a crash. That is what a cached index.html produces after a rebuild
    // changes the asset hashes, so it is the normal case, not an edge one.
    // Only an extensionless, route-shaped path gets the page.
    if (extname(pathname)) {
      res.writeHead(404, { "content-type": "text/plain", "cache-control": "no-store" });
      res.end("not found");
      return;
    }
    name = join(PUBLIC, "index.html");
    try {
      body = await readFile(name);
    } catch {
      res.writeHead(404, { "content-type": "text/plain" });
      res.end("frontend not built");
      return;
    }
  }
  // index.html names content-hashed assets, so it must never be served stale;
  // the assets it names can never change under their own name.
  res.writeHead(200, {
    "content-type": MIME[extname(name)] ?? "application/octet-stream",
    "cache-control": name.endsWith("index.html")
      ? "no-cache"
      : "public, max-age=31536000, immutable",
  });
  res.end(body);
}

// --- routing --------------------------------------------------------------
const PREVIEW = /^\/api\/preview\/(freecad|blender)(?:\.png|\/(orbit|zoom|reset))$/;

async function route(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const url = new URL(req.url ?? "/", "http://harness");

  if (url.pathname === "/api/chat" && req.method === "POST") return chat(req, res);

  if (url.pathname === "/api/health") {
    const [freecad, blender] = await Promise.all([connected("freecad"), connected("blender")]);
    res.writeHead(200, { "content-type": "application/json", "cache-control": "no-store" });
    res.end(JSON.stringify({ freecad, blender }));
    return;
  }

  const preview = PREVIEW.exec(url.pathname);
  if (preview) {
    const backend = preview[1] as Backend;
    const action = preview[2];
    // Control routes are POST with everything in the query string; they never
    // read a body.
    return proxy(
      res,
      backend,
      action ? `/preview/${action}` : "/preview.png",
      url.searchParams,
      action ? "POST" : "GET",
    );
  }

  if (url.pathname.startsWith("/api/")) {
    res.writeHead(404, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: "not found" }));
    return;
  }

  return serveStatic(res, url.pathname);
}

createServer((req, res) => {
  route(req, res).catch((e: unknown) => {
    if (!res.headersSent) res.writeHead(500, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: String(e) }));
  });
}).listen(PORT, "0.0.0.0", () => console.log(`harness on :${PORT}`));
