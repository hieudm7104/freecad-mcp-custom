"""On-demand FreeCAD Python API reference for the connected MCP client.

``execute_code`` lets the model run arbitrary FreeCAD Python, which is where
most of this server's real power is — and where a model with only a fuzzy
memory of the FreeCAD API invents calls that don't exist. (This deployment
learned that first-hand: ``FreeCADGui.activateDocument`` was a confident
guess and simply isn't a thing; the real name is ``setActiveDocument``.)

The existing ``asset_creation_strategy`` prompt covers *workflow* — check
state, prefer the parts library, verify edits — but stops exactly where the
model starts writing Python, and MCP **prompts** are in practice not fetched
by connector-style clients (ChatGPT's connector UI never surfaces them), so
even that guidance may never reach the model. A **tool** is the one channel
every client actually uses, so the reference is served as one.

Served per topic rather than as one blob: the vendored material is ~36 KB,
and a model paying for all of it on every session would be worse off than
one that pulls the 6 KB it needs. ``DEPLOYMENT_RULES`` below is the part
that cannot come from upstream — it is what is true about *this* FreeCAD
(headless under Xvfb, one shared ``/data`` volume, MinIO behind explicit
tools) rather than about FreeCAD in general.

See ``reference/NOTICE.md`` for provenance and what was left out.
"""

from pathlib import Path

_REFERENCE_DIR = Path(__file__).parent / "reference"

# topic -> (filename, one-line summary for the index)
_TOPICS: dict[str, tuple[str, str]] = {
    "fundamentals": (
        "scripting-fundamentals.md",
        "Module hierarchy, document operations, selection API, console, "
        "units/quantities, the property system.",
    ),
    "geometry": (
        "geometry-and-shapes.md",
        "Part module: primitives, curves/edges, wires/faces/solids, boolean "
        "operations, extrude/revolve/loft/sweep, topological exploration, "
        "Sketcher constraints, Mesh operations.",
    ),
    "parametric": (
        "parametric-objects.md",
        "FeaturePython objects: full object + ViewProvider templates, the "
        "complete property-type reference, dependency tracking.",
    ),
    "advanced": (
        "workbenches-and-advanced.md",
        "FEM and Path/CAM scripting, plus common recipes (mirror, linear and "
        "polar arrays, measuring between shapes). Also contains a "
        "custom-workbench walkthrough that does NOT apply here — see rules.",
    ),
}

# What is true about *this* FreeCAD, which no general FreeCAD documentation
# can tell the model. Every line here was established by running it.
DEPLOYMENT_RULES = """\
## Rules for THIS FreeCAD instance (read before writing execute_code)

FreeCAD 1.0.0, running as a GUI process under Xvfb inside a container,
driven entirely by this MCP server. There is **no human at the screen.**

1. **Never open a dialog or anything that waits for input.** No
   `QMessageBox`, `QDialog.exec_()`, `QInputDialog`, file dialogs, or
   `Gui.Control.showDialog()`. Nobody can dismiss them, so they block
   indefinitely and can hang the RPC connection that serves these tools —
   after which no further tool call is answered until the container is
   restarted (which also loses every unsaved document). If you want input,
   ask the user in chat instead.

2. **The only writable location is `/data`.** It is a volume shared with the
   Blender and renderer containers, so it is also how you hand a file to
   them. Files written anywhere else are invisible to the storage tools and
   vanish on the next container rebuild.

3. **`/data` is NOT object storage.** Nothing written there reaches MinIO by
   itself. Call `save_document_to_storage` (a document) or
   `upload_file_to_storage` (an export, a render) explicitly, or the work is
   lost on a rebuild. `list_storage_files` shows what is actually persisted.

4. **A document lives only in RAM until saved.** Restarting or rebuilding
   the container discards it. Save anything worth keeping.

5. **Always `doc.recompute()`** after creating or changing geometry, and
   before reading `.Shape`, measuring, exporting or taking a screenshot.
   This is the single most common omission in generated FreeCAD code.

6. **`FreeCADGui.setActiveDocument(name)`** switches the active document.
   `FreeCADGui.activateDocument` does **not** exist — do not call it.

7. **Units are millimetres.** A 33 mm box is a 33-unit object. Blender's
   units are metres, so anything exported there arrives 1000x oversized
   relative to the rest of that scene. That is expected, not a bug.

8. **Export meshes, not STEP, for Blender**: `obj.Shape.exportStl(
   "/data/part.stl")`. Blender has no STEP importer at all — not a
   packaging gap, it genuinely cannot read B-rep.

9. **Screenshots cost tokens.** Tools that change the model take
   `include_screenshot=False`; pass it for analytical steps and intermediate
   edits, then call `get_view()` once at the end.
"""

_FOOTER = (
    "\n\n---\nReference text vendored from github/awesome-copilot "
    "(freecad-scripts skill, MIT). Call "
    "`get_freecad_api_reference('index')` for the other topics and for the "
    "rules that apply to this deployment specifically."
)


def topics() -> list[str]:
    return ["index", *_TOPICS]


def _index() -> str:
    lines = [
        "# FreeCAD API reference — available topics",
        "",
        "Call `get_freecad_api_reference(topic)` with one of these before "
        "writing non-trivial `execute_code` scripts:",
        "",
    ]
    for name, (_, summary) in _TOPICS.items():
        lines.append(f"- **`{name}`** — {summary}")
    lines += [
        "",
        "There is deliberately no `gui` topic: PySide dialogs and task panels "
        "cannot work here (see rule 1 below).",
        "",
        DEPLOYMENT_RULES,
    ]
    return "\n".join(lines)


def get(topic: str) -> str:
    """Return the reference text for *topic*, or raise ``ValueError``."""
    key = (topic or "index").strip().lower()
    if key == "index":
        return _index()

    entry = _TOPICS.get(key)
    if entry is None:
        raise ValueError(
            f"Unknown topic {topic!r}. Available: {', '.join(topics())}. "
            "Use 'index' for a summary of each and the rules for this "
            "deployment."
        )

    filename, _ = entry
    path = _REFERENCE_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"Reference file missing from the install: {path}. The "
            "reference/ directory ships inside the package; a partial copy "
            "of the source tree will not have it."
        )

    # A short reminder rather than the full rules: the model asked for a
    # specific topic, and rule 1 is the one that can actually break the
    # server if it is forgotten mid-script.
    header = (
        "> This FreeCAD runs headless under Xvfb with no human present: never "
        "open a dialog or anything awaiting input, write files only under "
        "`/data`, and `doc.recompute()` after changing geometry. Full rules: "
        "`get_freecad_api_reference('index')`.\n\n"
    )
    return header + path.read_text(encoding="utf-8") + _FOOTER
