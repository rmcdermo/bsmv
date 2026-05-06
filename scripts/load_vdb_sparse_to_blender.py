# load_vdb_sparse_to_blender.py
#
# Drop-in Blender loader for bsmv output.
#
# This script is intentionally self-contained. Put it next to:
#   bsmv_blender_config.py
#
# If CLEAN_SCENE=True, every run removes old bsmv-managed objects/collections
# before loading new GEOM, scene/domain box, VDB volumes, sun, and camera.
#
# Managed collections:
#   bsmv_geometry
#   bsmv_scene
#   bsmv_vents
#   bsmv_vdb_volumes
#   FDS_VDB_VOLUMES
#
# Managed object prefixes:
#   bsmv_domain_bbox
#   bsmv_mesh_bbox
#   bsmv_vent
#   bsmv_sun
#   bsmv_area_light
#   bsmv_camera
#   VDB_
#   FDS_VDB_CASE
#   <BLENDER_CHID>_geom_

from __future__ import annotations

import bisect
import importlib.util
import json
import mathutils
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
    unique_candidates = []
    for p in candidates:
        try:
            key = str(p.resolve())
        except Exception:
            key = str(p)
        if key not in seen:
            seen.add(key)
            unique_candidates.append(p)

    for cfg_path in unique_candidates:
        if cfg_path.exists():
            print(f"[config] loading {cfg_path}")
            spec = importlib.util.spec_from_file_location("bsmv_blender_config", str(cfg_path))
            if spec is None or spec.loader is None:
                raise RuntimeError(f"Could not create import spec for config: {cfg_path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules["bsmv_blender_config"] = module
            spec.loader.exec_module(module)
            return module

    searched = "\n  ".join(str(p) for p in unique_candidates)
    raise RuntimeError(f"Could not find bsmv_blender_config.py. Searched:\n  {searched}")


cfg = _load_config()


def C(name: str, default: Any = None) -> Any:
    return getattr(cfg, name, default)


# -----------------------------------------------------------------------------
# Paths and names
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


# -----------------------------------------------------------------------------
# Cleanup
# -----------------------------------------------------------------------------

MANAGED_COLLECTION_NAMES = (
    "bsmv_geometry",
    "bsmv_scene",
    "bsmv_vents",
    "bsmv_vdb_volumes",
    "FDS_VDB_VOLUMES",
)

MANAGED_OBJECT_PREFIXES_BASE = (
    "bsmv_domain_bbox",
    "bsmv_mesh_bbox",
    "bsmv_vent",
    "bsmv_sun",
    "bsmv_area_light",
    "bsmv_camera",
    "VDB_",
    "FDS_VDB_CASE",
)


def remove_old_handlers() -> None:
    for handler_list in (bpy.app.handlers.frame_change_pre, bpy.app.handlers.frame_change_post):
        for h in list(handler_list):
            if getattr(h, "__name__", "") == "bsmv_sparse_frame_handler":
                handler_list.remove(h)


def delete_object(obj: bpy.types.Object) -> None:
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
    except Exception:
        pass


def delete_objects_by_prefix(prefix: str) -> None:
    for obj in list(bpy.data.objects):
        if obj.name.startswith(prefix):
            delete_object(obj)


def delete_tagged_objects() -> None:
    for obj in list(bpy.data.objects):
        try:
            if obj.get("bsmv_loader_managed", False):
                delete_object(obj)
        except Exception:
            pass


def unlink_collection_from_all_parents(coll: bpy.types.Collection) -> None:
    for scene in bpy.data.scenes:
        try:
            scene.collection.children.unlink(coll)
        except Exception:
            pass

    for parent in list(bpy.data.collections):
        if parent == coll:
            continue
        try:
            parent.children.unlink(coll)
        except Exception:
            pass


def remove_collection_recursive(coll: bpy.types.Collection | None) -> None:
    if coll is None:
        return

    for child in list(coll.children):
        remove_collection_recursive(child)

    for obj in list(coll.objects):
        delete_object(obj)

    unlink_collection_from_all_parents(coll)

    try:
        bpy.data.collections.remove(coll)
    except Exception:
        pass


def purge_orphans() -> None:
    for datablocks in (
        bpy.data.meshes,
        bpy.data.volumes,
        bpy.data.curves,
        bpy.data.materials,
        bpy.data.images,
    ):
        for datablock in list(datablocks):
            if datablock.users == 0:
                try:
                    datablocks.remove(datablock)
                except Exception:
                    pass

    try:
        bpy.ops.outliner.orphans_purge(do_recursive=True)
    except Exception:
        pass


def clean_bsmv_scene() -> None:
    remove_old_handlers()

    print("[clean] removing previous bsmv-managed objects and collections")

    # Remove managed collections and their contents first.
    for coll_name in MANAGED_COLLECTION_NAMES:
        coll = bpy.data.collections.get(coll_name)
        if coll is not None:
            remove_collection_recursive(coll)

    # Remove any tagged objects that escaped the collections.
    delete_tagged_objects()

    # Remove managed object name prefixes, including .001/.002 duplicates.
    prefixes = list(MANAGED_OBJECT_PREFIXES_BASE)

    chid = str(C("BLENDER_CHID", ""))
    if chid:
        prefixes.extend([
            f"{chid}_geom_",
            f"{sanitize_name(chid)}_geom_",
            f"VDB_{sanitize_name(chid)}_",
        ])

    for prefix in prefixes:
        delete_objects_by_prefix(prefix)

    purge_orphans()


def full_scene_clear() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()

    for coll in list(bpy.data.collections):
        try:
            bpy.data.collections.remove(coll)
        except Exception:
            pass

    purge_orphans()


def get_or_create_collection(name: str) -> bpy.types.Collection:
    """Create/get a top-level bsmv collection linked under the scene root."""
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)

    root = bpy.context.scene.collection
    if coll.name not in [child.name for child in root.children]:
        root.children.link(coll)

    return coll


def move_to_collection(obj: bpy.types.Object, coll: bpy.types.Collection) -> None:
    """Move object exclusively into coll.

    Blender imports OBJ/VDB objects into the active/default collection first.
    If we only link to the target collection, the object still also appears in
    the default "Collection", which makes the Outliner huge. So unlink it from
    every other user collection after linking it to coll.
    """
    if obj.name not in coll.objects:
        coll.objects.link(obj)

    for user_coll in list(obj.users_collection):
        if user_coll != coll:
            try:
                user_coll.objects.unlink(obj)
            except Exception:
                pass

    obj["bsmv_loader_managed"] = True


# -----------------------------------------------------------------------------
# View/render settings, light, camera
# -----------------------------------------------------------------------------

def set_rendered_view() -> None:
    if not C("SET_RENDERED_VIEW", True):
        return

    for area in bpy.context.screen.areas:
        if area.type == "VIEW_3D":
            try:
                area.spaces.active.shading.type = "RENDERED"
                area.spaces.active.overlay.show_relationship_lines = False
                area.spaces.active.clip_start = float(C("VIEW_CLIP_START", 0.001))
                area.spaces.active.clip_end = float(C("VIEW_CLIP_END", 10000.0))
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
        for attr, value in (
            ("volume_step_rate", C("CYCLES_VOLUME_STEP_RATE", 0.15)),
            ("volume_preview_step_rate", C("CYCLES_VOLUME_PREVIEW_STEP_RATE", 0.15)),
            ("preview_volume_step_rate", C("CYCLES_VOLUME_PREVIEW_STEP_RATE", 0.15)),
        ):
            try:
                setattr(cycles, attr, value)
            except Exception:
                pass


def add_basic_lighting() -> None:
    if not C("ADD_BASIC_LIGHTING", True):
        return

    # These should already be gone after clean_bsmv_scene(), but keep this safe.
    delete_objects_by_prefix("bsmv_sun")
    delete_objects_by_prefix("bsmv_area_light")

    bpy.ops.object.light_add(type="SUN", location=(0.0, 0.0, 3.0))
    sun = bpy.context.object
    sun.name = "bsmv_sun"
    move_to_collection(sun, get_or_create_collection("bsmv_scene"))

    try:
        sun.data.energy = float(C("SUN_ENERGY", 3.0))
        sun.rotation_euler = tuple(C("SUN_ROTATION", (0.8, 0.0, -0.6)))
    except Exception:
        pass

    try:
        bpy.context.scene.world.color = C("WORLD_COLOR", (0.8, 0.8, 0.8))
    except Exception:
        pass


def domain_bbox_from_scene_manifest() -> list[float] | None:
    try:
        geom_dir = abs_path(C("BLENDER_GEOM_DIR"))
        scene_path = geom_dir / "scene_manifest.json"
        if scene_path.exists():
            scene = read_json(scene_path)
            bbox = scene.get("domain_bbox")
            if bbox and len(bbox) == 6:
                return [float(v) for v in bbox]
    except Exception:
        pass
    return None


def bbox_from_loaded_objects() -> list[float] | None:
    coords = []

    for obj in bpy.data.objects:
        if not obj.get("bsmv_loader_managed", False):
            continue
        if obj.type not in {"MESH", "VOLUME"}:
            continue
        try:
            for corner in obj.bound_box:
                coords.append(obj.matrix_world @ mathutils.Vector(corner))
        except Exception:
            pass

    if not coords:
        return None

    xs = [v.x for v in coords]
    ys = [v.y for v in coords]
    zs = [v.z for v in coords]
    return [min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)]


def add_basic_camera() -> None:
    if not C("ADD_BASIC_CAMERA", True):
        return

    delete_objects_by_prefix("bsmv_camera")

    bbox = domain_bbox_from_scene_manifest()
    if bbox is None:
        bbox = bbox_from_loaded_objects()
    if bbox is None:
        bbox = [-0.75, 0.75, -0.75, 0.75, 0.0, 2.0]

    xmin, xmax, ymin, ymax, zmin, zmax = bbox
    cx = 0.5 * (xmin + xmax)
    cy = 0.5 * (ymin + ymax)
    cz = 0.5 * (zmin + zmax)

    sx = max(xmax - xmin, 1.0e-6)
    sy = max(ymax - ymin, 1.0e-6)
    sz = max(zmax - zmin, 1.0e-6)
    span = max(sx, sy, sz)

    dx, dy, dz = C("CAMERA_DIRECTION", (-1.25, -2.40, 1.15))
    distance = float(C("CAMERA_DISTANCE_MULTIPLIER", 1.75)) * span

    direction = mathutils.Vector((float(dx), float(dy), float(dz))).normalized()
    target = mathutils.Vector((cx, cy, cz + float(C("CAMERA_TARGET_Z_OFFSET", 0.0)) * sz))
    location = target + direction * distance

    bpy.ops.object.camera_add(location=location)
    cam = bpy.context.object
    cam.name = "bsmv_camera"
    move_to_collection(cam, get_or_create_collection("bsmv_scene"))

    look_dir = target - cam.location
    cam.rotation_euler = look_dir.to_track_quat("-Z", "Y").to_euler()

    cam.data.lens = float(C("CAMERA_LENS_MM", 35.0))
    cam.data.clip_start = float(C("CAMERA_CLIP_START", 0.001))
    cam.data.clip_end = float(C("CAMERA_CLIP_END", 10000.0))
    cam.data.dof.use_dof = False

    bpy.context.scene.camera = cam

    print(
        "[camera] added bsmv_camera "
        f"loc={tuple(round(v, 4) for v in cam.location)} "
        f"target={tuple(round(v, 4) for v in target)}"
    )


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
        (0, 1), (1, 2), (2, 3), (3, 0),
        (4, 5), (5, 6), (6, 7), (7, 4),
        (0, 4), (1, 5), (2, 6), (3, 7),
    ]

    mesh = bpy.data.meshes.new(name + "_mesh")
    mesh.from_pydata(verts, edges, [])
    mesh.update()

    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.display_type = "WIRE"
    obj.show_in_front = False
    obj["bsmv_loader_managed"] = True

    # Ensure it does not appear in any other collection if Blender auto-links it later.
    for user_coll in list(obj.users_collection):
        if user_coll != collection:
            try:
                user_coll.objects.unlink(obj)
            except Exception:
                pass

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
# VDB manifest/frame mapping
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
        try:
            score = max(score, float(fr.get("temperature_max", 0.0)))
            score = max(score, 1.0e6 * float(fr.get("density_max", 0.0)))
        except Exception:
            pass
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
    out: dict[int, Path] = {}

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

    # nearest
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

        # Principled Volume emission needs some density/extinction for integration.
        if "Density" in vol.inputs:
            vol.inputs["Density"].default_value = float(C("FLAME_BASE_DENSITY", 0.02))
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
# VDB loading/frame handler
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

    manifest_paths = discover_manifests(vdb_dir, chid)
    items = select_manifests(manifest_paths)
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
            # Use nearest only for initial object creation when strict mode would hide it.
            pos = bisect.bisect_left(keys, current)
            candidates = []
            if pos < len(keys):
                candidates.append(keys[pos])
            if pos > 0:
                candidates.append(keys[pos - 1])
            if not candidates:
                continue
            chosen = min(candidates, key=lambda x: abs(x - current))
            path = fmap[chosen]
            exact = False

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

        obj["bsmv_loader_managed"] = True
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
    clean_scene = bool(C("CLEAN_SCENE", C("CLEAR_SCENE", True)))

    if clean_scene:
        clean_bsmv_scene()
    else:
        remove_old_handlers()

    if bool(C("NUKE_SCENE", False)):
        full_scene_clear()

    set_render_settings()
    add_basic_lighting()

    if C("LOAD_GEOMETRY", True):
        load_geometry()

    if C("LOAD_SCENE_BOX", True):
        load_scene_box()

    if C("LOAD_VDB", True):
        load_vdbs()

    add_basic_camera()
    set_rendered_view()
    print("[done] sparse loader clean load complete")


if __name__ == "__main__" or C("BLENDER_AUTORUN", True):
    run()
