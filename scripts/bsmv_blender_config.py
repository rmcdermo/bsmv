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
CLEAR_SCENE = True

LOAD_GEOMETRY = True
LOAD_SCENE_BOX = True
LOAD_VDB = True

# VDB selection.
VDB_SORT_MODE = "mesh"          # "mesh" or "signal"
VDB_START_INDEX = 0
VDB_MAX_MESHES = None           # None = all
VDB_MANIFEST_STRIDE = 1

# Sparse thresholded VDB frame policy:
#   "nearest"       = use nearest available VDB frame, good for visual continuity/debugging
#   "exact_or_hide" = hide mesh if exact frame is missing
#   "hold_previous" = use most recent prior available frame
VDB_FRAME_POLICY = "exact_or_hide"

# Correct physical placement from bsmv manifest.
APPLY_MANIFEST_ORIGIN = True
APPLY_MANIFEST_SPACING = True

# Render/view.
SET_RENDERED_VIEW = False
SET_CYCLES = True
SCENE_FPS = 10

# Cycles volume stepping. Smaller values reveal finer detail but render slower.
CYCLES_VOLUME_STEP_RATE = 0.005
CYCLES_VOLUME_PREVIEW_STEP_RATE = 0.005

# Volume material transfer-function settings.
USE_SMOKE = True
SMOKE_DENSITY_SCALE = 8700.0
SMOKE_COLOR = (0.035, 0.035, 0.035, 1.0)

USE_FLAME = True
FLAME_TEMP_MIN = 600.0
FLAME_TEMP_MAX = 1200.0
EMISSION_STRENGTH = 1.0

# Diagnostics.
PRINT_EVERY = 20
PRINT_FIRST_N = 10
PRINT_FRAME_HANDLER_UPDATES = False
PRINT_GRID_LOADS = False
