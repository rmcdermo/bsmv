#!/usr/bin/env python3
"""
bsmv_make_animation.py

Serial, one-file animation renderer for bsmv/Blender VDB scenes.

Run this with system Python from a terminal. It launches a fresh Blender process
for each frame, waits for that frame to finish, then launches the next frame.
This is slower than parallel rendering but is much more stable for heavy VDB
volume scenes.

Default behavior:
  - one Blender process at a time
  - overwrite existing PNGs
  - Ctrl-C terminates the active Blender process
  - no separate bsmv_render_single_frame.py is needed

Example:
  python3 bsmv_make_animation.py \
    --blend FM_15cm_Burner_C2H4_16p8_5mm.blend \
    --start-frame 1 \
    --end-frame 191 \
    --step 1 \
    --outdir animation
"""

from __future__ import annotations

import argparse
import gc
import os
import platform
import signal
import subprocess
import sys
from pathlib import Path


# -----------------------------------------------------------------------------
# Shared utilities
# -----------------------------------------------------------------------------

def argv_after_double_dash() -> list[str]:
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1 :]
    return sys.argv[1:]


def is_single_frame_mode() -> bool:
    return "--bsmv-single-frame" in argv_after_double_dash()


def default_blender_bin() -> Path:
    system = platform.system()

    if system == "Darwin":
        return Path("/Applications/Blender.app/Contents/MacOS/Blender")

    if system == "Windows":
        candidates = [
            Path(r"C:\Program Files\Blender Foundation\Blender\blender.exe"),
            Path(r"C:\Program Files\Blender Foundation\Blender 4.3\blender.exe"),
            Path(r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe"),
        ]
        for c in candidates:
            if c.exists():
                return c
        return candidates[0]

    return Path("blender")


def positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def require_file(path: Path, label: str) -> None:
    if not path.exists():
        raise SystemExit(f"ERROR: {label} not found: {path}")


def png_path(outdir: Path, prefix: str, frame: int) -> Path:
    return outdir / f"{prefix}_{frame:04d}.png"


# -----------------------------------------------------------------------------
# Outer mode: launched by system Python
# -----------------------------------------------------------------------------

_ACTIVE_PROC: subprocess.Popen | None = None


def terminate_active_child(reason: str = "shutdown") -> None:
    global _ACTIVE_PROC

    proc = _ACTIVE_PROC
    if proc is None or proc.poll() is not None:
        return

    print(f"\n[anim] terminating active Blender process due to {reason}...", flush=True)

    try:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            proc.terminate()
    except Exception:
        pass

    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            if proc.poll() is None:
                if os.name == "posix":
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    proc.kill()
        except Exception:
            pass


def signal_handler(signum, frame) -> None:
    signame = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    print(f"\n[anim] caught {signame}; stopping...", flush=True)
    terminate_active_child(reason=signame)
    raise KeyboardInterrupt


def parse_outer_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Render a Blender animation serially, one fresh Blender process per frame.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    p.add_argument("--blend", required=True, help="Path to the .blend file")
    p.add_argument("--start-frame", "--start", type=int, default=1, help="First frame")
    p.add_argument("--end-frame", "--end", type=int, default=191, help="Last frame, inclusive")
    p.add_argument("--step", type=positive_int, default=1, help="Frame step")

    p.add_argument("--outdir", default="animation", help="Output PNG directory")
    p.add_argument("--prefix", default=None, help="Output filename prefix; default is blend filename stem")

    p.add_argument("--blender-bin", default=str(default_blender_bin()), help="Path to Blender executable")
    p.add_argument("--loader", default=None, help="Optional explicit path to load_vdb_sparse_to_blender.py")

    skip = p.add_mutually_exclusive_group()
    skip.add_argument("--skip-existing", action="store_true", default=False, help="Skip frames whose PNG already exists")
    skip.add_argument("--overwrite-existing", dest="skip_existing", action="store_false", help="Overwrite existing PNGs")
    p.set_defaults(skip_existing=False)

    p.add_argument("--samples", type=int, default=None, help="Override Cycles samples")
    p.add_argument("--render-engine", default=None, help="Override render engine, e.g. CYCLES")
    p.add_argument("--verbose", action="store_true", help="Verbose one-frame render logging")
    p.add_argument("--dry-run", action="store_true", help="Print Blender commands without running them")

    return p.parse_args(argv_after_double_dash())


def build_blender_cmd(
    blender_bin: Path,
    this_script: Path,
    blend: Path,
    frame: int,
    outdir: Path,
    prefix: str,
    loader: str | None,
    render_engine: str | None,
    samples: int | None,
    verbose: bool,
) -> list[str]:
    cmd = [
        str(blender_bin),
        "-b",
        str(blend),
        "-P",
        str(this_script),
        "--",
        "--bsmv-single-frame",
        "--frame",
        str(frame),
        "--outdir",
        str(outdir),
        "--prefix",
        prefix,
    ]

    if loader:
        cmd += ["--loader", str(Path(loader).expanduser().resolve())]
    if render_engine:
        cmd += ["--render-engine", render_engine]
    if samples is not None:
        cmd += ["--samples", str(samples)]
    if verbose:
        cmd += ["--verbose"]

    return cmd


def run_blender_frame(cmd: list[str]) -> int:
    global _ACTIVE_PROC

    _ACTIVE_PROC = subprocess.Popen(
        cmd,
        start_new_session=(os.name == "posix"),
    )
    try:
        return _ACTIVE_PROC.wait()
    finally:
        _ACTIVE_PROC = None


def run_outer() -> int:
    signal.signal(signal.SIGINT, signal_handler)
    try:
        signal.signal(signal.SIGTERM, signal_handler)
    except Exception:
        pass

    args = parse_outer_args()

    blend = Path(args.blend).expanduser().resolve()
    this_script = Path(__file__).expanduser().resolve()
    blender_bin = Path(args.blender_bin).expanduser()

    require_file(blend, "blend file")
    require_file(this_script, "this script")

    if str(blender_bin) != "blender":
        require_file(blender_bin, "Blender executable")

    if args.end_frame < args.start_frame:
        raise SystemExit("ERROR: --end-frame must be >= --start-frame")

    outdir = Path(args.outdir).expanduser()
    if not outdir.is_absolute():
        outdir = (blend.parent / outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    prefix = args.prefix if args.prefix else blend.stem
    frames = list(range(args.start_frame, args.end_frame + 1, args.step))

    print(f"[anim] Blender:       {blender_bin}", flush=True)
    print(f"[anim] Blend:         {blend}", flush=True)
    print(f"[anim] Driver:        {this_script}", flush=True)
    print(f"[anim] Loader:        {args.loader if args.loader else 'auto'}", flush=True)
    print(f"[anim] Outdir:        {outdir}", flush=True)
    print(f"[anim] Frames:        {args.start_frame} .. {args.end_frame} step {args.step}", flush=True)
    print(f"[anim] Prefix:        {prefix}", flush=True)
    print(f"[anim] Skip existing: {args.skip_existing}", flush=True)
    print(f"[anim] Mode:          serial, one fresh Blender process per frame", flush=True)

    completed = 0
    skipped = 0
    total = len(frames)

    try:
        for index, frame in enumerate(frames, start=1):
            png = png_path(outdir, prefix, frame)

            if args.skip_existing and png.exists():
                print(f"[anim] skip existing frame {frame}: {png}", flush=True)
                skipped += 1
                continue

            cmd = build_blender_cmd(
                blender_bin=blender_bin,
                this_script=this_script,
                blend=blend,
                frame=frame,
                outdir=outdir,
                prefix=prefix,
                loader=args.loader,
                render_engine=args.render_engine,
                samples=args.samples,
                verbose=args.verbose,
            )

            print(f"[anim] rendering frame {frame} ({index}/{total})", flush=True)

            if args.dry_run:
                print("[anim] dry run:", " ".join(cmd), flush=True)
                completed += 1
                continue

            rc = run_blender_frame(cmd)

            if rc != 0:
                print(f"[anim] ERROR: Blender failed on frame {frame} with return code {rc}", file=sys.stderr, flush=True)
                print("[anim] Completed PNGs remain on disk. Re-run with --skip-existing to resume.", file=sys.stderr, flush=True)
                return rc

            completed += 1
            print(f"[anim] finished frame {frame} ({completed} rendered, {skipped} skipped)", flush=True)

    except KeyboardInterrupt:
        terminate_active_child(reason="Ctrl-C")
        print("[anim] interrupted by user", flush=True)
        return 130
    finally:
        terminate_active_child(reason="shutdown")

    print(f"[anim] complete: rendered={completed}, skipped={skipped}, outdir={outdir}", flush=True)
    return 0


# -----------------------------------------------------------------------------
# Inner mode: launched by Blender with -P this_script -- --bsmv-single-frame ...
# -----------------------------------------------------------------------------

def parse_inner_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Internal one-frame Blender render mode.")

    p.add_argument("--bsmv-single-frame", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--frame", type=int, required=True, help="Frame number to render")
    p.add_argument("--outdir", type=str, default="animation", help="Output directory for PNGs")
    p.add_argument("--prefix", type=str, default=None, help="Filename prefix")
    p.add_argument("--loader", type=str, default=None, help="Path to load_vdb_sparse_to_blender.py")
    p.add_argument("--render-engine", type=str, default=None, help="Optional render engine override")
    p.add_argument("--samples", type=int, default=None, help="Optional Cycles samples override")
    p.add_argument("--verbose", action="store_true")

    return p.parse_args(argv_after_double_dash())


def blender_find_loader(explicit: str | None) -> Path:
    import bpy

    candidates: list[Path] = []

    if explicit:
        candidates.append(Path(explicit).expanduser())

    candidates.append(Path(__file__).resolve().parent / "load_vdb_sparse_to_blender.py")

    if bpy.data.filepath:
        candidates.append(Path(bpy.data.filepath).resolve().parent / "load_vdb_sparse_to_blender.py")

    candidates.append(Path.cwd() / "load_vdb_sparse_to_blender.py")

    for c in candidates:
        try:
            c = c.resolve()
        except Exception:
            pass
        if c.exists():
            return c

    searched = "\n".join(str(c) for c in candidates)
    raise FileNotFoundError(f"Could not locate load_vdb_sparse_to_blender.py. Searched:\n{searched}")


def free_blender_render_memory(verbose: bool = False) -> None:
    import bpy

    for name in ("Render Result", "Viewer Node", "Composite"):
        img = bpy.data.images.get(name)
        if img is not None:
            try:
                img.buffers_free()
                if verbose:
                    print(f"[mem] freed image buffers for {name}", flush=True)
            except Exception:
                pass

    try:
        bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
        if verbose:
            print("[mem] ran orphans_purge", flush=True)
    except Exception:
        pass

    gc.collect()


def run_inner_blender() -> int:
    import runpy
    import bpy

    args = parse_inner_args()
    loader = blender_find_loader(args.loader)

    if args.verbose:
        print(f"[render] opened blend: {bpy.data.filepath}", flush=True)
        print(f"[render] using loader: {loader}", flush=True)

    runpy.run_path(str(loader), run_name="__main__")

    scene = bpy.context.scene
    view_layer = bpy.context.view_layer

    if args.render_engine:
        scene.render.engine = args.render_engine

    if args.samples is not None and scene.render.engine == "CYCLES":
        scene.cycles.samples = args.samples
        scene.cycles.preview_samples = args.samples

    outdir = Path(args.outdir).expanduser()
    if not outdir.is_absolute():
        blend_dir = Path(bpy.data.filepath).resolve().parent if bpy.data.filepath else Path.cwd()
        outdir = (blend_dir / outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    prefix = args.prefix
    if not prefix:
        try:
            import bsmv_blender_config as cfg  # type: ignore
            prefix = getattr(cfg, "BLENDER_CHID", None)
        except Exception:
            prefix = None
    if not prefix:
        prefix = Path(bpy.data.filepath).stem if bpy.data.filepath else "frame"

    scene.frame_set(args.frame)
    view_layer.update()

    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.use_file_extension = True
    scene.render.use_overwrite = True
    scene.render.filepath = str(outdir / f"{prefix}_{args.frame:04d}")

    print(f"[render] frame={args.frame} -> {scene.render.filepath}.png", flush=True)
    bpy.ops.render.render(write_still=True)
    free_blender_render_memory(verbose=args.verbose)
    print(f"[render] done frame={args.frame}", flush=True)
    return 0


def main() -> int:
    if is_single_frame_mode():
        return run_inner_blender()
    return run_outer()


if __name__ == "__main__":
    raise SystemExit(main())
