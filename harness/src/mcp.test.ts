import assert from "node:assert/strict";
import { test } from "node:test";
import { Agent } from "@earendil-works/pi-agent-core";
import { createModels } from "@earendil-works/pi-ai";
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai/providers/faux";
import { toAgentTools } from "./mcp.js";

test("prefixed tool calls the server by its real name and drops unsupported blocks", async () => {
  const called: { name: string; args: Record<string, unknown> }[] = [];
  const tools = toAgentTools(
    "freecad",
    [
      {
        name: "list_storage_files",
        description: "list",
        inputSchema: { type: "object", properties: { prefix: { type: "string" } } },
      },
    ],
    async (name, args) => {
      called.push({ name, args });
      return {
        content: [
          { type: "text", text: `ok:${args.prefix}` },
          { type: "resource", uri: "file:///x" },
        ],
      };
    },
  );
  assert.equal(tools[0]?.name, "freecad_list_storage_files");

  // Run it through the real loop, with the MCP JSON Schema as-is, so a
  // regression in either the naming or the schema handling fails here.
  const faux = fauxProvider();
  const models = createModels();
  models.setProvider(faux.provider);
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall("freecad_list_storage_files", { prefix: "p" })], {
      stopReason: "toolUse",
    }),
    fauxAssistantMessage("done"),
  ]);
  const agent = new Agent({
    initialState: { systemPrompt: "test", model: faux.getModel(), tools },
    streamFn: models.streamSimple.bind(models),
  });
  const results: string[] = [];
  agent.subscribe((e) => {
    if (e.type === "tool_execution_end") results.push(JSON.stringify(e.result));
  });
  await agent.prompt("go");

  assert.deepEqual(called, [{ name: "list_storage_files", args: { prefix: "p" } }]);
  assert.match(results[0] ?? "", /ok:p/);
  assert.doesNotMatch(results[0] ?? "", /resource/);
});

test("an in-band MCP error becomes a thrown error", async () => {
  const [tool] = toAgentTools("blender", [{ name: "boom", inputSchema: { type: "object" } }], async () => ({
    content: [{ type: "text", text: "kaboom" }],
    isError: true,
  }));
  await assert.rejects(() => tool!.execute("call-1", {}), /kaboom/);
});
