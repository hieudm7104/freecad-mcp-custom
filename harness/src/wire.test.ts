import assert from "node:assert/strict";
import { test } from "node:test";
import { Agent } from "@earendil-works/pi-agent-core";
import { createModels } from "@earendil-works/pi-ai";
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai/providers/faux";
import { toAgentTools } from "./mcp.js";
import { type Frame, normalise } from "./wire.js";

/** Run a scripted conversation and collect the frames the browser would get. */
async function frames(responses: ReturnType<typeof fauxAssistantMessage>[]): Promise<Frame[]> {
  const faux = fauxProvider();
  const models = createModels();
  models.setProvider(faux.provider);
  faux.setResponses(responses);
  const tools = toAgentTools("freecad", [{ name: "get_view", inputSchema: { type: "object" } }], async () => ({
    // An image block is what makes agent_end/turn_end expensive to forward.
    content: [{ type: "image", data: "A".repeat(50_000), mimeType: "image/png" }],
  }));
  const agent = new Agent({
    initialState: { systemPrompt: "test", model: faux.getModel(), tools },
    streamFn: models.streamSimple.bind(models),
  });
  const out: Frame[] = [];
  agent.subscribe((e) => {
    const f = normalise(e);
    if (f) out.push(f);
  });
  await agent.prompt("go");
  return out;
}

test("a run normalises to exactly the frames frontend/src/Chat.tsx folds", async () => {
  const out = await frames([
    fauxAssistantMessage([fauxToolCall("freecad_get_view", {})], { stopReason: "toolUse" }),
    fauxAssistantMessage("hi"),
  ]);

  // The contract, both ends: Chat.tsx's apply() switches on these three.
  assert.deepEqual(new Set(out.map((f) => f.t)), new Set(["tool", "text"]));

  const tools = out.flatMap((f) => (f.t === "tool" ? [f] : []));
  assert.deepEqual(
    tools.map((f) => f.phase),
    ["start", "end"],
  );
  // The end frame carries the id its start did, which is how the UI pairs them.
  assert.equal(tools[0]!.id, tools[1]!.id);
  assert.equal(tools[0]!.phase === "start" && tools[0]!.name, "freecad_get_view");
  assert.equal(out.map((f) => (f.t === "text" ? f.delta : "")).join(""), "hi");

  // Nothing large reaches the browser: the 50 KB screenshot is clipped, and
  // turn_end/agent_end (which replay the whole transcript) emit nothing.
  assert.ok(JSON.stringify(out).length < 2_000, `frames too big: ${JSON.stringify(out).length}`);
});

test("a provider failure reaches the browser as an error frame", async () => {
  // prompt() resolves on a provider failure; it surfaces on the final message.
  const out = await frames([fauxAssistantMessage("", { stopReason: "error", errorMessage: "401 bad key" })]);
  assert.deepEqual(out.filter((f) => f.t === "error"), [{ t: "error", message: "401 bad key" }]);
});
