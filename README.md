# bsmv

`bsmv` is the first compiled C++ helper for the BlenderSMV workflow.

This initial archival source focuses on the same job as the Python exporters:

- read an FDS result directory such as `Phoenix01_Out`
- use the `.smv` file to find SMOKE3D entries and mesh grids
- stream `.s3d` files frame-by-frame
- decode the 8-bit smoke payload
- write per-mesh OpenVDB sequences and JSON manifests into `vdb_sequence`

## Current scope

This first compiled version writes per-mesh VDB sequences for:

- `EFFECTIVE FLAME TEMPERATURE` (stored as `temperature`, degC above ambient)
- `SOOT DENSITY` (stored as `density`)

The code is intentionally serial and simple. The hot loop is already compiled and frame-streaming.

## Build

```bash
cmake -S . -B build
cmake --build build -j
```

This project expects an OpenVDB installation discoverable by CMake.  Clone the repo `git@github.com:AcademySoftwareFoundation/openvdb.git` and follow the build instructions.

## Example

```bash
./build/bsmv \
  --chid Phoenix01 \
  --smv /full/path/to/Phoenix01.smv \
  --result-dir ./Phoenix01_Out \
  --out-dir ./vdb_sequence \
  --progress-every 10
```

Subset of meshes:

```bash
./build/bsmv \
  --chid Phoenix01 \
  --smv /full/path/to/Phoenix01.smv \
  --result-dir ./Phoenix01_Out \
  --out-dir ./vdb_sequence \
  --mesh-ids 1,2,7 \
  --start 0 \
  --stop 1000 \
  --stride 5
```

## Notes

- The decoder uses the same run-length logic as pyfdstools for `.s3d` smoke frames.
- The writer keeps unit voxel spacing in the VDB file and writes the physical origin/spacing to the JSON manifest, matching the earlier Python pipeline.
- This is the starting point for later merged/coarse/summary modes.
