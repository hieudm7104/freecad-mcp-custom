import { useEffect, useState } from "react";
import Chat from "./Chat.js";
import Preview, { type Kind } from "./Preview.js";

export default function App() {
  const [tab, setTab] = useState<Kind>("freecad");
  const [health, setHealth] = useState<{ freecad: boolean; blender: boolean } | null>(null);

  // Chained and slow, not a 5 s setInterval: /api/health fans out to both
  // backends and Blender's status goes through the addon's single command
  // queue, so a free-running timer stacks status commands behind whatever the
  // agent is doing — the backlog that makes Blender look hung. One in flight,
  // none while the tab is hidden, and what this reports (a container being
  // up) changes about as often as a restart; the panel's own frames surface
  // the displayed backend much sooner anyway.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    let mounted = true;
    let busy = false;
    const poll = async () => {
      clearTimeout(timer);
      if (busy || document.hidden) return; // visibilitychange re-arms it
      busy = true;
      try {
        // Timed out, not just guarded: `busy` is cleared and the next tick
        // is armed only in this `finally`, so a fetch that never settles
        // freezes the status line for the life of the page.
        const r = await fetch("/api/health", { signal: AbortSignal.timeout(10_000) });
        setHealth((await r.json()) as { freecad: boolean; blender: boolean });
      } catch {
        setHealth(null);
      } finally {
        busy = false;
        if (mounted) timer = setTimeout(poll, 30_000);
      }
    };
    void poll();
    document.addEventListener("visibilitychange", poll);
    return () => {
      mounted = false;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", poll);
    };
  }, []);

  const up = health?.[tab];
  return (
    <div className="app">
      <Chat />
      <section className="panel">
        <div className="tabs">
          {(["freecad", "blender"] as Kind[]).map((k) => (
            <button
              key={k}
              type="button"
              className={k === tab ? "tab active" : "tab"}
              onClick={() => setTab(k)}
            >
              {k === "freecad" ? "FreeCAD" : "Blender"}
              <span className={health?.[k] ? "dot up" : "dot"} />
            </button>
          ))}
          <span className="status">
            {health === null ? "harness unreachable" : up ? "connected" : `${tab} not connected`}
          </span>
        </div>
        {/* key={tab}: remounting drops any orbit queued for the other backend. */}
        <Preview key={tab} kind={tab} />
      </section>
    </div>
  );
}
