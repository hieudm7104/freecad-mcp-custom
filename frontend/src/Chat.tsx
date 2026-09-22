import { useEffect, useRef, useState } from "react";

type Part =
  | { kind: "text"; text: string }
  | { kind: "tool"; id: string; name: string; args: string; result?: string; failed?: boolean };
type Msg = { role: "user" | "assistant"; parts: Part[] };

/**
 * The SSE frames the harness sends. Mirrors harness/src/wire.ts — change both
 * or neither. It is the harness's own shape, not pi-agent-core's: guessing at
 * that library's internal event names is what made this panel render nothing.
 */
type Frame =
  | { t: "text"; delta: string }
  | { t: "tool"; phase: "start"; id: string; name: string; args: string }
  | { t: "tool"; phase: "end"; id: string; result: string; isError: boolean }
  | { t: "error"; message: string };

/** Fold one frame into the assistant message. */
export function apply(parts: Part[], f: Frame): Part[] {
  switch (f.t) {
    case "text": {
      const last = parts[parts.length - 1];
      if (last?.kind === "text") {
        return [...parts.slice(0, -1), { kind: "text", text: last.text + f.delta }];
      }
      return [...parts, { kind: "text", text: f.delta }];
    }
    case "tool":
      return f.phase === "start"
        ? [...parts, { kind: "tool", id: f.id, name: f.name, args: f.args }]
        : parts.map((p) =>
            p.kind === "tool" && p.id === f.id ? { ...p, result: f.result, failed: f.isError } : p,
          );
    case "error":
      return [...parts, { kind: "text", text: `\n⚠ ${f.message}\n` }];
    default:
      return parts; // a frame from a newer harness
  }
}

const textOf = (m: Msg) =>
  m.parts
    .filter((p): p is Extract<Part, { kind: "text" }> => p.kind === "text")
    .map((p) => p.text)
    .join("");

export default function Chat() {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  // The harness holds the response open for the whole run: without this, a
  // provider that hangs leaves the composer disabled until a page reload.
  // Aborting also drops the socket, which is what stops the run server-side.
  const abort = useRef<AbortController | null>(null);

  useEffect(() => bottom.current?.scrollIntoView({ block: "end" }), [msgs]);

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    const history: Msg[] = [...msgs, { role: "user", parts: [{ kind: "text", text }] }];
    setMsgs([...history, { role: "assistant", parts: [] }]);
    setInput("");
    setBusy(true);

    // Append into the last (assistant) message, which is always the one we
    // just pushed.
    const fold = (f: Frame) =>
      setMsgs((prev) => {
        const next = prev.slice();
        next[next.length - 1] = { role: "assistant", parts: apply(next[next.length - 1].parts, f) };
        return next;
      });

    const ctrl = new AbortController();
    abort.current = ctrl;
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ messages: history.map((m) => ({ role: m.role, content: textOf(m) })) }),
        signal: ctrl.signal,
      });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let i: number;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, i);
          buf = buf.slice(i + 2);
          for (const line of chunk.split("\n")) {
            if (!line.startsWith("data:")) continue;
            const data = line.slice(5).trim();
            if (!data || data === "[DONE]") continue;
            try {
              fold(JSON.parse(data));
            } catch {
              /* a half-written frame or a keep-alive comment: skip it */
            }
          }
        }
      }
    } catch (e) {
      // Stop is not a failure: whatever streamed before it stays on screen.
      if (!ctrl.signal.aborted) fold({ t: "error", message: String(e) });
    } finally {
      abort.current = null;
      setBusy(false);
    }
  }

  return (
    <section className="chat">
      <div className="messages">
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            {m.parts.map((p, j) =>
              p.kind === "text" ? (
                <div key={j} className="text">
                  {p.text}
                </div>
              ) : (
                <div key={j} className={`tool${p.failed ? " failed" : ""}`}>
                  <div className="tool-head">
                    {p.result === undefined ? "⏳" : p.failed ? "✕" : "✓"} {p.name}
                  </div>
                  <code>{p.args}</code>
                  {p.result !== undefined && <code className="tool-result">{p.result}</code>}
                </div>
              ),
            )}
            {m.role === "assistant" && m.parts.length === 0 && <div className="text dim">…</div>}
          </div>
        ))}
        <div ref={bottom} />
      </div>
      <div className="composer">
        <textarea
          value={input}
          placeholder="Ask it to model something…"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <button
          type="button"
          onClick={busy ? () => abort.current?.abort() : send}
          disabled={!busy && !input.trim()}
        >
          {busy ? "Stop" : "Send"}
        </button>
      </div>
    </section>
  );
}
