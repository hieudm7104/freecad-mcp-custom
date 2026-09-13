"""Batch-render a model exported from FreeCAD, run inside `blender -b`.

Usage (from the host):
    docker compose run --rm render --input /data/part.step --output /data/part.png

`/data` is the shared `freecad_data` volume — export a STEP/OBJ/glTF/FBX file
there from FreeCAD (e.g. via execute_code_headless + Shape.exportStep) before
invoking this.

Uses Cycles on the GPU (OptiX, falling back to CUDA) by default — see
`--device` to force a specific backend or `CPU`. Requires the host to have
an NVIDIA GPU with nvidia-container-toolkit installed and the `render`
service in docker-compose.yml requesting a GPU device (both already set up
on this deployment's Docker host).
"""

import argparse
import sys
from pathlib import Path

import bpy
import mathutils

SUPPORTED_IMPORTERS = {
    ".step": lambda p: bpy.ops.wm.stl_import if False else bpy.ops.import_scene.step(filepath=p),
    ".stp": lambda p: bpy.ops.import_scene.step(filepath=p),
    ".obj": lambda p: bpy.ops.wm.obj_import(filepath=p),
    ".stl": lambda p: bpy.ops.wm.stl_import(filepath=p),
    ".gltf": lambda p: bpy.ops.import_scene.gltf(filepath=p),
    ".glb": lambda p: bpy.ops.import_scene.gltf(filepath=p),
    ".fbx": lambda p: bpy.ops.import_scene.fbx(filepath=p),
}

# Blender's image_settings.file_format enum doesn't always match a file
# extension's own name (confirmed against bpy.types.ImageFormatSettings'
# actual enum items) — e.g. plain `.suffix.upper()` gives "JPG" (invalid;
# must be "JPEG"), "TIF" (invalid; must be "TIFF"), "EXR" (invalid; must be
# "OPEN_EXR"), all of which raise TypeError from Blender rather than just
# silently doing the wrong thing. Only extensions overridden here differ
# from their upper-cased form; anything else falls through to that default.
_FILE_FORMAT_OVERRIDES = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".tif": "TIFF",
    ".tiff": "TIFF",
    ".exr": "OPEN_EXR",
    ".tga": "TARGA",
}


def _blender_file_format(output_path: Path) -> str:
    suffix = output_path.suffix.lower()
    return _FILE_FORMAT_OVERRIDES.get(suffix, suffix.lstrip(".").upper() or "PNG")


def _parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Model file to render (STEP/OBJ/STL/glTF/FBX)")
    parser.add_argument("--output", required=True, help="Output image path")
    parser.add_argument("--samples", type=int, default=128, help="Cycles render samples (default: 128)")
    parser.add_argument("--resolution", type=int, nargs=2, default=(1920, 1080), metavar=("W", "H"))
    parser.add_argument(
        "--device",
        choices=["AUTO", "OPTIX", "CUDA", "CPU"],
        default="AUTO",
        help="Cycles compute device (default: AUTO — try OptiX, then CUDA, then fall back to CPU)",
    )
    return parser.parse_args(argv)


def _enable_gpu(preferred: str) -> str:
    """Try to enable a Cycles GPU backend. Returns the backend actually used
    ("OPTIX", "CUDA", "HIP", "ONEAPI", or "CPU" if no GPU device was found).

    Not every Blender build ships every backend (e.g. Ubuntu's apt package
    only exposes CUDA/HIP, not OPTIX) — setting ``compute_device_type`` to an
    unsupported value raises ``TypeError``, so each candidate is tried and
    any failure just moves on to the next rather than pre-checking what the
    build claims to support (that enum can itself be unreliable before a
    device has actually been probed).
    """
    prefs = bpy.context.preferences.addons["cycles"].preferences
    wanted = ["OPTIX", "CUDA", "HIP", "ONEAPI"] if preferred == "AUTO" else [preferred]

    for backend in wanted:
        try:
            prefs.compute_device_type = backend
        except TypeError:
            continue
        prefs.get_devices()
        gpu_devices = [d for d in prefs.devices if d.type == backend]
        if not gpu_devices:
            continue
        for d in prefs.devices:
            d.use = d.type == backend
        print(f"Cycles {backend}: enabling {len(gpu_devices)} device(s): "
              f"{', '.join(d.name for d in gpu_devices)}")
        return backend

    print("No GPU device found for any backend — falling back to CPU rendering")
    return "CPU"


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

    # `bpy.ops.view3d.camera_to_view_selected()` (the previous approach here)
    # requires a 3D viewport area, which never exists under `blender -b`
    # (background mode) — bpy.context.area is always None — so it silently
    # no-op'd every single render, leaving the camera at a fixed location
    # regardless of the imported object's actual size/position. For any
    # model not coincidentally sized/placed to match that hardcoded camera,
    # the object fell outside the frame and the render came out solid black.
    # Compute the actual world-space bounding box instead and frame from that.
    corners = [
        obj.matrix_world @ mathutils.Vector(corner)
        for obj in imported
        for corner in obj.bound_box
    ]
    min_corner = mathutils.Vector(min(c[i] for c in corners) for i in range(3))
    max_corner = mathutils.Vector(max(c[i] for c in corners) for i in range(3))
    center = (min_corner + max_corner) / 2
    radius = max((max_corner - min_corner).length / 2, 1e-3)

    # Comfortable margin for the camera's default ~40 degree field of view.
    distance = radius * 3.5
    cam_location = center + mathutils.Vector((1.0, -1.0, 0.6)).normalized() * distance

    bpy.ops.object.camera_add(location=cam_location)
    camera = bpy.context.object
    camera.rotation_euler = (center - cam_location).to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = camera

    bpy.ops.object.light_add(type="SUN", location=center + mathutils.Vector((0, 0, distance)))
    light = bpy.context.object
    light.data.energy = 3.0
    light.rotation_euler = mathutils.Vector((0.3, -0.3, -1.0)).to_track_quat("-Z", "Y").to_euler()


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
    # This Blender build has no OpenImageDenoiser compiled in — leaving
    # denoising on (Cycles' default) makes render.render() raise instead of
    # just skipping it.
    scene.cycles.use_denoising = False
    backend = _enable_gpu(args.device) if args.device != "CPU" else "CPU"
    scene.cycles.device = "GPU" if backend != "CPU" else "CPU"
    scene.render.resolution_x, scene.render.resolution_y = args.resolution
    scene.render.filepath = str(output_path)
    scene.render.image_settings.file_format = _blender_file_format(output_path)

    bpy.ops.render.render(write_still=True)
    print(f"Rendered {input_path} -> {output_path}")


if __name__ == "__main__":
    main()
