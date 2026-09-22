// Frame pacing for the live preview panels, ported from the page in
// src/freecad_mcp/preview.py (minus its gap cap, see gapFor). Kept in its own
// module so it can be tested without a DOM (see poll.test.js) — it is the only
// real logic in this app. Plain JS + JSDoc, not .ts, purely so `node --test`
// runs it directly: this Node build has no TypeScript support compiled in, and
// a test runner is not worth a dependency for a handful of assertions. tsc
// still checks it (checkJs).
//
// Why it is not a setInterval: a flat 2 s timer asked for frames faster than
// Blender could render them, and the addon has a single command queue, so the
// backlog blocked everything else — the page's own status polls and any MCP
// client's tool calls — while the box sat at ~1500% CPU rendering screenshots
// for a tab nobody was necessarily looking at. The real backpressure is "one
// request in flight, chained off the <img>'s load/error"; this gap is the
// floor on top of that.
export const IDLE_GAP_MS = 900;
export const ACTIVE_GAP_MS = 120;
export const ACTIVE_WINDOW_MS = 2000;

/**
 * Delay before requesting the next frame.
 *
 * Fast while the user is actually touching the image, ~1 fps while it is
 * merely being watched, and never below half the measured frame cost — an
 * expensive backend backs off on its own and leaves room in the single command
 * queue for everyone else. preview.py's copy caps the gap at 1200 ms, which
 * cancels that floor above ~2.4 s frames — i.e. for the software-GL backend
 * (a 19.9 s frame, per CLAUDE.md) the floor exists for. No cap here.
 *
 * @param {number} took how long the last frame took, ms
 * @param {number} sinceInteraction ms since the last drag/scroll/click
 * @returns {number} ms to wait before asking for the next frame
 */
export function gapFor(took, sinceInteraction) {
  const active = sinceInteraction < ACTIVE_WINDOW_MS;
  return Math.max(active ? ACTIVE_GAP_MS : IDLE_GAP_MS, took * 0.5);
}
