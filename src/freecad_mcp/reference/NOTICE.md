# Vendored FreeCAD API reference

The four `*.md` files next to this notice are vendored **verbatim** from the
`freecad-scripts` skill in [github/awesome-copilot][src], MIT licensed.

[src]: https://github.com/github/awesome-copilot/tree/main/skills/freecad-scripts

Kept unmodified so they stay easy to re-sync with upstream. Everything this
deployment needs to *add* or *contradict* lives in `../api_reference.py`
instead — see `DEPLOYMENT_RULES` there — rather than being edited into these
files.

## What was deliberately left out

`references/gui-and-interface.md` is **not** vendored. It documents PySide
dialogs, `QMessageBox`, task panels and `Gui.Control.showDialog()` — all of
which assume a human sitting at a FreeCAD window. Here FreeCAD runs under
Xvfb driven by an MCP client with nobody to dismiss a modal dialog, so that
material is at best useless and at worst a way to wedge the RPC server that
serves these tools. Handing it to the model would invite exactly the code
that must never run here.

`workbenches-and-advanced.md` *is* vendored for its FEM, Path/CAM and
"common recipes" (mirror, array, polar array, measure) sections, but its
custom-workbench walkthrough needs files on disk and a FreeCAD restart, so it
does not apply to a live MCP session either. `api_reference.py` says so in
the index rather than cutting the section out.

## Accuracy spot-check (2026-09-16)

Checked against API this deployment had already verified the hard way:
`setActiveDocument` appears and `activateDocument` (which does not exist on
`FreeCADGui`) appears nowhere — the exact distinction that was originally
guessed wrong here. `recompute` is mentioned 19 times, which is the single
thing generated FreeCAD code most often forgets.
