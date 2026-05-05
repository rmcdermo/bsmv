# smv_to_blender.py
#
# Blender-side loader for bsmv output.
# This version:
#   - imports bsmv OBJ GEOM with FDS/Blender z-up orientation preserved
#   - reads geometry/scene_manifest.json
#   - draws the global domain box, optional mesh boxes, and rectangular vents
#   - then optionally starts VDB loading

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import bpy
from mathutils import Vector


# -----------------------------------------------------------------------------
# User settings
# -----------------------------------------------------------------------------

BLENDER_AUTORUN = True

BLENDER_CHID = "FM_15cm_Burner_C2H4_16p8_5mm"

# Absolute paths are safest. Relative paths are resolved relative to the saved
# .blend file if there is one, otherwise relative to Blender's current directory.
BLENDER_VDB_DIR = "/Users/rmcdermo/spark_home/rmcdermo/GitHub/firemodels/fds/Validation/FM_Burner/Blender_Test/vdb_sequence_dummy"
BLENDER_GEOM_DIR = "/Users/rmcdermo/spark_home/GitHub/firemodels/fds/Validation/FM_Burner/Blender_Test/geometry_16p8_5mm"

BLENDER_LOAD_GEOM = True
BLENDER_LOAD_SCENE = True
BLENDER_LOAD_VDB = False

BLENDER_CLEAR_SCENE = True

# For very large cases, keep this modest for viewport sanity. None loads all.
BLENDER_MAX_MESHES = None

# Only make the first N VDB volume objects visible initially.
BLENDER_VISIBLE_COUNT = 12

# bsmv OBJ files now carry SURF_ID colors through .mtl files. Keep this False
# to preserve those imported materials. Set True only if you want all GEOM gray.
BLENDER_FORCE_NEUTRAL_GEOM_MATERIAL = False

BLENDER_SKIP_EXISTING_GEOM = True
BLENDER_SKIP_EXISTING_VDB = True

BLENDER_DRAW_DOMAIN_BOX = True
BLENDER_DRAW_MESH_BOXES = False
BLENDER_DRAW_VENTS = True

BLENDER_ADD_BASIC_CAMERA_AND_LIGHT = True
BLENDER_SET_CLIP_DISTANCES = True


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------

def abs_path(p: str | Path) -> Path:
    s = str(p)

    # Only expand the current user's ~/ shorthand. Do not expand strings like
    # ~spark_home, because Python treats that as "home directory of user spark_home".
    if s == "~" or s.startswith("~/"):
        path = Path(s).expanduser()
    else:
        path = Path(s)

    if path.is_absolute():
        return path.resolve()

    if bpy.data.filepath:
        return (Path(bpy.data.filepath).parent / path).resolve()

    return path.resolve()


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()


def get_or_create_collection(name: str) -> bpy.types.Collection:
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(coll)
    return coll


def move_object_to_collection(obj: bpy.types.Object, coll: bpy.types.Collection) -> None:
    if obj.name not in coll.objects:
        coll.objects.link(obj)
    try:
        bpy.context.scene.collection.objects.unlink(obj)
    except Exception:
        pass


def sanitize_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


# -----------------------------------------------------------------------------
# Materials
# -----------------------------------------------------------------------------

def make_material(name: str, rgba=(0.7, 0.7, 0.7, 1.0)) -> bpy.types.Material:
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        mat.diffuse_color = rgba
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf is not None:
            if "Base Color" in bsdf.inputs:
                bsdf.inputs["Base Color"].default_value = rgba
            if "Alpha" in bsdf.inputs:
                bsdf.inputs["Alpha"].default_value = rgba[3]
            if "Roughness" in bsdf.inputs:
                bsdf.inputs["Roughness"].default_value = 0.65
        mat.blend_method = "BLEND"
        mat.use_screen_refraction = False
    return mat


def geom_material() -> bpy.types.Material:
    return make_material("bsmv_geom_neutral", (0.55, 0.55, 0.55, 1.0))


def domain_material() -> bpy.types.Material:
    return make_material("bsmv_domain_wire", (0.2, 0.8, 1.0, 1.0))


def mesh_box_material() -> bpy.types.Material:
    return make_material("bsmv_mesh_wire", (0.8, 0.8, 0.8, 0.35))


def vent_material(rgb, alpha, suffix: str) -> bpy.types.Material:
    r, g, b = rgb
    a = max(0.05, min(1.0, alpha))
    return make_material(f"bsmv_vent_{suffix}", (float(r), float(g), float(b), a))


# -----------------------------------------------------------------------------
# OBJ / GEOM import
# -----------------------------------------------------------------------------

def import_obj(filepath: Path) -> list[bpy.types.Object]:
    """Import OBJ and return all newly-created objects.

    bsmv writes OBJ vertices directly as FDS x,y,z coordinates. FDS is z-up,
    and Blender is also z-up. Therefore the OBJ importer must not do its usual
    Y-up conversion.
    """
    before = set(bpy.data.objects.keys())

    if hasattr(bpy.ops.wm, "obj_import"):
        bpy.ops.wm.obj_import(
            filepath=str(filepath),
            forward_axis="Y",
            up_axis="Z",
            global_scale=1.0,
            clamp_size=0.0,
        )
    elif hasattr(bpy.ops, "import_scene") and hasattr(bpy.ops.import_scene, "obj"):
        bpy.ops.import_scene.obj(
            filepath=str(filepath),
            axis_forward="Y",
            axis_up="Z",
            global_scale=1.0,
            clamp_size=0.0,
        )
    else:
        raise RuntimeError("OBJ import operator not available in this Blender build.")

    after = set(bpy.data.objects.keys())
    return [bpy.data.objects[name] for name in sorted(after - before)]


def import_bsmv_geometry(geom_dir: str | Path, chid: str) -> list[bpy.types.Object]:
    geom_dir = abs_path(geom_dir)
    if not geom_dir.exists():
        print(f"[geom] directory does not exist: {geom_dir}")
        return []

    obj_files = sorted(geom_dir.glob(f"{chid}_geom_*.obj"))
    if not obj_files:
        obj_files = sorted(geom_dir.glob("*_geom_*.obj"))
    if not obj_files:
        obj_files = sorted(geom_dir.glob("*.obj"))

    if not obj_files:
        print(f"[geom] no OBJ files found in {geom_dir}")
        return []

    coll = get_or_create_collection("bsmv_geometry")
    mat = geom_material() if BLENDER_FORCE_NEUTRAL_GEOM_MATERIAL else None
    imported: list[bpy.types.Object] = []

    print(f"[geom] importing {len(obj_files)} OBJ file(s) from {geom_dir}")

    for obj_path in obj_files:
        base_name = sanitize_name(obj_path.stem)
        if BLENDER_SKIP_EXISTING_GEOM and bpy.data.objects.get(base_name) is not None:
            print(f"[geom] skip existing {base_name}")
            imported.append(bpy.data.objects[base_name])
            continue

        new_objs = import_obj(obj_path)
        for n, obj in enumerate(new_objs):
            obj.name = base_name if len(new_objs) == 1 else f"{base_name}_{n + 1:02d}"
            obj.data.name = obj.name + "_mesh"
            move_object_to_collection(obj, coll)
            if mat is not None and hasattr(obj.data, "materials"):
                obj.data.materials.clear()
                obj.data.materials.append(mat)
            obj.hide_viewport = False
            obj.hide_render = False
            imported.append(obj)

        print(f"[geom] imported {obj_path.name}")

    return imported


# -----------------------------------------------------------------------------
# Scene manifest drawing: domain, mesh boxes, vents
# -----------------------------------------------------------------------------

def bbox_vertices_and_edges(bbox):
    xmin, xmax, ymin, ymax, zmin, zmax = [float(x) for x in bbox]
    verts = [
        (xmin, ymin, zmin), (xmax, ymin, zmin),
        (xmax, ymax, zmin), (xmin, ymax, zmin),
        (xmin, ymin, zmax), (xmax, ymin, zmax),
        (xmax, ymax, zmax), (xmin, ymax, zmax),
    ]
    edges = [
        (0, 1), (1, 2), (2, 3), (3, 0),
        (4, 5), (5, 6), (6, 7), (7, 4),
        (0, 4), (1, 5), (2, 6), (3, 7),
    ]
    return verts, edges


def add_wire_box(name: str, bbox, mat: bpy.types.Material, coll: bpy.types.Collection) -> bpy.types.Object:
    verts, edges = bbox_vertices_and_edges(bbox)
    mesh = bpy.data.meshes.new(name + "_mesh")
    mesh.from_pydata(verts, edges, [])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    coll.objects.link(obj)
    obj.data.materials.append(mat)
    obj.display_type = "WIRE"
    obj.show_in_front = True
    return obj


def vent_corners_from_bbox(bbox):
    xmin, xmax, ymin, ymax, zmin, zmax = [float(x) for x in bbox]
    dx = abs(xmax - xmin)
    dy = abs(ymax - ymin)
    dz = abs(zmax - zmin)
    eps = 1.0e-8

    if dz <= max(dx, dy, eps) * 1.0e-6:
        z = 0.5 * (zmin + zmax)
        return [(xmin, ymin, z), (xmax, ymin, z), (xmax, ymax, z), (xmin, ymax, z)]
    if dy <= max(dx, dz, eps) * 1.0e-6:
        y = 0.5 * (ymin + ymax)
        return [(xmin, y, zmin), (xmax, y, zmin), (xmax, y, zmax), (xmin, y, zmax)]
    if dx <= max(dy, dz, eps) * 1.0e-6:
        x = 0.5 * (xmin + xmax)
        return [(x, ymin, zmin), (x, ymax, zmin), (x, ymax, zmax), (x, ymin, zmax)]

    # Fallback: draw the bottom face.
    return [(xmin, ymin, zmin), (xmax, ymin, zmin), (xmax, ymax, zmin), (xmin, ymax, zmin)]


def add_rect_vent(name: str, bbox, mat: bpy.types.Material, coll: bpy.types.Collection) -> bpy.types.Object:
    verts = vent_corners_from_bbox(bbox)
    mesh = bpy.data.meshes.new(name + "_mesh")
    mesh.from_pydata(verts, [], [(0, 1, 2, 3)])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    coll.objects.link(obj)
    obj.data.materials.append(mat)
    obj.show_transparent = True
    return obj


def surface_lookup(scene: dict) -> dict[int, dict]:
    out = {}
    for sf in scene.get("surfaces", []):
        try:
            out[int(sf.get("surface_index", -999))] = sf
        except Exception:
            pass
    return out


def vent_color(vent: dict, surfaces: dict[int, dict]):
    if vent.get("has_rgb"):
        return vent.get("rgb", [0.7, 0.7, 0.7]), vent.get("transparency", 1.0)
    sf = surfaces.get(int(vent.get("surf_index", -999)))
    if sf:
        return sf.get("rgb", [0.7, 0.7, 0.7]), sf.get("transparency", 1.0)
    return [0.7, 0.7, 0.7], 1.0


def load_bsmv_scene(geom_dir: str | Path) -> list[bpy.types.Object]:
    geom_dir = abs_path(geom_dir)
    scene_path = geom_dir / "scene_manifest.json"
    if not scene_path.exists():
        print(f"[scene] no scene_manifest.json found at {scene_path}")
        return []

    scene = read_json(scene_path)
    coll = get_or_create_collection("bsmv_scene")
    objects: list[bpy.types.Object] = []

    if BLENDER_DRAW_DOMAIN_BOX and scene.get("domain_bbox"):
        objects.append(add_wire_box("bsmv_domain_bbox", scene["domain_bbox"], domain_material(), coll))

    if BLENDER_DRAW_MESH_BOXES:
        mat = mesh_box_material()
        for mesh_info in scene.get("meshes", []):
            mid = int(mesh_info.get("mesh_index_1based", 0))
            bbox = mesh_info.get("bbox")
            if bbox:
                objects.append(add_wire_box(f"bsmv_mesh_{mid:04d}_bbox", bbox, mat, coll))

    if BLENDER_DRAW_VENTS:
        surfaces = surface_lookup(scene)
        vent_coll = get_or_create_collection("bsmv_vents")
        for n, vent in enumerate(scene.get("vents", []), start=1):
            if vent.get("circular"):
                # Rectangular vents are enough for the first visible pass.
                continue
            if not vent.get("has_bbox"):
                continue
            rgb, alpha = vent_color(vent, surfaces)
            mat = vent_material(rgb, alpha, f"{n:04d}")
            mid = int(vent.get("mesh_index_1based", 0))
            vid = int(vent.get("vent_index_1based", n))
            objects.append(add_rect_vent(f"bsmv_vent_m{mid:04d}_{vid:04d}", vent["bbox"], mat, vent_coll))

    print(
        f"[scene] loaded scene manifest: "
        f"{len(scene.get('meshes', []))} mesh boxes available, "
        f"{len(scene.get('surfaces', []))} surfaces, "
        f"{len(scene.get('vents', []))} vents"
    )
    return objects


# -----------------------------------------------------------------------------
# VDB loading. This is intentionally conservative; existing VDB material tweaks
# can be layered on top later.
# -----------------------------------------------------------------------------

def find_mesh_manifests(vdb_dir: Path, chid: str) -> list[Path]:
    out = sorted(vdb_dir.glob(f"{chid}_mesh_*_manifest.json"))
    if not out:
        out = sorted(vdb_dir.glob("*_mesh_*_manifest.json"))
    return out


def import_vdb(filepath: Path) -> bpy.types.Object | None:
    before = set(bpy.data.objects.keys())
    if not hasattr(bpy.ops.object, "volume_import"):
        raise RuntimeError("Blender does not have bpy.ops.object.volume_import; cannot import VDB.")
    bpy.ops.object.volume_import(filepath=str(filepath))
    after = set(bpy.data.objects.keys())
    new_names = sorted(after - before)
    if not new_names:
        return None
    return bpy.data.objects[new_names[-1]]


def import_bsmv_vdb_sequence(vdb_dir: str | Path, chid: str) -> list[bpy.types.Object]:
    vdb_dir = abs_path(vdb_dir)
    if not vdb_dir.exists():
        print(f"[vdb] directory does not exist: {vdb_dir}")
        return []

    manifests = find_mesh_manifests(vdb_dir, chid)
    if BLENDER_MAX_MESHES is not None:
        manifests = manifests[: int(BLENDER_MAX_MESHES)]
    if not manifests:
        print(f"[vdb] no mesh manifests found in {vdb_dir}")
        return []

    coll = get_or_create_collection("bsmv_vdb_volumes")
    loaded: list[bpy.types.Object] = []
    print(f"[vdb] loading {len(manifests)} mesh manifest(s) from {vdb_dir}")

    for m_index, manifest_path in enumerate(manifests):
        manifest = read_json(manifest_path)
        frames = manifest.get("frames", [])
        if not frames:
            continue
        first_vdb = vdb_dir / frames[0].get("filename", "")
        if not first_vdb.exists():
            print(f"[vdb] WARNING: missing {first_vdb}")
            continue

        obj_name = sanitize_name(str(manifest.get("mesh_id", manifest_path.stem.replace("_manifest", ""))))
        if BLENDER_SKIP_EXISTING_VDB and bpy.data.objects.get(obj_name):
            loaded.append(bpy.data.objects[obj_name])
            continue

        obj = import_vdb(first_vdb)
        if obj is None:
            continue
        obj.name = obj_name
        obj.data.name = obj_name + "_volume"
        move_object_to_collection(obj, coll)

        origin = manifest.get("origin", [0.0, 0.0, 0.0])
        spacing = manifest.get("spacing", [1.0, 1.0, 1.0])
        obj.location = (float(origin[0]), float(origin[1]), float(origin[2]))
        obj.scale = (float(spacing[0]), float(spacing[1]), float(spacing[2]))

        if BLENDER_VISIBLE_COUNT is not None and m_index >= int(BLENDER_VISIBLE_COUNT):
            obj.hide_viewport = True
            obj.hide_render = True

        loaded.append(obj)
        print(f"[vdb] loaded {obj.name}")

    return loaded


# -----------------------------------------------------------------------------
# View helpers
# -----------------------------------------------------------------------------

def compute_scene_bbox(objects: list[bpy.types.Object]):
    pts = []
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for obj in objects:
        try:
            eval_obj = obj.evaluated_get(depsgraph)
            for corner in eval_obj.bound_box:
                pts.append(eval_obj.matrix_world @ Vector(corner))
        except Exception:
            pass
    if not pts:
        return None
    mn = (min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts))
    mx = (max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts))
    return mn, mx


def add_basic_camera_and_light(objects: list[bpy.types.Object]) -> None:
    if not BLENDER_ADD_BASIC_CAMERA_AND_LIGHT:
        return
    bbox = compute_scene_bbox(objects)
    if bbox is None:
        return

    mn, mx = bbox
    cx = 0.5 * (mn[0] + mx[0])
    cy = 0.5 * (mn[1] + mx[1])
    cz = 0.5 * (mn[2] + mx[2])
    radius = max(mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2], 1.0)

    if bpy.data.objects.get("bsmv_camera") is None:
        bpy.ops.object.camera_add(
            location=(cx - 1.7 * radius, cy - 2.4 * radius, cz + 1.2 * radius),
            rotation=(math.radians(62.0), 0.0, math.radians(-35.0)),
        )
        cam = bpy.context.object
        cam.name = "bsmv_camera"
        bpy.context.scene.camera = cam
    else:
        cam = bpy.data.objects["bsmv_camera"]

    if BLENDER_SET_CLIP_DISTANCES and cam.type == "CAMERA":
        cam.data.clip_start = 0.01
        cam.data.clip_end = max(100000.0, 10.0 * radius)

    if bpy.data.objects.get("bsmv_sun") is None:
        bpy.ops.object.light_add(type="SUN", location=(cx, cy, cz + radius))
        sun = bpy.context.object
        sun.name = "bsmv_sun"
        sun.data.energy = 2.0


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def run() -> None:
    if BLENDER_CLEAR_SCENE:
        clear_scene()

    loaded_objects: list[bpy.types.Object] = []

    if BLENDER_LOAD_GEOM:
        loaded_objects.extend(import_bsmv_geometry(BLENDER_GEOM_DIR, BLENDER_CHID))

    if BLENDER_LOAD_SCENE:
        loaded_objects.extend(load_bsmv_scene(BLENDER_GEOM_DIR))

    # Force Blender to show geometry/scene before starting heavy VDB imports.
    bpy.context.view_layer.update()
    print("[geom/scene] loaded; starting VDB load" if BLENDER_LOAD_VDB else "[geom/scene] loaded; VDB load is off")

    if BLENDER_LOAD_VDB:
        loaded_objects.extend(import_bsmv_vdb_sequence(BLENDER_VDB_DIR, BLENDER_CHID))

    add_basic_camera_and_light(loaded_objects)

    print(
        "[done] loaded "
        f"{len([o for o in loaded_objects if o.type == 'MESH'])} mesh object(s), "
        f"{len([o for o in loaded_objects if o.type == 'VOLUME'])} volume object(s)"
    )


if BLENDER_AUTORUN:
    run()
