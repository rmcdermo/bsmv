# load_vdb_sparse_to_blender.py
#
# Single clean-loader script for bsmv output.
#
# Every run:
#   1. clears the Blender scene
#   2. imports bsmv GEOM OBJ/MTL with SURF_ID colors
#   3. draws the domain box from scene_manifest.json
#   4. loads thresholded sparse VDBs
#   5. applies manifest origin and spacing
#   6. forces Blender to load VDB grids
#   7. installs a frame handler to swap VDB files by manifest frame
#
# Settings are in bsmv_blender_config.py next to this file.

from __future__ import annotations

import bisect
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import bpy
from bpy.app.handlers import persistent


# -----------------------------------------------------------------------------
# Config loading
# -----------------------------------------------------------------------------

def _resolve_blender_path(path_text: str) -> Path:
    s = str(path_text)
    if s.startswith("//"):
        return Path(bpy.path.abspath(s)).resolve()
    return Path(s).expanduser().resolve()


def _script_dir() -> Path:
    try:
        if "__file__" in globals() and __file__:
            p = _resolve_blender_path(__file__)
            if p.exists():
                return p.parent
    except Exception:
        pass

    try:
        text = bpy.context.space_data.text
        if text is not None and text.filepath:
            p = _resolve_blender_path(text.filepath)
            if p.exists():
                return p.parent
    except Exception:
        pass

    if bpy.data.filepath:
        return Path(bpy.data.filepath).resolve().parent

    return Path.cwd().resolve()


SCRIPT_DIR = _script_dir()


def _load_config() -> Any:
    candidates = [
        SCRIPT_DIR / "bsmv_blender_config.py",
        Path.cwd() / "bsmv_blender_config.py",
    ]

    try:
        text_block = bpy.context.space_data.text
        if text_block is not None and text_block.filepath:
            candidates.append(_resolve_blender_path(text_block.filepath).parent / "bsmv_blender_config.py")
    except Exception:
        pass

    if bpy.data.filepath:
        candidates.append(Path(bpy.data.filepath).resolve().parent / "bsmv_blender_config.py")

    seen = set()
    for cfg_path in candidates:
        try:
            key = str(cfg_path.resolve())
        except Exception:
            key = str(cfg_path)
        if key in seen:
            continue
        seen.add(key)

        if cfg_path.exists():
            print(f"[config] loading {cfg_path}")
            spec = importlib.util.spec_from_file_location("bsmv_blender_config", str(cfg_path))
            if spec is None or spec.loader is None:
                raise RuntimeError(f"Could not load config spec: {cfg_path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules["bsmv_blender_config"] = module
            spec.loader.exec_module(module)
            return module

    searched = "\n  ".join(str(p) for p in candidates)
    raise RuntimeError(f"Could not find bsmv_blender_config.py. Searched:\n  {searched}")


cfg = _load_config()


def C(name: str, default: Any = None) -> Any:
    return getattr(cfg, name, default)


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------

def abs_path(p: str | Path) -> Path:
    s = str(p)
    if s == "~" or s.startswith("~/"):
        return Path(s).expanduser().resolve()
    p = Path(s)
    if p.is_absolute():
        return p.resolve()
    if bpy.data.filepath:
        return (Path(bpy.data.filepath).resolve().parent / p).resolve()
    return p.resolve()


def sanitize_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def mesh_number_from_name(name: str) -> int:
    m = re.search(r"_mesh_(\d+)", name)
    return int(m.group(1)) if m else 10**9


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def remove_old_handlers() -> None:
    for handler_list in [bpy.app.handlers.frame_change_pre, bpy.app.handlers.frame_change_post]:
        for h in list(handler_list):
            if getattr(h, "__name__", "") == "bsmv_sparse_frame_handler":
                handler_list.remove(h)


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()

    # Remove unused datablocks. This keeps repeated runs from accumulating junk.
    for datablocks in [
        bpy.data.meshes,
        bpy.data.volumes,
        bpy.data.materials,
        bpy.data.images,
        bpy.data.curves,
        bpy.data.collections,
    ]:
        for datablock in list(datablocks):
            if datablock.users == 0:
                datablocks.remove(datablock)


def get_or_create_collection(name: str) -> bpy.types.Collection:
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(coll)
    return coll


def move_to_collection(obj: bpy.types.Object, coll: bpy.types.Collection) -> None:
    if obj.name not in coll.objects:
        coll.objects.link(obj)
    try:
        bpy.context.scene.collection.objects.unlink(obj)
    except Exception:
        pass


def set_rendered_view() -> None:
    if not C("SET_RENDERED_VIEW", True):
        return
    for area in bpy.context.screen.areas:
        if area.type == "VIEW_3D":
            try:
                area.spaces.active.shading.type = "RENDERED"
                area.spaces.active.overlay.show_relationship_lines = False
            except Exception:
                pass


def set_render_settings() -> None:
    if C("SET_CYCLES", True):
        try:
            bpy.context.scene.render.engine = "CYCLES"
        except Exception:
            pass

    try:
        bpy.context.scene.render.fps = int(C("SCENE_FPS", 10))
    except Exception:
        pass

    cycles = getattr(bpy.context.scene, "cycles", None)
    if cycles is not None:
        for attr, value in [
            ("volume_step_rate", C("CYCLES_VOLUME_STEP_RATE", 0.15)),
            ("volume_preview_step_rate", C("CYCLES_VOLUME_PREVIEW_STEP_RATE", 0.15)),
            ("preview_volume_step_rate", C("CYCLES_VOLUME_PREVIEW_STEP_RATE", 0.15)),
        ]:
            try:
                setattr(cycles, attr, value)
            except Exception:
                pass


# -----------------------------------------------------------------------------
# GEOM / scene loading
# -----------------------------------------------------------------------------

def import_obj(filepath: Path) -> list[bpy.types.Object]:
    before = set(bpy.data.objects.keys())

    if hasattr(bpy.ops.wm, "obj_import"):
        bpy.ops.wm.obj_import(
            filepath=str(filepath),
            forward_axis="Y",
            up_axis="Z",
            global_scale=1.0,
            clamp_size=0.0,
        )
    elif hasattr(bpy.ops.import_scene, "obj"):
        bpy.ops.import_scene.obj(
            filepath=str(filepath),
            axis_forward="Y",
            axis_up="Z",
            global_scale=1.0,
            clamp_size=0.0,
        )
    else:
        raise RuntimeError("OBJ import operator not available.")

    after = set(bpy.data.objects.keys())
    return [bpy.data.objects[name] for name in sorted(after - before)]


def load_geometry() -> list[bpy.types.Object]:
    geom_dir = abs_path(C("BLENDER_GEOM_DIR"))
    chid = str(C("BLENDER_CHID"))

    if not geom_dir.exists():
        raise FileNotFoundError(f"GEOM directory does not exist: {geom_dir}")

    obj_files = sorted(geom_dir.glob(f"{chid}_geom_*.obj"))
    if not obj_files:
        obj_files = sorted(geom_dir.glob("*_geom_*.obj"))

    if not obj_files:
        print(f"[geom] no OBJ files found in {geom_dir}")
        return []

    coll = get_or_create_collection("bsmv_geometry")
    loaded = []

    for obj_path in obj_files:
        new_objs = import_obj(obj_path)
        for obj in new_objs:
            obj.name = sanitize_name(obj_path.stem)
            obj.data.name = obj.name + "_mesh"
            move_to_collection(obj, coll)
            obj.hide_viewport = False
            obj.hide_render = False
            loaded.append(obj)
        print(f"[geom] imported {obj_path.name}")

    return loaded


def make_wire_box(name: str, bbox: list[float], collection: bpy.types.Collection) -> bpy.types.Object:
    xmin, xmax, ymin, ymax, zmin, zmax = [float(v) for v in bbox]

    verts = [
        (xmin, ymin, zmin), (xmax, ymin, zmin), (xmax, ymax, zmin), (xmin, ymax, zmin),
        (xmin, ymin, zmax), (xmax, ymin, zmax), (xmax, ymax, zmax), (xmin, ymax, zmax),
    ]
    edges = [
        (0,1), (1,2), (2,3), (3,0),
        (4,5), (5,6), (6,7), (7,4),
        (0,4), (1,5), (2,6), (3,7),
    ]

    mesh = bpy.data.meshes.new(name + "_mesh")
    mesh.from_pydata(verts, edges, [])
    mesh.update()

    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.display_type = "WIRE"
    obj.show_in_front = False
    return obj


def load_scene_box() -> list[bpy.types.Object]:
    geom_dir = abs_path(C("BLENDER_GEOM_DIR"))
    scene_path = geom_dir / "scene_manifest.json"
    if not scene_path.exists():
        print(f"[scene] no scene_manifest.json at {scene_path}")
        return []

    scene = read_json(scene_path)
    coll = get_or_create_collection("bsmv_scene")
    loaded = []

    bbox = scene.get("domain_bbox")
    if bbox:
        loaded.append(make_wire_box("bsmv_domain_bbox", bbox, coll))
        print(f"[scene] drew domain box {bbox}")

    return loaded


# -----------------------------------------------------------------------------
# VDB manifest / sparse frame mapping
# -----------------------------------------------------------------------------

def discover_manifests(vdb_dir: Path, chid: str) -> list[Path]:
    patterns = [
        f"{chid}_mesh_*_manifest.json",
        f"{chid}*mesh*manifest.json",
        "*_mesh_*_manifest.json",
    ]
    for pat in patterns:
        paths = sorted(vdb_dir.glob(pat))
        if paths:
            print(f"[vdb] found {len(paths)} manifests with {pat}")
            return paths
    raise FileNotFoundError(f"No VDB manifests found in {vdb_dir}")


def signal_score(manifest: dict[str, Any]) -> float:
    score = 0.0
    for fr in manifest.get("frames", []):
        score = max(score, float(fr.get("temperature_max", 0.0)))
        score = max(score, 1.0e6 * float(fr.get("density_max", 0.0)))
    return score


def select_manifests(paths: list[Path]) -> list[tuple[Path, dict[str, Any]]]:
    items = [(p, read_json(p)) for p in paths]

    if C("VDB_SORT_MODE", "mesh") == "signal":
        items.sort(key=lambda pm: signal_score(pm[1]), reverse=True)
    else:
        items.sort(key=lambda pm: mesh_number_from_name(pm[0].name))

    start = int(C("VDB_START_INDEX", 0))
    stride = max(1, int(C("VDB_MANIFEST_STRIDE", 1)))
    items = items[start::stride]

    max_meshes = C("VDB_MAX_MESHES", None)
    if max_meshes is not None:
        items = items[:int(max_meshes)]

    return items


def frame_map(vdb_dir: Path, manifest: dict[str, Any]) -> tuple[list[int], dict[int, Path]]:
    out = {}
    for fr in manifest.get("frames", []):
        try:
            idx = int(fr["frame_index"])
            p = vdb_dir / str(fr["filename"])
        except Exception:
            continue
        if p.exists():
            out[idx] = p
    keys = sorted(out.keys())
    return keys, out


def choose_frame(keys: list[int], mapping: dict[int, Path], frame: int) -> tuple[int | None, Path | None, bool]:
    if not keys:
        return None, None, False

    if frame in mapping:
        return frame, mapping[frame], True

    policy = C("VDB_FRAME_POLICY", "nearest")
    if policy == "exact_or_hide":
        return None, None, False

    if policy == "hold_previous":
        i = bisect.bisect_right(keys, frame) - 1
        if i < 0:
            return None, None, False
        k = keys[i]
        return k, mapping[k], False

    i = bisect.bisect_left(keys, frame)
    candidates = []
    if i < len(keys):
        candidates.append(keys[i])
    if i > 0:
        candidates.append(keys[i - 1])
    k = min(candidates, key=lambda x: abs(x - frame))
    return k, mapping[k], False


def apply_timeline(items: list[tuple[Path, dict[str, Any]]]) -> None:
    frames = []
    for _, manifest in items:
        for fr in manifest.get("frames", []):
            try:
                frames.append(int(fr["frame_index"]))
            except Exception:
                pass

    if not frames:
        return

    bpy.context.scene.frame_start = min(frames)
    bpy.context.scene.frame_end = max(frames)

    if not (bpy.context.scene.frame_start <= bpy.context.scene.frame_current <= bpy.context.scene.frame_end):
        bpy.context.scene.frame_set(bpy.context.scene.frame_start)


# -----------------------------------------------------------------------------
# VDB material
# -----------------------------------------------------------------------------

def make_vdb_material() -> bpy.types.Material:
    name = "bsmv_vdb_volume_material"
    old = bpy.data.materials.get(name)
    if old is not None:
        bpy.data.materials.remove(old)

    mat = bpy.data.materials.new(name)
    mat.use_nodes = True

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    for n in list(nodes):
        nodes.remove(n)

    out = nodes.new(type="ShaderNodeOutputMaterial")
    out.location = (850, 0)

    info = nodes.new(type="ShaderNodeVolumeInfo")
    info.location = (-650, 0)

    shaders = []

    if C("USE_SMOKE", True):
        vol = nodes.new(type="ShaderNodeVolumePrincipled")
        vol.location = (350, 130)

        dens_mul = nodes.new(type="ShaderNodeMath")
        dens_mul.operation = "MULTIPLY"
        dens_mul.inputs[1].default_value = float(C("SMOKE_DENSITY_SCALE", 250.0))
        dens_mul.location = (-250, 130)

        if "Density" in info.outputs and "Density" in vol.inputs:
            links.new(info.outputs["Density"], dens_mul.inputs[0])
            links.new(dens_mul.outputs["Value"], vol.inputs["Density"])

        if "Color" in vol.inputs:
            vol.inputs["Color"].default_value = C("SMOKE_COLOR", (0.035, 0.035, 0.035, 1.0))
        if "Blackbody Intensity" in vol.inputs:
            vol.inputs["Blackbody Intensity"].default_value = 0.0
        if "Emission Strength" in vol.inputs:
            vol.inputs["Emission Strength"].default_value = 0.0

        shaders.append(vol.outputs["Volume"])

    if C("USE_FLAME", True):
        vol = nodes.new(type="ShaderNodeVolumePrincipled")
        vol.location = (350, -130)

        temp_min = float(C("FLAME_TEMP_MIN", 120.0))
        temp_max = float(C("FLAME_TEMP_MAX", 900.0))
        temp_range = max(temp_max - temp_min, 1.0)

        sub = nodes.new(type="ShaderNodeMath")
        sub.operation = "SUBTRACT"
        sub.inputs[1].default_value = temp_min
        sub.location = (-450, -100)

        div = nodes.new(type="ShaderNodeMath")
        div.operation = "DIVIDE"
        div.inputs[1].default_value = temp_range
        div.location = (-270, -100)

        clamp = nodes.new(type="ShaderNodeClamp")
        clamp.location = (-90, -100)
        clamp.inputs["Min"].default_value = 0.0
        clamp.inputs["Max"].default_value = 1.0

        strength = nodes.new(type="ShaderNodeMath")
        strength.operation = "MULTIPLY"
        strength.inputs[1].default_value = float(C("EMISSION_STRENGTH", 3.0))
        strength.location = (100, -100)

        bb_temp = nodes.new(type="ShaderNodeMath")
        bb_temp.operation = "ADD"
        bb_temp.inputs[1].default_value = 293.15
        bb_temp.location = (-270, -300)

        bb = nodes.new(type="ShaderNodeBlackbody")
        bb.location = (-90, -300)

        if "Temperature" in info.outputs:
            links.new(info.outputs["Temperature"], sub.inputs[0])
            links.new(sub.outputs["Value"], div.inputs[0])
            links.new(div.outputs["Value"], clamp.inputs["Value"])
            links.new(clamp.outputs["Result"], strength.inputs[0])

            links.new(info.outputs["Temperature"], bb_temp.inputs[0])
            links.new(bb_temp.outputs["Value"], bb.inputs["Temperature"])

        if "Density" in vol.inputs:
            vol.inputs["Density"].default_value = 0.02
        if "Emission Color" in vol.inputs:
            links.new(bb.outputs["Color"], vol.inputs["Emission Color"])
        elif "Color" in vol.inputs:
            links.new(bb.outputs["Color"], vol.inputs["Color"])
        if "Emission Strength" in vol.inputs:
            links.new(strength.outputs["Value"], vol.inputs["Emission Strength"])
        if "Blackbody Intensity" in vol.inputs:
            vol.inputs["Blackbody Intensity"].default_value = 0.0

        shaders.append(vol.outputs["Volume"])

    if not shaders:
        return mat

    if len(shaders) == 1:
        links.new(shaders[0], out.inputs["Volume"])
    else:
        add = nodes.new(type="ShaderNodeAddShader")
        add.location = (650, 0)
        links.new(shaders[0], add.inputs[0])
        links.new(shaders[1], add.inputs[1])
        links.new(add.outputs["Shader"], out.inputs["Volume"])

    return mat


# -----------------------------------------------------------------------------
# VDB loading / frame handler
# -----------------------------------------------------------------------------

VDB_STATE: dict[str, Any] = {"entries": {}}


def import_vdb(filepath: Path) -> bpy.types.Object | None:
    before = set(bpy.data.objects.keys())

    bpy.ops.object.volume_import(
        filepath=str(filepath),
        align="WORLD",
        location=(0, 0, 0),
        scale=(1, 1, 1),
    )

    after = set(bpy.data.objects.keys())
    names = sorted(after - before)
    if not names:
        return None

    return bpy.data.objects[names[-1]]


def load_grids(obj: bpy.types.Object, label: str = "") -> None:
    try:
        ok = obj.data.grids.load()
        names = [g.name for g in obj.data.grids]
        if C("PRINT_GRID_LOADS", False):
            print(f"[grids {label}] {obj.name}: ok={ok} {names}")
    except Exception as exc:
        print(f"[grids warning] {obj.name}: {exc}")


def set_vdb_path(obj: bpy.types.Object, path: Path) -> None:
    target = str(path)
    if obj.get("bsmv_current_vdb_path", "") == target:
        return

    obj.data.filepath = target
    obj["bsmv_current_vdb_path"] = target
    load_grids(obj, "swap")

    try:
        obj.data.update_tag()
    except Exception:
        pass
    try:
        obj.update_tag()
    except Exception:
        pass


@persistent
def bsmv_sparse_frame_handler(scene: bpy.types.Scene) -> None:
    frame = int(scene.frame_current)
    visible = 0
    hidden = 0

    for obj_name, entry in list(VDB_STATE["entries"].items()):
        obj = bpy.data.objects.get(obj_name)
        if obj is None:
            continue

        chosen, path, exact = choose_frame(entry["keys"], entry["map"], frame)
        if path is None:
            obj.hide_viewport = True
            obj.hide_render = True
            hidden += 1
            continue

        set_vdb_path(obj, path)
        obj.hide_viewport = False
        obj.hide_render = False
        obj["bsmv_loaded_frame"] = int(chosen)
        obj["bsmv_requested_frame"] = frame
        obj["bsmv_exact_frame"] = bool(exact)
        visible += 1

    if C("PRINT_FRAME_HANDLER_UPDATES", False):
        print(f"[frame {frame}] visible={visible} hidden={hidden}")


def load_vdbs() -> list[bpy.types.Object]:
    vdb_dir = abs_path(C("BLENDER_VDB_DIR"))
    chid = str(C("BLENDER_CHID"))

    if not vdb_dir.exists():
        raise FileNotFoundError(f"VDB directory does not exist: {vdb_dir}")

    paths = discover_manifests(vdb_dir, chid)
    items = select_manifests(paths)
    apply_timeline(items)

    coll = get_or_create_collection("bsmv_vdb_volumes")
    mat = make_vdb_material()

    current = int(bpy.context.scene.frame_current)
    loaded = []
    VDB_STATE["entries"] = {}

    for i, (manifest_path, manifest) in enumerate(items, start=1):
        keys, fmap = frame_map(vdb_dir, manifest)
        if not keys:
            print(f"[vdb skip] no VDB frames for {manifest_path.name}")
            continue

        chosen, path, exact = choose_frame(keys, fmap, current)
        if path is None:
            chosen, path, exact = choose_frame(keys, fmap, current)

        if path is None:
            continue

        obj = import_vdb(path)
        if obj is None:
            continue

        mesh_id = str(manifest.get("mesh_id", manifest_path.stem.replace("_manifest", "")))
        obj.name = f"VDB_{sanitize_name(chid)}_{sanitize_name(mesh_id)}"
        obj.data.name = obj.name + "_volume"
        move_to_collection(obj, coll)

        load_grids(obj, "initial")

        origin = manifest.get("origin", [0, 0, 0])
        spacing = manifest.get("spacing", [1, 1, 1])

        if C("APPLY_MANIFEST_ORIGIN", True):
            obj.location = (float(origin[0]), float(origin[1]), float(origin[2]))

        if C("APPLY_MANIFEST_SPACING", True):
            obj.scale = (float(spacing[0]), float(spacing[1]), float(spacing[2]))
        else:
            obj.scale = (1.0, 1.0, 1.0)

        obj.data.materials.clear()
        obj.data.materials.append(mat)

        obj["bsmv_current_vdb_path"] = str(path)
        obj["bsmv_manifest_path"] = str(manifest_path)
        obj["bsmv_loaded_frame"] = int(chosen)
        obj["bsmv_manifest_origin"] = tuple(float(v) for v in origin)
        obj["bsmv_manifest_spacing"] = tuple(float(v) for v in spacing)

        VDB_STATE["entries"][obj.name] = {"keys": keys, "map": fmap}
        loaded.append(obj)

        if i <= int(C("PRINT_FIRST_N", 10)):
            print(
                f"[vdb] {obj.name} "
                f"loc={tuple(round(v, 4) for v in obj.location)} "
                f"scale={tuple(round(v, 6) for v in obj.scale)} "
                f"dims={tuple(round(v, 4) for v in obj.dimensions)} "
                f"frame={chosen} grids={[g.name for g in obj.data.grids]}"
            )

        every = int(C("PRINT_EVERY", 20))
        if every and i % every == 0:
            print(f"[vdb] loaded {i}/{len(items)}")

    bpy.app.handlers.frame_change_post.append(bsmv_sparse_frame_handler)

    bpy.context.scene.frame_set(int(bpy.context.scene.frame_current))
    bpy.context.view_layer.update()

    print(f"[vdb] loaded {len(loaded)} volume objects")
    return loaded


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def run() -> None:
    remove_old_handlers()

    if C("CLEAR_SCENE", True):
        clear_scene()

    set_render_settings()

    if C("LOAD_GEOMETRY", True):
        load_geometry()

    if C("LOAD_SCENE_BOX", True):
        load_scene_box()

    if C("LOAD_VDB", True):
        load_vdbs()

    set_rendered_view()
    print("[done] clean sparse load complete")


if __name__ == "__main__" or C("BLENDER_AUTORUN", True):
    run()
