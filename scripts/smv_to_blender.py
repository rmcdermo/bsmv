
#!/usr/bin/env python3
"""
Read an FDS/Smokeview .smv file and create corresponding geometry in Blender.

Updated to:
- parse CVENT blocks (circular vents)
- optionally read the sibling .fds file (<chid>.fds) for circular vent metadata
  and SURF COLOR names
- use those colors for circular pool vents such as LNG_POOL and WATER_POOL

Two ways to use this script:

1. From Terminal:

   blender --python smv_to_blender.py --chid simple_test

2. From Blender's Text Editor:

   - open this file in the Scripting workspace
   - edit the BLENDER_* settings below if needed
   - press Alt-P (Run Script)

Optional from Blender's Python Console:

   import runpy
   ns = runpy.run_path('/full/path/smv_to_blender.py')
   ns['run'](chid='simple_test', smv_dir='/full/path/to/case_dir')
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import bpy
from mathutils import Vector

EPS = 1.0e-9

# -----------------------------------------------------------------------------
# Blender Text Editor defaults
# -----------------------------------------------------------------------------

BLENDER_AUTORUN = True
BLENDER_CHID = "simple_test"
BLENDER_SMV_DIR = None
BLENDER_SMV_PATH = None

BLENDER_OPEN_OUTLINE = True
BLENDER_ROOM_OUTLINE = False
BLENDER_SHOW_OPEN_FACE = False
BLENDER_OPEN_FRAME_WIDTH = 0.01
BLENDER_SHOW_MESH_SEAMS = False
BLENDER_OPEN_IN_RENDER = False
BLENDER_CIRCLE_VERTS = 128

# -----------------------------------------------------------------------------
# Color helpers
# -----------------------------------------------------------------------------

FDS_NAMED_COLORS: Dict[str, Tuple[float, float, float, float]] = {
   "BLACK":      (0.00, 0.00, 0.00, 1.0),
   "WHITE":      (1.00, 1.00, 1.00, 1.0),
   "RED":        (1.00, 0.00, 0.00, 1.0),
   "GREEN":      (0.00, 1.00, 0.00, 1.0),
   "BLUE":       (0.00, 0.00, 1.00, 1.0),
   "YELLOW":     (1.00, 1.00, 0.00, 1.0),
   "CYAN":       (0.00, 1.00, 1.00, 1.0),
   "MAGENTA":    (1.00, 0.00, 1.00, 1.0),
   "AQUA":       (0.00, 1.00, 1.00, 1.0),
   "AQUAMARINE": (0.498, 1.000, 0.831, 1.0),
   "GRAY":       (0.50, 0.50, 0.50, 1.0),
   "GREY":       (0.50, 0.50, 0.50, 1.0),
   "SILVER":     (0.75, 0.75, 0.75, 1.0),
   "ORANGE":     (1.00, 0.65, 0.00, 1.0),
   "BROWN":      (0.65, 0.16, 0.16, 1.0),
   "TAN":        (0.82, 0.71, 0.55, 1.0),
   "PURPLE":     (0.50, 0.00, 0.50, 1.0),
   "PINK":       (1.00, 0.75, 0.80, 1.0),
}

def choose_surface_color(surface_name: str,
                         surface_color_name: Optional[str] = None,
                         surface_rgba: Optional[Tuple[float, float, float, float]] = None) -> Tuple[float, float, float, float]:
   if surface_color_name:
      rgba = FDS_NAMED_COLORS.get(surface_color_name.strip().upper())
      if rgba is not None:
         return rgba

   if surface_rgba is not None:
      return surface_rgba

   s = surface_name.upper()
   if s == "INERT":
      return (0.93, 0.74, 0.31, 1.0)
   if "BURNER" in s:
      return (1.00, 0.08, 0.02, 1.0)
   if "OPEN" in s:
      return (1.00, 1.00, 1.00, 0.05)
   return (0.75, 0.75, 0.75, 1.0)

# -----------------------------------------------------------------------------
# Blender Text Editor path helper
# -----------------------------------------------------------------------------

def get_text_editor_script_path() -> Optional[Path]:
   candidates: List[Path] = []

   try:
      space = getattr(bpy.context, "space_data", None)
      if space is not None and getattr(space, "type", None) == 'TEXT_EDITOR':
         text_block = getattr(space, "text", None)
         if text_block is not None:
            fp = getattr(text_block, "filepath", "")
            if fp:
               candidates.append(Path(bpy.path.abspath(fp)).expanduser())
   except Exception:
      pass

   try:
      screen = getattr(bpy.context, "screen", None)
      if screen is not None:
         for area in screen.areas:
            if area.type != 'TEXT_EDITOR':
               continue
            space = area.spaces.active
            text_block = getattr(space, "text", None)
            if text_block is not None:
               fp = getattr(text_block, "filepath", "")
               if fp:
                  candidates.append(Path(bpy.path.abspath(fp)).expanduser())
   except Exception:
      pass

   try:
      fp = globals().get("__file__", "")
      if fp:
         candidates.append(Path(fp).expanduser())
   except Exception:
      pass

   try:
      for text_block in bpy.data.texts:
         fp = getattr(text_block, "filepath", "")
         if fp:
            candidates.append(Path(bpy.path.abspath(fp)).expanduser())
   except Exception:
      pass

   seen = set()
   for cand in candidates:
      try:
         rc = cand.resolve()
      except Exception:
         continue
      key = str(rc)
      if key in seen:
         continue
      seen.add(key)
      if rc.exists():
         return rc

   return None

# -----------------------------------------------------------------------------
# Generic parsing helpers
# -----------------------------------------------------------------------------

def _next_nonempty(lines: List[str], i: int) -> int:
   while i < len(lines) and lines[i].strip() == "":
      i += 1
   return i


def _parse_float_list(text: str) -> List[float]:
   vals: List[float] = []
   for token in text.replace(",", " ").split():
      try:
         vals.append(float(token))
      except ValueError:
         pass
   return vals


def _round_key(values: Tuple[float, ...], ndigits: int = 6) -> Tuple[float, ...]:
   return tuple(round(v, ndigits) for v in values)

# -----------------------------------------------------------------------------
# FDS parsing (optional metadata / validation)
# -----------------------------------------------------------------------------

def parse_fds(path: Path) -> dict:
   text = path.read_text()
   text = re.sub(r'!.*', '', text)
   records = []
   for rec in text.split('/'):
      rec = rec.strip()
      if rec:
         records.append(rec)

   data = {
      "surface_color_names": {},
      "circular_vents": [],
   }

   for rec in records:
      rec_up = rec.upper()

      if rec_up.startswith("&SURF"):
         m_id = re.search(r"\bID\s*=\s*'([^']+)'", rec, re.I | re.S)
         if m_id is None:
            continue
         surf_id = m_id.group(1).strip()

         m_color = re.search(r"\bCOLOR\s*=\s*'([^']+)'", rec, re.I | re.S)
         if m_color is not None:
            data["surface_color_names"][surf_id] = m_color.group(1).strip()
         continue

      if rec_up.startswith("&VENT"):
         m_radius = re.search(r"\bRADIUS\s*=\s*([-+0-9.Ee]+)", rec, re.I | re.S)
         if m_radius is None:
            continue

         m_id = re.search(r"\bID\s*=\s*'([^']+)'", rec, re.I | re.S)
         m_surf = re.search(r"\bSURF_ID\s*=\s*'([^']+)'", rec, re.I | re.S)
         m_xyz = re.search(r"\bXYZ\s*=\s*([^/]+?)(?=\s+\b[A-Z_]+\s*=|$)", rec, re.I | re.S)
         m_xb  = re.search(r"\bXB\s*=\s*([^/]+?)(?=\s+\b[A-Z_]+\s*=|$)", rec, re.I | re.S)

         center = (0.0, 0.0, 0.0)
         xb = None

         if m_xyz is not None:
            xyz_vals = _parse_float_list(m_xyz.group(1))
            if len(xyz_vals) >= 3:
               center = (xyz_vals[0], xyz_vals[1], xyz_vals[2])

         if m_xb is not None:
            xb_vals = _parse_float_list(m_xb.group(1))
            if len(xb_vals) >= 6:
               xb = tuple(xb_vals[:6])

         data["circular_vents"].append({
            "id": m_id.group(1).strip() if m_id else None,
            "surface_name": m_surf.group(1).strip() if m_surf else None,
            "center": center,
            "radius": float(m_radius.group(1)),
            "xb": xb,
         })

   return data

# -----------------------------------------------------------------------------
# SMV parsing
# -----------------------------------------------------------------------------

def parse_smv(path: Path, fds_info: Optional[dict] = None) -> dict:
   lines = path.read_text().splitlines()
   i = 0

   data = {
      "chid": path.stem,
      "surfdef": "INERT",
      "surface_order": [],
      "surface_rgba": {},
      "outline_segments": [],
      "obsts": [],
      "vents": [],
      "cvents": [],
      "ventorig": [],
      "pdim": None,
   }

   while i < len(lines):
      i = _next_nonempty(lines, i)
      if i >= len(lines):
         break

      key = lines[i].strip()

      if key == "CHID":
         i = _next_nonempty(lines, i + 1)
         data["chid"] = lines[i].strip()
         i += 1
         continue

      if key == "SURFDEF":
         i = _next_nonempty(lines, i + 1)
         data["surfdef"] = lines[i].strip()
         i += 1
         continue

      if key == "SURFACE":
         i = _next_nonempty(lines, i + 1)
         name = lines[i].strip()
         data["surface_order"].append(name)

         i = _next_nonempty(lines, i + 1)
         i = _next_nonempty(lines, i + 1)
         rgba_line = lines[i].split('!')[0].strip()
         rgba_vals = _parse_float_list(rgba_line)

         if len(rgba_vals) >= 7:
            # Use the final RGB triplet written by Smokeview.
            data["surface_rgba"][name] = (rgba_vals[4], rgba_vals[5], rgba_vals[6], 1.0)
         elif len(rgba_vals) >= 4:
            data["surface_rgba"][name] = (rgba_vals[-3], rgba_vals[-2], rgba_vals[-1], 1.0)

         i = _next_nonempty(lines, i + 1)
         i += 1
         continue

      if key == "OUTLINE":
         i = _next_nonempty(lines, i + 1)
         nseg = int(lines[i].split()[0])
         i += 1
         for _ in range(nseg):
            vals = [float(x) for x in lines[i].split()[:6]]
            data["outline_segments"].append(tuple(vals))
            i += 1
         continue

      if key == "VENTORIG":
         i = _next_nonempty(lines, i + 1)
         nvent = int(lines[i].split()[0])
         i += 1
         for iv in range(nvent):
            raw = lines[i].split('!')[0]
            vals = _parse_float_list(raw)
            if len(vals) >= 6:
               data["ventorig"].append({
                  "name": f"VENTORIG_{iv+1:04d}",
                  "xb": tuple(vals[:6]),
               })
            i += 1
         continue

      if key == "PDIM":
         i = _next_nonempty(lines, i + 1)
         vals = [float(x) for x in lines[i].split()[:6]]
         data["pdim"] = tuple(vals)
         i += 1
         continue

      if key == "OBST":
         i = _next_nonempty(lines, i + 1)
         nobst = int(lines[i].split()[0])
         i += 1
         for iob in range(nobst):
            vals = lines[i].split()
            xb = tuple(float(x) for x in vals[:6])
            data["obsts"].append({
               "name": f"OBST_{iob+1:04d}",
               "xb": xb,
            })
            i += 1
            i += 1
         continue

      if key == "VENT":
         i = _next_nonempty(lines, i + 1)
         nvent = int(lines[i].split()[0])
         i += 1
         vent_defs = []
         for iv in range(nvent):
            vals = lines[i].split()
            xb = tuple(float(x) for x in vals[:6])
            surface_index = 0
            if len(vals) >= 8:
               surface_index = int(vals[7])
            vent_defs.append({
               "name": f"VENT_{iv+1:04d}",
               "xb": xb,
               "surface_index": surface_index,
            })
            i += 1
         i += nvent
         data["vents"].extend(vent_defs)
         continue

      if key == "CVENT":
         i = _next_nonempty(lines, i + 1)
         ncvent = int(lines[i].split()[0])
         i += 1

         cvent_defs = []
         for iv in range(ncvent):
            raw = lines[i].rstrip()
            left = raw.split('%')[0]
            vals = left.split()

            xb = tuple(float(x) for x in vals[:6])
            surface_index = 0
            if len(vals) >= 8:
               surface_index = int(vals[7])

            center = None
            radius = None
            if '%' in raw:
               rhs = raw.split('%', 1)[1]
               rhs_vals = _parse_float_list(rhs)
               if len(rhs_vals) >= 4:
                  center = (rhs_vals[0], rhs_vals[1], rhs_vals[2])
                  radius = rhs_vals[3]

            cvent_defs.append({
               "name": f"CVENT_{iv+1:04d}",
               "xb": xb,
               "surface_index": surface_index,
               "center": center,
               "radius": radius,
            })
            i += 1

         i += ncvent
         data["cvents"].extend(cvent_defs)
         continue

      i += 1

   surface_index_lookup = {0: data["surfdef"]}
   next_index = 1
   for name in data["surface_order"]:
      if name == data["surfdef"]:
         continue
      surface_index_lookup[next_index] = name
      next_index += 1
   data["surface_index_lookup"] = surface_index_lookup

   for vent in data["vents"]:
      vent["surface_name"] = surface_index_lookup.get(vent["surface_index"], data["surfdef"])

   for cvent in data["cvents"]:
      cvent["surface_name"] = surface_index_lookup.get(cvent["surface_index"], data["surfdef"])

   data["surface_color_names"] = {}
   if fds_info is not None:
      data["surface_color_names"] = dict(fds_info.get("surface_color_names", {}))

   data["circular_vents"] = consolidate_cvents(data["cvents"], fds_info=fds_info)
   return data


def _cvent_plane_axis(xb: Tuple[float, float, float, float, float, float]) -> Tuple[str, float]:
   x1, x2, y1, y2, z1, z2 = xb
   if abs(x2 - x1) < EPS:
      return "x", x1
   if abs(y2 - y1) < EPS:
      return "y", y1
   if abs(z2 - z1) < EPS:
      return "z", z1
   raise ValueError(f"CVENT is not planar: {xb}")


def consolidate_cvents(cvents: List[dict], fds_info: Optional[dict] = None) -> List[dict]:
   grouped: Dict[Tuple, dict] = {}

   for cv in cvents:
      if cv.get("center") is None or cv.get("radius") is None:
         continue

      axis, plane_value = _cvent_plane_axis(cv["xb"])
      key = (
         cv["surface_name"],
         axis,
         round(plane_value, 6),
         round(cv["center"][0], 6),
         round(cv["center"][1], 6),
         round(cv["center"][2], 6),
         round(cv["radius"], 6),
      )

      if key not in grouped:
         grouped[key] = {
            "name": cv["name"],
            "surface_name": cv["surface_name"],
            "xb": cv["xb"],
            "axis": axis,
            "plane_value": plane_value,
            "center": cv["center"],
            "radius": cv["radius"],
            "parts": 1,
         }
      else:
         g = grouped[key]
         g["parts"] += 1
         x1, x2, y1, y2, z1, z2 = g["xb"]
         a1, a2, b1, b2, c1, c2 = cv["xb"]
         g["xb"] = (
            min(x1, a1), max(x2, a2),
            min(y1, b1), max(y2, b2),
            min(z1, c1), max(z2, c2),
         )

   circles = list(grouped.values())

   if fds_info is not None:
      fds_circles = fds_info.get("circular_vents", [])
      for cv in circles:
         best = None
         best_err = 1.0e99

         for fv in fds_circles:
            if fv.get("surface_name") not in (None, cv["surface_name"]):
               continue
            if fv.get("radius") is None:
               continue

            c0 = cv["center"]
            c1 = fv["center"]
            err = abs(cv["radius"] - fv["radius"])
            err += abs(c0[0] - c1[0]) + abs(c0[1] - c1[1]) + abs(c0[2] - c1[2])

            if err < best_err:
               best_err = err
               best = fv

         if best is not None and best_err < 1.0e-4:
            cv["fds_id"] = best.get("id")
            if best.get("surface_name"):
               cv["surface_name"] = best["surface_name"]
            if best.get("xb") is not None:
               cv["fds_xb"] = best["xb"]

   circles.sort(key=lambda d: (d["surface_name"], d.get("fds_id") or d["name"]))
   return circles

# -----------------------------------------------------------------------------
# Blender helpers
# -----------------------------------------------------------------------------

def clear_collection(name: str) -> bpy.types.Collection:
   old = bpy.data.collections.get(name)
   if old is not None:
      objs = list(old.objects)
      for obj in objs:
         bpy.data.objects.remove(obj, do_unlink=True)
      bpy.data.collections.remove(old)

   coll = bpy.data.collections.new(name)
   bpy.context.scene.collection.children.link(coll)
   return coll


def make_principled_material(name: str, color: Tuple[float, float, float, float], alpha: float = 1.0) -> bpy.types.Material:
   mat = bpy.data.materials.get(name)
   if mat is None:
      mat = bpy.data.materials.new(name=name)

   mat.use_nodes = True
   nodes = mat.node_tree.nodes
   links = mat.node_tree.links
   for node in list(nodes):
      nodes.remove(node)

   out = nodes.new(type="ShaderNodeOutputMaterial")
   bsdf = nodes.new(type="ShaderNodeBsdfPrincipled")
   bsdf.inputs["Base Color"].default_value = color
   bsdf.inputs["Roughness"].default_value = 0.6
   bsdf.inputs["Alpha"].default_value = alpha
   links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

   if alpha < 0.999:
      mat.blend_method = 'BLEND'
      mat.use_transparent_shadow = False

   return mat


def make_emission_material(name: str, color: Tuple[float, float, float, float], strength: float = 1.0) -> bpy.types.Material:
   mat = bpy.data.materials.get(name)
   if mat is None:
      mat = bpy.data.materials.new(name=name)

   mat.use_nodes = True
   nodes = mat.node_tree.nodes
   links = mat.node_tree.links
   for node in list(nodes):
      nodes.remove(node)

   out = nodes.new(type="ShaderNodeOutputMaterial")
   emit = nodes.new(type="ShaderNodeEmission")
   emit.inputs["Color"].default_value = color
   emit.inputs["Strength"].default_value = strength
   links.new(emit.outputs["Emission"], out.inputs["Surface"])
   return mat


def _rect_vertices(xb: Tuple[float, float, float, float, float, float], offset: float = 0.0) -> List[Tuple[float, float, float]]:
   x1, x2, y1, y2, z1, z2 = xb

   if abs(x2 - x1) < EPS:
      x = x1 + offset
      return [(x, y1, z1), (x, y2, z1), (x, y2, z2), (x, y1, z2)]

   if abs(y2 - y1) < EPS:
      y = y1 + offset
      return [(x1, y, z1), (x2, y, z1), (x2, y, z2), (x1, y, z2)]

   if abs(z2 - z1) < EPS:
      z = z1 + offset
      return [(x1, y1, z), (x2, y1, z), (x2, y2, z), (x1, y2, z)]

   raise ValueError(f"VENT is not a plane: {xb}")


def add_rect_face(name: str, xb: Tuple[float, float, float, float, float, float], mat: bpy.types.Material, coll: bpy.types.Collection, offset: float = 0.0, hide_render: bool = False):
   verts = _rect_vertices(xb, offset=offset)
   mesh = bpy.data.meshes.new(name + "_mesh")
   mesh.from_pydata(verts, [], [(0, 1, 2, 3)])
   mesh.update()

   obj = bpy.data.objects.new(name, mesh)
   obj.data.materials.append(mat)
   coll.objects.link(obj)
   obj.hide_render = bool(hide_render)
   return obj


def add_segment(name: str, p1: Tuple[float, float, float], p2: Tuple[float, float, float], mat: bpy.types.Material, coll: bpy.types.Collection, bevel: float = 0.005):
   curve = bpy.data.curves.new(name + "_curve", type='CURVE')
   curve.dimensions = '3D'
   curve.bevel_depth = bevel
   curve.bevel_resolution = 3

   spline = curve.splines.new('POLY')
   spline.points.add(1)
   spline.points[0].co = (*p1, 1.0)
   spline.points[1].co = (*p2, 1.0)

   obj = bpy.data.objects.new(name, curve)
   obj.data.materials.append(mat)
   coll.objects.link(obj)
   return obj


def add_box(name: str, xb: Tuple[float, float, float, float, float, float], mat: bpy.types.Material, coll: bpy.types.Collection):
   x1, x2, y1, y2, z1, z2 = xb
   cx = 0.5*(x1 + x2)
   cy = 0.5*(y1 + y2)
   cz = 0.5*(z1 + z2)
   sx = 0.5*(x2 - x1)
   sy = 0.5*(y2 - y1)
   sz = 0.5*(z2 - z1)

   bpy.ops.mesh.primitive_cube_add(location=(cx, cy, cz))
   obj = bpy.context.active_object
   obj.name = name
   obj.scale = (sx, sy, sz)
   if len(obj.data.materials) == 0:
      obj.data.materials.append(mat)
   else:
      obj.data.materials[0] = mat

   for old in list(obj.users_collection):
      old.objects.unlink(obj)
   coll.objects.link(obj)
   return obj


def add_circle_face(name: str,
                    axis: str,
                    plane_value: float,
                    center: Tuple[float, float, float],
                    radius: float,
                    mat: bpy.types.Material,
                    coll: bpy.types.Collection,
                    offset: float = 0.0,
                    vertices: int = 128):
   if axis == "x":
      location = (plane_value + offset, center[1], center[2])
      normal = Vector((1.0, 0.0, 0.0))
   elif axis == "y":
      location = (center[0], plane_value + offset, center[2])
      normal = Vector((0.0, 1.0, 0.0))
   elif axis == "z":
      location = (center[0], center[1], plane_value + offset)
      normal = Vector((0.0, 0.0, 1.0))
   else:
      raise ValueError(f"Unknown circle axis: {axis}")

   bpy.ops.mesh.primitive_circle_add(vertices=vertices, radius=radius, fill_type='NGON', location=location)
   obj = bpy.context.active_object
   obj.name = name
   obj.rotation_euler = Vector((0.0, 0.0, 1.0)).rotation_difference(normal).to_euler()

   if len(obj.data.materials) == 0:
      obj.data.materials.append(mat)
   else:
      obj.data.materials[0] = mat

   for old in list(obj.users_collection):
      old.objects.unlink(obj)
   coll.objects.link(obj)
   return obj


def look_at(obj: bpy.types.Object, target: Vector):
   direction = target - obj.location
   quat = direction.to_track_quat('-Z', 'Y')
   obj.rotation_euler = quat.to_euler()


def setup_camera_and_light(data: dict, coll: bpy.types.Collection):
   if data["pdim"] is None:
      return

   x1, x2, y1, y2, z1, z2 = data["pdim"]
   cx = 0.5*(x1 + x2)
   cy = 0.5*(y1 + y2)
   cz = 0.5*(z1 + z2)
   lx = x2 - x1
   ly = y2 - y1
   lz = z2 - z1

   cam_data = bpy.data.cameras.new(data["chid"] + "_cam")
   cam = bpy.data.objects.new(data["chid"] + "_cam", cam_data)
   cam.location = (cx, y1 - 1.7*max(lx, ly), z1 + 0.52*lz)
   cam.data.lens = 35.0
   coll.objects.link(cam)
   look_at(cam, Vector((cx, cy, cz)))
   bpy.context.scene.camera = cam

   light_data = bpy.data.lights.new(data["chid"] + "_sun", type='SUN')
   light_data.energy = 2.0
   sun = bpy.data.objects.new(data["chid"] + "_sun", light_data)
   sun.location = (cx - 2.0*lx, y1 - 0.5*ly, z2 + 2.0*lz)
   sun.rotation_euler = (math.radians(35.0), 0.0, math.radians(-20.0))
   coll.objects.link(sun)


def _touch_or_overlap(a1, a2, b1, b2, eps=1.0e-9):
   return not (a2 < b1 - eps or b2 < a1 - eps)


def _same(a, b, eps=1.0e-9):
   return abs(a - b) < eps


def _merge_two_open_xb(xb1, xb2, eps=1.0e-9):
   x1a, x2a, y1a, y2a, z1a, z2a = xb1
   x1b, x2b, y1b, y2b, z1b, z2b = xb2

   if _same(x1a, x2a, eps) and _same(x1b, x2b, eps) and _same(x1a, x1b, eps):
      same_y = _same(y1a, y1b, eps) and _same(y2a, y2b, eps)
      same_z = _same(z1a, z1b, eps) and _same(z2a, z2b, eps)

      if same_y and _touch_or_overlap(z1a, z2a, z1b, z2b, eps):
         return (x1a, x2a, y1a, y2a, min(z1a, z1b), max(z2a, z2b))
      if same_z and _touch_or_overlap(y1a, y2a, y1b, y2b, eps):
         return (x1a, x2a, min(y1a, y1b), max(y2a, y2b), z1a, z2a)

   if _same(y1a, y2a, eps) and _same(y1b, y2b, eps) and _same(y1a, y1b, eps):
      same_x = _same(x1a, x1b, eps) and _same(x2a, x2b, eps)
      same_z = _same(z1a, z1b, eps) and _same(z2a, z2b, eps)

      if same_x and _touch_or_overlap(z1a, z2a, z1b, z2b, eps):
         return (x1a, x2a, y1a, y2a, min(z1a, z1b), max(z2a, z2b))
      if same_z and _touch_or_overlap(x1a, x2a, x1b, x2b, eps):
         return (min(x1a, x1b), max(x2a, x2b), y1a, y2a, z1a, z2a)

   if _same(z1a, z2a, eps) and _same(z1b, z2b, eps) and _same(z1a, z1b, eps):
      same_x = _same(x1a, x1b, eps) and _same(x2a, x2b, eps)
      same_y = _same(y1a, y1b, eps) and _same(y2a, y2b, eps)

      if same_x and _touch_or_overlap(y1a, y2a, y1b, y2b, eps):
         return (x1a, x2a, min(y1a, y1b), max(y2a, y2b), z1a, z2a)
      if same_y and _touch_or_overlap(x1a, x2a, x1b, x2b, eps):
         return (min(x1a, x1b), max(x2a, x2b), y1a, y2a, z1a, z2a)

   return None


def coalesce_open_vents(vents: List[dict]) -> List[dict]:
   open_vents = [dict(v) for v in vents if v["surface_name"].upper() == "OPEN"]
   changed = True
   while changed:
      changed = False
      merged = []
      used = [False] * len(open_vents)

      for i in range(len(open_vents)):
         if used[i]:
            continue
         xb = open_vents[i]["xb"]

         for j in range(i + 1, len(open_vents)):
            if used[j]:
               continue
            xb_new = _merge_two_open_xb(xb, open_vents[j]["xb"])
            if xb_new is not None:
               xb = xb_new
               used[j] = True
               changed = True

         used[i] = True
         vv = dict(open_vents[i])
         vv["xb"] = xb
         merged.append(vv)

      open_vents = merged

   return open_vents


def add_rect_frame(name: str, xb, mat, coll, width: float = 0.01, offset: float = 0.001, hide_render: bool = False):
   x1, x2, y1, y2, z1, z2 = xb

   if abs(x2 - x1) < EPS:
      x = x1 + offset
      w = min(width, 0.49*(y2-y1), 0.49*(z2-z1))
      add_rect_face(name+"_left",   (x, x, y1, y1+w, z1, z2), mat, coll, hide_render=hide_render)
      add_rect_face(name+"_right",  (x, x, y2-w, y2, z1, z2), mat, coll, hide_render=hide_render)
      add_rect_face(name+"_bottom", (x, x, y1+w, y2-w, z1, z1+w), mat, coll, hide_render=hide_render)
      add_rect_face(name+"_top",    (x, x, y1+w, y2-w, z2-w, z2), mat, coll, hide_render=hide_render)
      return

   if abs(y2 - y1) < EPS:
      y = y1 + offset
      w = min(width, 0.49*(x2-x1), 0.49*(z2-z1))
      add_rect_face(name+"_left",   (x1, x1+w, y, y, z1, z2), mat, coll, hide_render=hide_render)
      add_rect_face(name+"_right",  (x2-w, x2, y, y, z1, z2), mat, coll, hide_render=hide_render)
      add_rect_face(name+"_bottom", (x1+w, x2-w, y, y, z1, z1+w), mat, coll, hide_render=hide_render)
      add_rect_face(name+"_top",    (x1+w, x2-w, y, y, z2-w, z2), mat, coll, hide_render=hide_render)
      return

   if abs(z2 - z1) < EPS:
      z = z1 + offset
      w = min(width, 0.49*(x2-x1), 0.49*(y2-y1))
      add_rect_face(name+"_left",   (x1, x1+w, y1, y2, z, z), mat, coll, hide_render=hide_render)
      add_rect_face(name+"_right",  (x2-w, x2, y1, y2, z, z), mat, coll, hide_render=hide_render)
      add_rect_face(name+"_bottom", (x1+w, x2-w, y1, y1+w, z, z), mat, coll, hide_render=hide_render)
      add_rect_face(name+"_top",    (x1+w, x2-w, y2-w, y2, z, z), mat, coll, hide_render=hide_render)
      return

   raise ValueError(f"OPEN vent is not planar: {xb}")

# -----------------------------------------------------------------------------
# Build scene
# -----------------------------------------------------------------------------

def build_scene(data: dict, make_open_outline: bool = True, make_room_outline: bool = True):
   coll = clear_collection(data["chid"] + "_SMV")

   mats: Dict[str, bpy.types.Material] = {}
   surface_names = {data["surfdef"]}
   surface_names |= {v["surface_name"] for v in data["vents"]}
   surface_names |= {v["surface_name"] for v in data["circular_vents"]}

   for sname in surface_names:
      rgba = choose_surface_color(
         sname,
         surface_color_name=data.get("surface_color_names", {}).get(sname),
         surface_rgba=data.get("surface_rgba", {}).get(sname),
      )
      mats[sname] = make_principled_material("MAT_" + sname.replace(" ", "_"), rgba, alpha=rgba[3])

   outline_mat = make_emission_material("MAT_OUTLINE_BLACK", (0.0, 0.0, 0.0, 1.0), strength=1.0)
   open_mat = make_principled_material("MAT_OPEN_MAGENTA", (1.0, 0.0, 1.0, 1.0), alpha=1.0)

   obst_mat = mats.get(
      data["surfdef"],
      make_principled_material("MAT_OBST", choose_surface_color(data["surfdef"]))
   )

   for obst in data["obsts"]:
      add_box(obst["name"], obst["xb"], obst_mat, coll)

   non_open_vents = [v for v in data["vents"] if v["surface_name"].upper() != "OPEN"]
   open_vents = coalesce_open_vents(data["vents"])

   if not BLENDER_SHOW_MESH_SEAMS:
      merged = []
      by_surface: Dict[str, List[dict]] = {}
      for v in non_open_vents:
         by_surface.setdefault(v["surface_name"], []).append(dict(v))

      for sname, group in by_surface.items():
         changed = True
         while changed:
            changed = False
            new_group = []
            used = [False] * len(group)

            for i in range(len(group)):
               if used[i]:
                  continue
               xb = group[i]["xb"]

               for j in range(i + 1, len(group)):
                  if used[j]:
                     continue
                  xb_new = _merge_two_open_xb(xb, group[j]["xb"])
                  if xb_new is not None:
                     xb = xb_new
                     used[j] = True
                     changed = True

               used[i] = True
               vv = dict(group[i])
               vv["xb"] = xb
               new_group.append(vv)

            group = new_group

         merged.extend(group)

      non_open_vents = merged

   for iv, vent in enumerate(non_open_vents, start=1):
      sname = vent["surface_name"]
      xb = vent["xb"]
      name = f"{sname}_{iv:04d}"

      offset = 0.0
      if "BURNER" in sname.upper():
         offset = 0.001

      add_rect_face(name, xb, mats[sname], coll, offset=offset)

   for iv, cvent in enumerate(data["circular_vents"], start=1):
      sname = cvent["surface_name"]
      name_root = cvent.get("fds_id") or sname
      name = f"{name_root}_{iv:04d}"

      offset = 0.0
      if cvent["axis"] == "z":
         if sname.upper() == "LNG_POOL":
            offset = 2.0e-4
         elif sname.upper() == "WATER_POOL":
            offset = 1.0e-4

      add_circle_face(
         name=name,
         axis=cvent["axis"],
         plane_value=cvent["plane_value"],
         center=cvent["center"],
         radius=cvent["radius"],
         mat=mats[sname],
         coll=coll,
         offset=offset,
         vertices=BLENDER_CIRCLE_VERTS,
      )

   if make_open_outline:
      open_fill_mat = None
      if BLENDER_SHOW_OPEN_FACE:
         open_fill_mat = make_principled_material("MAT_OPEN_FILL", (1.0, 0.0, 1.0, 0.08), alpha=0.08)

      for iv, vent in enumerate(open_vents, start=1):
         xb = vent["xb"]
         name = f"OPEN_{iv:04d}"

         if BLENDER_SHOW_OPEN_FACE:
            add_rect_face(name + "_fill", xb, open_fill_mat, coll, offset=0.001, hide_render=not BLENDER_OPEN_IN_RENDER)

         add_rect_frame(
            name + "_outline",
            xb,
            open_mat,
            coll,
            width=BLENDER_OPEN_FRAME_WIDTH,
            offset=0.001,
            hide_render=not BLENDER_OPEN_IN_RENDER
         )

   if make_room_outline:
      for iseg, seg in enumerate(data["outline_segments"], start=1):
         p1 = (seg[0], seg[1], seg[2])
         p2 = (seg[3], seg[4], seg[5])
         add_segment(f"OUTLINE_{iseg:04d}", p1, p2, outline_mat, coll, bevel=0.004)

   setup_camera_and_light(data, coll)
   bpy.context.scene.render.engine = 'BLENDER_EEVEE'

   print(f"Built scene for CHID = {data['chid']}")
   print(f"  OBST count       = {len(data['obsts'])}")
   print(f"  VENT count       = {len(data['vents'])}")
   print(f"  OPEN merged      = {len(open_vents)}")
   print(f"  CVENT pieces     = {len(data['cvents'])}")
   print(f"  CVENT circles    = {len(data['circular_vents'])}")
   for cv in data["circular_vents"]:
      print(f"    {cv.get('fds_id') or cv['surface_name']}: center={cv['center']} radius={cv['radius']} plane={cv['axis']}={cv['plane_value']}")
   print(f"  SURFDEF          = {data['surfdef']}")
   print(f"  Surfaces         = {data['surface_index_lookup']}")

# -----------------------------------------------------------------------------
# Path resolution
# -----------------------------------------------------------------------------

def resolve_smv_path(chid: str, smv: Optional[str] = None, smv_dir: Optional[str] = None) -> Path:
   if smv is not None:
      return Path(smv).expanduser().resolve()

   search_dirs: List[Path] = []

   if smv_dir is not None:
      search_dirs.append(Path(smv_dir).expanduser().resolve())

   script_path = get_text_editor_script_path()
   if script_path is not None:
      search_dirs.append(script_path.parent)
      search_dirs.append(script_path.parent / "Simple_Test")

   search_dirs.append(Path.cwd())
   search_dirs.append(Path.cwd() / "Simple_Test")

   seen = set()
   for d in search_dirs:
      try:
         rd = d.resolve()
      except Exception:
         continue
      key = str(rd)
      if key in seen:
         continue
      seen.add(key)
      candidate = rd / f"{chid}.smv"
      if candidate.exists():
         return candidate

   if search_dirs:
      try:
         return search_dirs[0].resolve() / f"{chid}.smv"
      except Exception:
         pass
   return Path.cwd() / f"{chid}.smv"


def resolve_fds_path(smv_path: Path) -> Path:
   return smv_path.with_suffix(".fds")

# -----------------------------------------------------------------------------
# Run
# -----------------------------------------------------------------------------

def run(chid: str = "simple_test",
        smv: Optional[str] = None,
        smv_dir: Optional[str] = None,
        make_open_outline: bool = True,
        make_room_outline: bool = True):
   smv_path = resolve_smv_path(chid=chid, smv=smv, smv_dir=smv_dir)
   if not smv_path.exists():
      print(f"Current working directory: {Path.cwd()}")
      print(f"Text editor script path: {get_text_editor_script_path()}")
      raise FileNotFoundError(f"Could not find SMV file: {smv_path}")

   fds_info = None
   fds_path = resolve_fds_path(smv_path)
   if fds_path.exists():
      fds_info = parse_fds(fds_path)
      print(f"Read FDS metadata from: {fds_path}")
   else:
      print(f"No sibling FDS file found at: {fds_path}")

   data = parse_smv(smv_path, fds_info=fds_info)
   build_scene(
      data,
      make_open_outline=make_open_outline,
      make_room_outline=make_room_outline,
   )

# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def parse_args(argv: List[str]) -> argparse.Namespace:
   if "--" in argv:
      argv = argv[argv.index("--") + 1:]
   else:
      argv = []

   parser = argparse.ArgumentParser()
   parser.add_argument("--chid", default="simple_test", help="Case CHID; script looks for <chid>.smv unless --smv is given")
   parser.add_argument("--smv", default=None, help="Explicit path to .smv file")
   parser.add_argument("--smv-dir", default=None, help="Directory containing <chid>.smv")
   parser.add_argument("--no-open-outline", action="store_true", help="Do not draw magenta outline around OPEN vents")
   parser.add_argument("--no-room-outline", action="store_true", help="Do not draw black room outline from OUTLINE block")
   return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None):
   if argv is None:
      argv = sys.argv

   args = parse_args(argv)
   run(
      chid=args.chid,
      smv=args.smv,
      smv_dir=args.smv_dir,
      make_open_outline=not args.no_open_outline,
      make_room_outline=not args.no_room_outline,
   )


if __name__ == "__main__":
   has_cli_args = "--" in sys.argv and len(sys.argv[sys.argv.index("--") + 1:]) > 0

   if has_cli_args:
      main(sys.argv)
   elif BLENDER_AUTORUN:
      run(
         chid=BLENDER_CHID,
         smv=BLENDER_SMV_PATH,
         smv_dir=BLENDER_SMV_DIR,
         make_open_outline=BLENDER_OPEN_OUTLINE,
         make_room_outline=BLENDER_ROOM_OUTLINE,
      )
