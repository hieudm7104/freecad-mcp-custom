import { useEffect, useRef, useState } from "react";
import { gapFor } from "./poll.js";

export type Kind = "freecad" | "blender";

const SENSITIVITY = 0.4; // degrees per pixel dragged

/**
 * One live viewport, driven entirely through the harness (`/api/preview/*`) —
 * the API keys stay server-side and the browser never talks to mcp_freecad or
 * mcp_blender directly.
 *
 * Mounted with key={kind} so switching tabs remounts it: that is what drops a
 * half-sent orbit meant for the other backend, which the original page had to
 * do by hand.
 */
export default function Preview({ kind }: { kind: Kind }) {
  const base = `/api/preview/${kind}`;
  const [src, setSrc] = useState("");
  const [hint, setHint] = useState("drag to orbit · scroll to zoom");
  const [error, setError] = useState("");
  const [live, setLive] = useState(() => {
    try {
      return localStorage.getItem("preview-live") !== "0";
    } catch {
      return true;
    }
  });

  // Everything the scheduler reads lives in refs: it runs from timers and from
  // the <img>'s own load/error, where a re-render must not reset it.
  const liveRef = useRef(live);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const inFlight = useRef(false);
  const frameStart = useRef(0);
  const lastInteraction = useRef(0);
  const pendingManual = useRef(false);
  const img = useRef<HTMLImageElement>(null);

  function noteInteraction() {
    lastInteraction.current = Date.now();
  }

  function scheduleFrame(delay: number) {
    clearTimeout(timer.current);
    timer.current = setTimeout(() => refreshImage(), delay);
  }

  // `manual` = the user asked for this frame (a drag, a reset, a tab switch),
  // so it happens even while paused. It never bypasses inFlight though — two
  // captures at once is exactly the pile-up this pacing exists to prevent — so
  // a manual request arriving mid-frame is remembered and run right after.
  function refreshImage(manual = false) {
    clearTimeout(timer.current);
    if (inFlight.current) {
      if (manual) pendingManual.current = true;
      return;
    }
    // A hidden tab still pays the full render cost on the server.
    if (document.hidden) {
      if (liveRef.current) scheduleFrame(1000);
      return;
    }
    if (!liveRef.current && !manual) return;
    inFlight.current = true;
    frameStart.current = Date.now();
    setSrc(`${base}.png?t=${Date.now()}`);
  }

  function onFrameSettled(ok: boolean) {
    inFlight.current = false;
    if (!ok) {
      // mcp-blender's /preview.png has no catch-all: with blender-cli down or
      // restarting, get_blender_connection() raises and Starlette answers 500.
      // With FREECAD_MCP_PREVIEW=0/BLENDER_MCP_PREVIEW=0 the route isn't
      // registered at all and every /preview* path 401s. Both land here, so
      // this branch is what keeps a dead panel from looking like one that
      // just hasn't drawn yet.
      setHint("no frame — preview route failed (backend down, or preview disabled)");
      if (liveRef.current) scheduleFrame(3000);
      return;
    }
    const took = Date.now() - frameStart.current;
    const gap = gapFor(took, Date.now() - lastInteraction.current);
    // mcp-freecad, unlike mcp-blender, catches everything and answers 200
    // with a 1x1 transparent PNG when the RPC behind it is down, so a `load`
    // event is not evidence of a frame either.
    const blank = (img.current?.naturalWidth ?? 0) <= 1;
    setHint(
      blank
        ? "no frame — backend not responding"
        : `drag to orbit · scroll to zoom · ${(took / 1000).toFixed(2)}s/frame` +
            (liveRef.current ? ` · ${(1000 / (took + gap)).toFixed(1)} fps` : " · paused"),
    );
    if (pendingManual.current) {
      pendingManual.current = false;
      refreshImage(true);
      return;
    }
    if (liveRef.current) scheduleFrame(gap);
  }

  // Both servers report a refused orbit/zoom/reset as 200 + {"success":
  // false}, so the status code alone proves nothing — and a "success" that
  // left the screenshot byte-identical is exactly how the Blender orbit bug
  // hid. Throw here and every caller's existing .catch shows it.
  async function post(action: string) {
    setError("");
    const res = await fetch(`${base}/${action}`, { method: "POST" });
    const data = (await res.json().catch(() => ({}))) as { success?: boolean; error?: string };
    if (!res.ok || data.success === false) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  function toggleLive() {
    const next = !liveRef.current;
    liveRef.current = next;
    setLive(next);
    try {
      localStorage.setItem("preview-live", next ? "1" : "0");
    } catch {
      /* private window, blocked storage — the toggle just doesn't stick */
    }
    if (next) refreshImage(true);
    else clearTimeout(timer.current);
  }

  // Pointermove fires far faster than a frame, so drags are accumulated and
  // flushed one request at a time instead of queueing up behind each other.
  const dragging = useRef(false);
  const last = useRef({ x: 0, y: 0 });
  const queued = useRef({ az: 0, el: 0 });
  const sendingOrbit = useRef(false);

  async function flushOrbit() {
    if (sendingOrbit.current) return;
    sendingOrbit.current = true;
    while (queued.current.az !== 0 || queued.current.el !== 0) {
      const { az, el } = queued.current;
      queued.current = { az: 0, el: 0 };
      try {
        await post(`orbit?dx=${az}&dy=${el}`);
        refreshImage(true);
      } catch (e) {
        setError(`orbit failed: ${e}`);
        break;
      }
    }
    sendingOrbit.current = false;
  }

  useEffect(() => {
    refreshImage(true); // paused still shows one frame to start from
    const onVisibility = () => {
      if (!document.hidden) refreshImage(true);
    };
    document.addEventListener("visibilitychange", onVisibility);
    // React attaches wheel passively at the root, where preventDefault is
    // ignored, so this one goes straight on the element.
    const el = img.current;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      noteInteraction();
      post(`zoom?factor=${e.deltaY > 0 ? 1.1 : 0.9}`)
        .then(() => refreshImage(true))
        .catch((err) => setError(`zoom failed: ${err}`));
    };
    el?.addEventListener("wheel", onWheel, { passive: false });
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      el?.removeEventListener("wheel", onWheel);
      clearTimeout(timer.current);
    };
  }, []);

  return (
    <div className="preview">
      <div className="preview-bar">
        <button
          type="button"
          onClick={() => {
            noteInteraction();
            post("reset")
              .then(() => refreshImage(true))
              .catch((err) => setError(`reset failed: ${err}`));
          }}
        >
          Reset view
        </button>
        <button type="button" className={live ? "" : "paused"} onClick={toggleLive}>
          {live ? "⏸ Live" : "▶ Paused"}
        </button>
        {error && <span className="err">{error}</span>}
      </div>
      <div className="shot">
        <img
          ref={img}
          src={src || undefined}
          alt={`${kind} 3D view`}
          draggable={false}
          onLoad={() => onFrameSettled(true)}
          onError={() => onFrameSettled(false)}
          onPointerDown={(e) => {
            dragging.current = true;
            last.current = { x: e.clientX, y: e.clientY };
            e.currentTarget.setPointerCapture(e.pointerId);
          }}
          onPointerMove={(e) => {
            if (!dragging.current) return;
            const dx = e.clientX - last.current.x;
            const dy = e.clientY - last.current.y;
            last.current = { x: e.clientX, y: e.clientY };
            noteInteraction();
            queued.current.az += -dx * SENSITIVITY;
            queued.current.el += dy * SENSITIVITY;
            flushOrbit();
          }}
          onPointerUp={() => (dragging.current = false)}
          onPointerCancel={() => (dragging.current = false)}
        />
        <div className="hint">{hint}</div>
      </div>
    </div>
  );
}
