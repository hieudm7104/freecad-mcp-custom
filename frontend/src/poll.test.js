import { strict as assert } from "node:assert";
import test from "node:test";
import { ACTIVE_WINDOW_MS, gapFor } from "./poll.js";

test("gapFor", () => {
  // Cheap frame, nobody touching it: ~1 fps.
  assert.equal(gapFor(80, 60_000), 900);
  // Cheap frame right after a drag: fast enough to feel direct.
  assert.equal(gapFor(80, 200), 120);
  // The interaction window is exclusive at its edge.
  assert.equal(gapFor(80, ACTIVE_WINDOW_MS), 900);
  // Half the frame cost wins once a frame is expensive, active or not.
  assert.equal(gapFor(1000, 200), 500);
  assert.equal(gapFor(2000, 60_000), 1000);
  // ...and keeps winning all the way up: a 19.9 s software-GL Blender frame
  // (CLAUDE.md's measurement) backs off to ~10 s rather than being capped
  // back to ~1.2 s, which would have polled that backend at a 94% duty cycle.
  assert.equal(gapFor(19_890, 60_000), 9945);
  assert.equal(gapFor(19_890, 200), 9945); // a drag does not speed it up either
});
