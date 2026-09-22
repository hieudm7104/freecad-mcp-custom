import type { AgentEvent } from "@earendil-works/pi-agent-core";

/**
 * The SSE frames this harness sends the browser.
 *
 * frontend/src/Chat.tsx mirrors this type — change both or neither.
 *
 * pi-agent-core's own AgentEvent is deliberately not the wire format: it left
 * the frontend guessing at a library's internal names (it read `event` for
 * `assistantMessageEvent` and matched `tool_start` for `tool_execution_start`,
 * so nothing rendered at all), and its `turn_end`/`agent_end` carry the whole
 * transcript — base64 screenshots included — on every turn.
 */
export type Frame =
  | { t: "text"; delta: string }
  | { t: "tool"; phase: "start"; id: string; name: string; args: string }
  | { t: "tool"; phase: "end"; id: string; result: string; isError: boolean }
  | { t: "error"; message: string };

/** Tool args and results carry base64 images; the UI only shows a snippet. */
const clip = (v: unknown, n = 300): string => {
  const s = typeof v === "string" ? v : JSON.stringify(v ?? null);
  return s.length > n ? `${s.slice(0, n)}…` : s;
};

/** One agent event -> one frame, or nothing when the browser has no use for it. */
export function normalise(event: AgentEvent): Frame | undefined {
  switch (event.type) {
    case "message_update":
      return event.assistantMessageEvent.type === "text_delta"
        ? { t: "text", delta: event.assistantMessageEvent.delta }
        : undefined;
    case "message_end":
      // A provider failure resolves prompt() instead of rejecting it: the
      // StreamFn contract puts the failure on the final assistant message
      // (pi-agent-core types.d.ts:15) and on state.errorMessage. Read here so
      // it reaches the browser as an error instead of an empty answer.
      return "errorMessage" in event.message && event.message.errorMessage
        ? { t: "error", message: event.message.errorMessage }
        : undefined;
    case "tool_execution_start":
      return {
        t: "tool",
        phase: "start",
        id: event.toolCallId,
        name: event.toolName,
        args: clip(event.args),
      };
    case "tool_execution_end":
      return {
        t: "tool",
        phase: "end",
        id: event.toolCallId,
        result: clip(event.result),
        isError: event.isError,
      };
    default:
      // Run/turn bookkeeping and the transcript replays: nothing new to show.
      return undefined;
  }
}
