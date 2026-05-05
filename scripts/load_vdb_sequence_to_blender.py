import bpy
import json
import math
import re
from pathlib import Path

# -----------------------------------------------------------------------------
# User controls
# -----------------------------------------------------------------------------
BLENDER_AUTORUN = True
BLENDER_CHID = "FM_15cm_Burner_C2H4_16p8_5mm"
BLENDER_MESH_IDS = None
BLENDER_VDB_DIR = "/Users/rmcdermo/spark_home/rmcdermo/GitHub/firemodels/fds/Validation/FM_Burner/Blender_Test/vdb_sequence_16p8_5mm"

# Loading / ordering
BLENDER_SORT_MODE = "mesh"       # "mesh", "rank", or "signal"
BLENDER_START_INDEX = 0
BLENDER_MAX_MESHES = None
BLENDER_MANIFEST_STRIDE = 1
BLENDER_SKIP_EXISTING = False
BLENDER_SKIP_BAD_MANIFESTS = True
BLENDER_SKIP_MISSING_VDB = True
BLENDER_PRINT_EVERY = 1 

# Optional signal filtering
SIGNAL_REQUIRE_DATA = False
SIGNAL_TEMP_MIN = 400.0
SIGNAL_DENSITY_MIN = 1.0e-10
SIGNAL_TEMP_WEIGHT = 1.0
SIGNAL_DENSITY_WEIGHT = 1.0

# Flame controls
FLAME_TEMP_MIN = 600.0
FLAME_TEMP_MAX = 1200.0
EMISSION_STRENGTH = 1.0

# Smoke controls
USE_SMOKE = True
SMOKE_COLOR = (0.035, 0.035, 0.035, 1.0)
SMOKE_ANISOTROPY = 0.0

# Automatic physical smoke scaling:
#   Density input ~= K_m * d_eff
# where
#   K_m   = smoke_mass_extinction from the manifest
#   d_eff = cbrt(dx*dy*dz) from manifest spacing
USE_AUTO_SMOKE_SCALE = False
SMOKE_SCALE_MULTIPLIER = 1.0

# Manual fallback if auto metadata is missing or auto mode is disabled
SMOKE_DENSITY_SCALE = 1.0

# Scene settings
SET_CYCLES = True
SET_EEVEE = False
SCENE_FPS = 10
VDB_COLLECTION_NAME = "FDS_VDB_VOLUMES"
CASE_PARENT_NAME = "FDS_VDB_CASE"

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def status(msg: str):
    print(msg, flush=True)


def get_text_editor_script_path():
    text = getattr(getattr(bpy.context, "space_data", None), "text", None)
    if text is not None and text.filepath:
        return Path(bpy.path.abspath(text.filepath)).resolve()
    return None


def script_dir() -> Path:
    p = get_text_editor_script_path()
    if p is not None:
        return p.parent
    return Path.cwd()


def resolve_vdb_dir() -> Path:
    if BLENDER_VDB_DIR:
        return Path(BLENDER_VDB_DIR).expanduser().resolve()
    return (script_dir() / "vdb_sequence").resolve()


def resolve_ranking_path(vdb_dir: Path, chid: str) -> Path:
    return vdb_dir / f"{chid}_mesh_ranking.json"


def ensure_collection(name: str):
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(coll)
    return coll


def ensure_empty(name: str, coll):
    obj = bpy.data.objects.get(name)
    if obj is None:
        obj = bpy.data.objects.new(name, None)
        coll.objects.link(obj)
    return obj


def mesh_index_from_manifest_name(path: Path) -> int:
    m = re.search(r"_mesh_(\d{4})_manifest\.json$", path.name)
    if m is None:
        raise ValueError(f"Could not parse mesh id from manifest name: {path.name}")
    return int(m.group(1))


def object_name_from_manifest(manifest: dict) -> str:
    return f"VDB_{manifest['chid']}_{manifest['mesh_id']}"


def discover_manifest_paths(vdb_dir: Path, chid: str, mesh_ids=None):
    paths = sorted(vdb_dir.glob(f"{chid}_mesh_*_manifest.json"), key=mesh_index_from_manifest_name)

    if mesh_ids is not None:
        wanted = {int(m) for m in mesh_ids}
        paths = [p for p in paths if mesh_index_from_manifest_name(p) in wanted]

    if not paths:
        raise FileNotFoundError(
            f"No manifests found for chid={chid!r} in {vdb_dir}. "
            f"Looked for {chid}_mesh_*_manifest.json"
        )
    return paths


def load_manifest(path: Path):
    return json.loads(path.read_text())


def manifest_signal_info(manifest: dict):
    frames = manifest.get("frames", [])
    if not frames:
        return {
            "temp_max": 0.0,
            "density_max": 0.0,
            "score": 0.0,
            "has_signal": False,
        }

    temp_max = max(float(fr.get("temperature_max", 0.0)) for fr in frames)
    density_max = max(float(fr.get("density_max", 0.0)) for fr in frames)

    temp_term = max(0.0, temp_max - SIGNAL_TEMP_MIN)
    dens_term = max(0.0, density_max - SIGNAL_DENSITY_MIN)
    dens_score = math.log10(1.0 + max(dens_term, 0.0) * 1.0e12) if dens_term > 0.0 else 0.0
    score = SIGNAL_TEMP_WEIGHT * temp_term + SIGNAL_DENSITY_WEIGHT * dens_score

    has_signal = (temp_max >= SIGNAL_TEMP_MIN) or (density_max > SIGNAL_DENSITY_MIN)

    return {
        "temp_max": temp_max,
        "density_max": density_max,
        "score": score,
        "has_signal": has_signal,
    }


def sort_by_rank(paths, vdb_dir: Path, chid: str):
    ranking_path = resolve_ranking_path(vdb_dir, chid)
    if not ranking_path.exists():
        status(f"Ranking file not found, falling back to mesh order: {ranking_path}")
        return sorted(paths, key=mesh_index_from_manifest_name)

    try:
        ranking = json.loads(ranking_path.read_text())
    except Exception as exc:
        status(f"Could not read ranking file ({exc}); falling back to mesh order.")
        return sorted(paths, key=mesh_index_from_manifest_name)

    rank_lookup = {}
    if isinstance(ranking, list):
        for i, item in enumerate(ranking):
            if isinstance(item, dict):
                mesh_idx = item.get("mesh_index_1based") or item.get("mesh_index") or item.get("mesh")
                if mesh_idx is not None:
                    rank_lookup[int(mesh_idx)] = i
            else:
                try:
                    rank_lookup[int(item)] = i
                except Exception:
                    pass
    elif isinstance(ranking, dict):
        for k, v in ranking.items():
            try:
                rank_lookup[int(k)] = int(v) if isinstance(v, int) else 0
            except Exception:
                pass

    def keyfunc(path: Path):
        mesh_idx = mesh_index_from_manifest_name(path)
        return (rank_lookup.get(mesh_idx, 10**9), mesh_idx)

    return sorted(paths, key=keyfunc)


def sort_manifest_paths(paths, vdb_dir: Path, chid: str, sort_mode: str):
    if sort_mode == "mesh":
        return sorted(paths, key=mesh_index_from_manifest_name), {}

    if sort_mode == "rank":
        return sort_by_rank(paths, vdb_dir, chid), {}

    info = {}
    sortable = []
    for path in paths:
        try:
            manifest = load_manifest(path)
            sig = manifest_signal_info(manifest)
            info[path] = sig
            sortable.append((sig["score"], sig["temp_max"], sig["density_max"], mesh_index_from_manifest_name(path), path))
        except Exception as exc:
            info[path] = {
                "temp_max": 0.0,
                "density_max": 0.0,
                "score": -1.0,
                "has_signal": False,
                "error": str(exc),
            }
            sortable.append((-1.0, 0.0, 0.0, mesh_index_from_manifest_name(path), path))

    sortable.sort(key=lambda x: (x[0], x[1], x[2], -x[3]), reverse=True)
    return [item[-1] for item in sortable], info


def slice_manifest_paths(paths):
    start = max(0, int(BLENDER_START_INDEX))
    stride = max(1, int(BLENDER_MANIFEST_STRIDE))
    sliced = paths[start::stride]
    if BLENDER_MAX_MESHES is not None:
        sliced = sliced[: int(BLENDER_MAX_MESHES)]
    return sliced


def effective_spacing(spacing):
    dx = abs(float(spacing[0]))
    dy = abs(float(spacing[1]))
    dz = abs(float(spacing[2]))
    return (dx * dy * dz) ** (1.0 / 3.0)


def compute_smoke_density_input(manifest: dict) -> float:
    if not USE_SMOKE:
        return 0.0

    if not USE_AUTO_SMOKE_SCALE:
        return float(SMOKE_DENSITY_SCALE)

    spacing = manifest.get("spacing", [1.0, 1.0, 1.0])
    km = float(manifest.get("smoke_mass_extinction", 0.0))
    deff = effective_spacing(spacing)

    auto_scale = km * deff * float(SMOKE_SCALE_MULTIPLIER)
    if auto_scale <= 0.0:
        return float(SMOKE_DENSITY_SCALE)
    return auto_scale


def build_base_material(name: str = "FDS_FlameSmoke_Volume_Base"):
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True

    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    out = nodes.new(type="ShaderNodeOutputMaterial")
    out.location = (840, 0)

    vol = nodes.new(type="ShaderNodeVolumePrincipled")
    vol.location = (520, 0)
    vol.name = "FDS_VolumePrincipled"

    try:
        vol.density_attribute = "density"
    except Exception:
        pass
    try:
        vol.temperature_attribute = "temperature"
    except Exception:
        pass

    vol.inputs["Color"].default_value = SMOKE_COLOR
    vol.inputs["Anisotropy"].default_value = SMOKE_ANISOTROPY
    vol.inputs["Density"].default_value = SMOKE_DENSITY_SCALE if USE_SMOKE else 0.0

    if "Blackbody Intensity" in vol.inputs:
        vol.inputs["Blackbody Intensity"].default_value = 0.0

    attr_temp = nodes.new(type="ShaderNodeAttribute")
    attr_temp.location = (-860, 160)
    attr_temp.attribute_name = "temperature"

    map_temp = nodes.new(type="ShaderNodeMapRange")
    map_temp.location = (-620, 160)
    map_temp.inputs[1].default_value = FLAME_TEMP_MIN
    map_temp.inputs[2].default_value = FLAME_TEMP_MAX
    map_temp.inputs[3].default_value = 0.0
    map_temp.inputs[4].default_value = 1.0
    map_temp.clamp = True

    ramp = nodes.new(type="ShaderNodeValToRGB")
    ramp.location = (-360, 160)
    while len(ramp.color_ramp.elements) > 2:
        ramp.color_ramp.elements.remove(ramp.color_ramp.elements[-1])
    e0 = ramp.color_ramp.elements[0]
    e1 = ramp.color_ramp.elements[1]
    e0.position = 0.0
    e0.color = (0.0, 0.0, 0.0, 1.0)
    e1.position = 1.0
    e1.color = (1.0, 0.97, 0.90, 1.0)
    e2 = ramp.color_ramp.elements.new(0.20)
    e2.color = (0.20, 0.00, 0.00, 1.0)
    e3 = ramp.color_ramp.elements.new(0.45)
    e3.color = (0.95, 0.12, 0.02, 1.0)
    e4 = ramp.color_ramp.elements.new(0.75)
    e4.color = (1.0, 0.55, 0.06, 1.0)

    emit_strength = nodes.new(type="ShaderNodeMath")
    emit_strength.location = (-120, 20)
    emit_strength.operation = 'MULTIPLY'
    emit_strength.inputs[1].default_value = EMISSION_STRENGTH

    links.new(attr_temp.outputs["Fac"], map_temp.inputs[0])
    links.new(map_temp.outputs["Result"], ramp.inputs[0])
    links.new(map_temp.outputs["Result"], emit_strength.inputs[0])

    if "Emission Color" in vol.inputs:
        links.new(ramp.outputs["Color"], vol.inputs["Emission Color"])
    if "Emission Strength" in vol.inputs:
        links.new(emit_strength.outputs["Value"], vol.inputs["Emission Strength"])

    links.new(vol.outputs["Volume"], out.inputs["Volume"])
    return mat


def material_for_manifest(base_mat, manifest: dict, obj_name: str):
    mat_name = f"FDS_FlameSmoke_{obj_name}"
    old = bpy.data.materials.get(mat_name)
    if old is not None:
        bpy.data.materials.remove(old, do_unlink=True)

    mat = base_mat.copy()
    mat.name = mat_name

    density_input = compute_smoke_density_input(manifest)
    vol = mat.node_tree.nodes.get("FDS_VolumePrincipled")
    if vol is not None:
        vol.inputs["Color"].default_value = SMOKE_COLOR
        vol.inputs["Anisotropy"].default_value = SMOKE_ANISOTROPY
        vol.inputs["Density"].default_value = density_input

    manifest["_loader_smoke_density_input"] = density_input
    return mat


def remove_existing_volume(name: str):
    obj = bpy.data.objects.get(name)
    if obj is not None:
        bpy.data.objects.remove(obj, do_unlink=True)


def existing_volume(name: str):
    return bpy.data.objects.get(name)


def usable_frames(vdb_dir: Path, manifest: dict):
    frames = manifest.get("frames", [])
    good = []
    for fr in frames:
        try:
            f = vdb_dir / fr["filename"]
        except Exception:
            break
        if not f.exists():
            break
        good.append(fr)
    return good


def import_volume_sequence(vdb_dir: Path, manifest_path: Path, base_mat, coll, parent):
    manifest = load_manifest(manifest_path)
    obj_name = object_name_from_manifest(manifest)

    if BLENDER_SKIP_EXISTING and existing_volume(obj_name) is not None:
        return existing_volume(obj_name), manifest, "existing"

    frames = usable_frames(vdb_dir, manifest)
    if not frames:
        msg = f"No usable VDB frames found for manifest: {manifest_path.name}"
        if BLENDER_SKIP_MISSING_VDB:
            status("SKIP: " + msg)
            return None, manifest, "missing"
        raise RuntimeError(msg)

    first_file = vdb_dir / frames[0]["filename"]

    if not first_file.exists():
        msg = f"Missing first VDB file: {first_file}"
        if BLENDER_SKIP_MISSING_VDB:
            status("SKIP: " + msg)
            return None, manifest, "missing"
        raise FileNotFoundError(msg)

    if not BLENDER_SKIP_EXISTING:
        remove_existing_volume(obj_name)

    bpy.ops.object.volume_import(filepath=str(first_file))
    obj = bpy.context.active_object
    obj.name = obj_name

    origin = manifest.get("origin", [0.0, 0.0, 0.0])
    spacing = manifest.get("spacing", [1.0, 1.0, 1.0])

    obj.location = tuple(float(v) for v in origin)
    obj.rotation_euler = (0.0, 0.0, 0.0)
    obj.scale = tuple(float(v) for v in spacing)
    obj.parent = parent

    vol = obj.data
    vol.filepath = str(first_file)
    vol.is_sequence = True
    vol.frame_start = 1
    vol.frame_duration = len(frames)
    vol.frame_offset = 0

    if hasattr(vol, "sequence_mode"):
        try:
            vol.sequence_mode = 'CLIP'
        except Exception:
            pass

    mat = material_for_manifest(base_mat, manifest, obj_name)
    if len(vol.materials) == 0:
        vol.materials.append(mat)
    else:
        vol.materials[0] = mat

    for old_coll in list(obj.users_collection):
        old_coll.objects.unlink(obj)
    coll.objects.link(obj)

    return obj, manifest, "imported"


def set_scene_engine():
    if SET_EEVEE:
        try:
            bpy.context.scene.render.engine = 'BLENDER_EEVEE'
        except Exception:
            pass
    elif SET_CYCLES:
        try:
            bpy.context.scene.render.engine = 'CYCLES'
        except Exception:
            pass


def run(chid: str = BLENDER_CHID, mesh_ids=BLENDER_MESH_IDS):
    vdb_dir = resolve_vdb_dir()
    all_paths = discover_manifest_paths(vdb_dir, chid, mesh_ids=mesh_ids)
    all_paths, signal_info = sort_manifest_paths(all_paths, vdb_dir, chid, BLENDER_SORT_MODE)

    if BLENDER_SORT_MODE == "signal" and SIGNAL_REQUIRE_DATA:
        filtered = []
        rejected = 0
        for path in all_paths:
            info = signal_info.get(path, {})
            if info.get("has_signal", False):
                filtered.append(path)
            else:
                rejected += 1
        all_paths = filtered
        status(f"Signal filter rejected {rejected} low/empty manifests")

    manifest_paths = slice_manifest_paths(all_paths)

    status(f"VDB dir            : {vdb_dir}")
    status(f"Total manifests    : {len(all_paths)}")
    status(f"Selected manifests : {len(manifest_paths)}")
    status(f"Sort mode          : {BLENDER_SORT_MODE}")
    status(f"Start index        : {BLENDER_START_INDEX}")
    status(f"Max meshes         : {BLENDER_MAX_MESHES}")

    if BLENDER_SORT_MODE == "signal" and manifest_paths:
        status("Top selected meshes by signal:")
        for path in manifest_paths[:min(10, len(manifest_paths))]:
            info = signal_info.get(path, {})
            status(
                f"  mesh {mesh_index_from_manifest_name(path):4d}  "
                f"score={info.get('score', 0.0):10.3f}  "
                f"Tmax={info.get('temp_max', 0.0):8.3f}  "
                f"rhoMax={info.get('density_max', 0.0):.6g}"
            )

    base_mat = build_base_material()
    coll = ensure_collection(VDB_COLLECTION_NAME)
    parent = ensure_empty(f"{CASE_PARENT_NAME}_{chid}", coll)

    objs = []
    manifests = []
    imported_count = 0
    skipped_count = 0

    for i, manifest_path in enumerate(manifest_paths, start=1):
        try:
            obj, manifest, mode = import_volume_sequence(vdb_dir, manifest_path, base_mat, coll, parent)
        except Exception as exc:
            if BLENDER_SKIP_BAD_MANIFESTS:
                skipped_count += 1
                status(f"SKIP BAD MANIFEST: {manifest_path.name} :: {exc}")
                continue
            raise

        if mode == "imported":
            imported_count += 1
            objs.append(obj)
            manifests.append(manifest)
            status(
                f"Loaded {obj.name}: smoke_density_input="
                f"{manifest.get('_loader_smoke_density_input', 0.0):.6g}"
            )
        elif mode == "existing":
            objs.append(obj)
            manifests.append(manifest)
        else:
            skipped_count += 1

        if (i % max(1, int(BLENDER_PRINT_EVERY))) == 0 or i == len(manifest_paths):
            status(
                f"Progress: {i}/{len(manifest_paths)}  "
                f"imported={imported_count} existing={len(objs)-imported_count} skipped={skipped_count}"
            )

    if manifests:
        frame_counts = [len(usable_frames(vdb_dir, m)) for m in manifests]
        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = max(frame_counts) if frame_counts else 1
        bpy.context.scene.render.fps = SCENE_FPS

    set_scene_engine()

    status(f"Imported/kept {len(objs)} VDB sequences from {vdb_dir}")
    status(f"Skipped {skipped_count} manifests")
    status("Material: per-object FDS_FlameSmoke_*")
    return objs


if __name__ == "__main__":
    if BLENDER_AUTORUN:
        run()
