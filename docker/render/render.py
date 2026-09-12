"""Batch-render a model exported from FreeCAD, run inside `blender -b`.

Usage (from the host):
    docker compose run --rm render --input /data/part.step --output /data/part.png

`/data` is the shared `freecad_data` volume — export a STEP/OBJ/glTF/FBX file
there from FreeCAD (e.g. via execute_code_headless + Shape.exportStep) before
invoking this.
"""

import argparse
import sys
from pathlib import Path

import bpy

SUPPORTED_IMPORTERS = {
    ".step": lambda p: bpy.ops.wm.stl_import if False else bpy.ops.import_scene.step(filepath=p),
    ".stp": lambda p: bpy.ops.import_scene.step(filepath=p),
    ".obj": lambda p: bpy.ops.wm.obj_import(filepath=p),
    ".stl": lambda p: bpy.ops.wm.stl_import(filepath=p),
    ".gltf": lambda p: bpy.ops.import_scene.gltf(filepath=p),
    ".glb": lambda p: bpy.ops.import_scene.gltf(filepath=p),
    ".fbx": lambda p: bpy.ops.import_scene.fbx(filepath=p),
}


def _parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Model file to render (STEP/OBJ/STL/glTF/FBX)")
    parser.add_argument("--output", required=True, help="Output image path")
    parser.add_argument("--samples", type=int, default=128, help="Cycles render samples (default: 128)")
    parser.add_argument("--resolution", type=int, nargs=2, default=(1920, 1080), metavar=("W", "H"))
    return parser.parse_args(argv)


def _clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def _import(path: Path) -> None:
    importer = SUPPORTED_IMPORTERS.get(path.suffix.lower())
    if importer is None:
        raise SystemExit(f"Unsupported input format: {path.suffix} (supported: {', '.join(SUPPORTED_IMPORTERS)})")
    importer(str(path))


def _frame_camera_and_light() -> None:
    bpy.ops.object.select_all(action="SELECT")
    imported = list(bpy.context.selected_objects)
    if not imported:
        raise SystemExit("Import produced no objects to render")

    bpy.ops.object.camera_add(location=(6, -6, 4), rotation=(1.0, 0.0, 0.8))
    bpy.context.scene.camera = bpy.context.object

    bpy.ops.object.light_add(type="SUN", location=(4, -4, 8))
    bpy.context.object.data.energy = 3.0

    bpy.ops.object.select_all(action="DESELECT")
    for obj in imported:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = imported[0]
    bpy.ops.view3d.camera_to_view_selected() if bpy.context.area else None


def main() -> None:
    args = _parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.exists():
        raise SystemExit(f"Input not found: {input_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    _clear_scene()
    _import(input_path)
    _frame_camera_and_light()

    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = args.samples
    scene.cycles.device = "CPU"
    scene.render.resolution_x, scene.render.resolution_y = args.resolution
    scene.render.filepath = str(output_path)
    scene.render.image_settings.file_format = output_path.suffix.lstrip(".").upper() or "PNG"

    bpy.ops.render.render(write_still=True)
    print(f"Rendered {input_path} -> {output_path}")


if __name__ == "__main__":
    main()
