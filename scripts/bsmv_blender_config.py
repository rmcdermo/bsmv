# bsmv_blender_config.py
#
# Case-specific settings for load_vdb_sparse_to_blender.py.
# Keep this file; replace load_vdb_sparse_to_blender.py when the algorithm changes.

BLENDER_AUTORUN = True
BLENDER_CHID = "FM_15cm_Burner_C2H4_16p8_5mm"

# Paths visible from Blender on the Mac.
BLENDER_GEOM_DIR = "/Users/rmcdermo/spark_home/rmcdermo/GitHub/firemodels/fds/Validation/FM_Burner/Blender_Test/geometry_16p8_5mm"
BLENDER_VDB_DIR  = "/Users/rmcdermo/spark_home/rmcdermo/GitHub/firemodels/fds/Validation/FM_Burner/Blender_Test/vdb_sequence_16p8_5mm"

# This loader is intended to give a clean, complete scene every run.
CLEAN_SCENE = True

LOAD_GEOMETRY  = True
LOAD_SCENE_BOX = True
LOAD_VDB       = True

# --- Flame support / mesh filtering ---
# Bias the sparse loader toward hot cells so the flame gets narrower and less blobby.
SIGNAL_REQUIRE_DATA   = True
SIGNAL_TEMP_MIN       = 500.0
SIGNAL_DENSITY_MIN    = 0.0
SIGNAL_TEMP_WEIGHT    = 1.0
SIGNAL_DENSITY_WEIGHT = 0.10

# VDB selection.
VDB_SORT_MODE = "mesh"          # "mesh" or "signal"
VDB_START_INDEX = 0
VDB_MAX_MESHES = None           # None = all
VDB_MANIFEST_STRIDE = 1

# Sparse thresholded VDB frame policy:
#   "nearest"       = use nearest available VDB frame, good for visual continuity/debugging
#   "exact_or_hide" = hide mesh if exact frame is missing
#   "hold_previous" = use most recent prior available frame
VDB_FRAME_POLICY = "nearest"

# Correct physical placement from bsmv manifest.
APPLY_MANIFEST_ORIGIN = True
APPLY_MANIFEST_SPACING = True

# Render/view.
SET_RENDERED_VIEW = False
SET_CYCLES = True
SCENE_FPS = 10

# Cycles volume stepping. Smaller values reveal finer detail but render slower.
CYCLES_VOLUME_STEP_RATE = 0.001
CYCLES_VOLUME_PREVIEW_STEP_RATE = 0.001

# Volume material transfer-function settings.
# For Material Preview / quick scene checks, keep smoke off so the gray density
# cloud does not hide the flame. Turn USE_SMOKE back on later for smoke renders.
USE_SMOKE = True
SMOKE_DENSITY_SCALE = 100.0
SMOKE_COLOR = (0.035, 0.035, 0.035, 1.0)

USE_FLAME = True
FLAME_TEMP_MIN = 600.0
FLAME_TEMP_MAX = 1200.0
EMISSION_STRENGTH = 3.0

# Lighting hooks
ADD_BASIC_LIGHTING = True
SUN_ENERGY = 3.0
WORLD_COLOR = (0.8, 0.8, 0.8)

# Camera knobs
ADD_BASIC_CAMERA = True
CAMERA_LENS_MM = 35.0
CAMERA_DISTANCE_MULTIPLIER = 1.75
CAMERA_DIRECTION = (-1.25, -2.40, 1.15)
CAMERA_TARGET_Z_OFFSET = 0.0
CAMERA_CLIP_START = 0.001
CAMERA_CLIP_END = 10000.0
VIEW_CLIP_START = 0.001
VIEW_CLIP_END = 10000.0

# Diagnostics.
PRINT_EVERY = 20
PRINT_FIRST_N = 10
PRINT_FRAME_HANDLER_UPDATES = False
PRINT_GRID_LOADS = False

# Scene helper drawing; these are now handled by load_vdb_sparse_to_blender.py.
DRAW_DOMAIN_BOX = True
DRAW_MESH_BOXES = False
DRAW_VENTS = True
DOMAIN_BOX_COLOR = (0.0, 0.0, 0.0, 1.0)
MESH_BOX_COLOR = (0.8, 0.8, 0.8, 0.35)
SCENE_WIRES_IN_FRONT = False

# Keep Material Preview clean. This hides camera/light helper overlays and the sun direction line.
HIDE_LIGHT_CAMERA_EXTRAS = True

# Sun placement: 30 degrees off vertical; azimuth controls side direction.
SUN_TILT_DEG = 30.0
SUN_AZIMUTH_DEG = -35.0
SUN_DISTANCE_MULTIPLIER = 1.25
