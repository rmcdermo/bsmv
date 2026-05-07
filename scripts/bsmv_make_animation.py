#!/usr/bin/env python3
"""
bsmv_make_animation.py

One-file animation renderer for bsmv/Blender VDB scenes.

Outer mode:
  Run this with system Python from a terminal. It launches Blender in batch
  mode and renders frames either serially or in parallel.

Inner mode:
  The same file is also used internally by Blender with -P. You do not normally
  call this mode yourself.

Key behaviors:
  - By default, existing PNGs are overwritten (better when the .blend camera
    or scene setup has changed).
  - Use --skip-existing if you want resume behavior.
  - Use --jobs N to render frames in parallel with up to N Blender processes.
  - A single Ctrl-C terminates all active Blender subprocesses.
  - Parent process prints a periodic heartbeat/status line while frames render.

Examples:
  python3 bsmv_make_animation.py \
    --blend FM_15cm_Burner_C2H4_16p8_5mm.blend \
    --start-frame 1 \
    --end-frame 191 \
    --step 1 \
    --outdir animation

  python3 bsmv_make_animation.py \
    --blend FM_15cm_Burner_C2H4_16p8_5mm.blend \
    --start-frame 1 \
    --end-frame 191 \
    --step 1 \
    --outdir animation \
    --jobs 8
"""

from __future__ import annotations

import argparse
import gc
import os
import platform
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path


# -----------------------------------------------------------------------------
# Shared utilities
# -----------------------------------------------------------------------------

def argv_after_double_dash() -> list[str]:
    """Return args after Blender's '--' separator, or normal script args."""
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

_ACTIVE_PROCS: set[subprocess.Popen] = set()
_ACTIVE_META: dict[subprocess.Popen, dict] = {}
_ACTIVE_LOCK = threading.Lock()
_STOP_REQUESTED = False
_STATUS_STOP = threading.Event()


def _register_proc(proc: subprocess.Popen, frame: int | None = None, log_path: Path | None = None) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS.add(proc)
        _ACTIVE_META[proc] = {
            "frame": frame,
            "log_path": log_path,
            "start_time": time.time(),
        }


def _unregister_proc(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS.discard(proc)
        _ACTIVE_META.pop(proc, None)


def _format_elapsed(seconds: float) -> str:
    seconds = int(max(0, seconds))
    mm, ss = divmod(seconds, 60)
    hh, mm = divmod(mm, 60)
    if hh:
        return f"{hh:d}:{mm:02d}:{ss:02d}"
    return f"{mm:02d}:{ss:02d}"


def _tail_last_interesting_line(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    try:
        # Only read the last chunk. Blender logs can get large.
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192), 0)
            chunk = f.read().decode("utf-8", errors="replace")
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        for ln in reversed(lines):
            low = ln.lower()
            if (
                "sample" in low
                or "rendering" in low
                or "time:" in low
                or "remaining" in low
                or ln.startswith("[render]")
            ):
                return ln[-120:]
        return lines[-1][-120:] if lines else ""
    except Exception:
        return ""


def status_heartbeat(total_frames: int, get_completed, heartbeat_sec: float = 10.0) -> None:
    """Print periodic status so a long render does not look hung."""
    heartbeat_sec = max(1.0, float(heartbeat_sec))
    while not _STATUS_STOP.wait(heartbeat_sec):
        with _ACTIVE_LOCK:
            items = list(_ACTIVE_META.items())

        if not items:
            continue

        active_bits = []
        now = time.time()
        for proc, meta in sorted(items, key=lambda x: (x[1].get("frame") or 0)):
            frame = meta.get("frame")
            elapsed = _format_elapsed(now - float(meta.get("start_time", now)))
            pid = proc.pid
            tail = _tail_last_interesting_line(meta.get("log_path"))
            bit = f"f{int(frame):04d} pid={pid} elapsed={elapsed}"
            if tail:
                bit += f" | {tail}"
            active_bits.append(bit)

        print(
            f"[status] done {get_completed()}/{total_frames}; active {len(active_bits)}: "
            + " || ".join(active_bits),
            flush=True,
        )


def terminate_all_children(reason: str = "signal") -> None:
    """Terminate all active Blender subprocesses."""
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE_PROCS)

    if procs:
        print(f"[anim] terminating {len(procs)} active Blender subprocess(es) due to {reason}...")

    for proc in procs:
        try:
            if proc.poll() is not None:
                continue

            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                proc.terminate()
        except Exception:
            pass

    # Give them a moment, then hard-kill stragglers.
    for proc in procs:
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


def _signal_handler(signum, frame) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    signame = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    print(f"\n[anim] caught {signame}; stopping...")
    terminate_all_children(reason=signame)
    raise KeyboardInterrupt


def parse_outer_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Render a Blender animation one fresh Blender process per frame.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    p.add_argument("--blend", required=True, help="Path to the .blend file")
    p.add_argument("--start-frame", "--start", type=int, default=1, help="First frame")
    p.add_argument("--end-frame", "--end", type=int, default=191, help="Last frame, inclusive")
    p.add_argument("--step", type=positive_int, default=1, help="Frame step")

    p.add_argument("--outdir", default="animation", help="Output PNG directory")
    p.add_argument("--prefix", default=None, help="Output filename prefix; default is blend filename stem")

    p.add_argument(
        "--blender-bin",
        default=str(default_blender_bin()),
        help="Path to Blender executable",
    )
    p.add_argument(
        "--loader",
        default=None,
        help="Optional explicit path to load_vdb_sparse_to_blender.py",
    )

    p.add_argument(
        "--jobs", "-j", type=positive_int, default=1,
        help="Maximum number of Blender frame renders to run in parallel",
    )

    skip = p.add_mutually_exclusive_group()
    skip.add_argument("--skip-existing", action="store_true", default=False,
                      help="Skip frames whose PNG already exists")
    skip.add_argument("--overwrite-existing", dest="skip_existing", action="store_false",
                      help="Overwrite existing PNGs (default)")
    # Ensure explicit default is overwrite.
    p.set_defaults(skip_existing=False)

    p.add_argument("--samples", type=int, default=None, help="Override Cycles samples")
    p.add_argument("--render-engine", default=None, help="Override render engine, e.g. CYCLES")
    p.add_argument("--verbose", action="store_true", help="Verbose per-frame Blender driver output")
    p.add_argument("--heartbeat-sec", type=float, default=10.0,
                   help="Seconds between parent status updates while frames are rendering")
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


def render_one_frame_subprocess(
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
) -> tuple[int, int]:
    if _STOP_REQUESTED:
        return frame, 130

    cmd = build_blender_cmd(
        blender_bin=blender_bin,
        this_script=this_script,
        blend=blend,
        frame=frame,
        outdir=outdir,
        prefix=prefix,
        loader=loader,
        render_engine=render_engine,
        samples=samples,
        verbose=verbose,
    )

    if verbose:
        print("[anim] cmd:", " ".join(cmd))

    # start_new_session=True puts each Blender child in its own process group.
    # That lets a single Ctrl-C in the parent kill all active children cleanly.
    proc = subprocess.Popen(cmd, start_new_session=(os.name == "posix"))
    _register_proc(proc, frame=frame, log_path=log_path)
    try:
        rc = proc.wait()
    finally:
        _unregister_proc(proc)

    return frame, rc


def run_outer() -> int:
    global _STOP_REQUESTED

    # Install signal handlers for clean Ctrl-C termination.
    signal.signal(signal.SIGINT, _signal_handler)
    try:
        signal.signal(signal.SIGTERM, _signal_handler)
    except Exception:
        pass

    args = parse_outer_args()

    blend = Path(args.blend).expanduser().resolve()
    this_script = Path(__file__).expanduser().resolve()
    blender_bin = Path(args.blender_bin).expanduser()

    require_file(blend, "blend file")
    require_file(this_script, "this script")

    # macOS app executable is absolute. For Linux "blender" on PATH may not exist
    # as a file; in that case let subprocess find it.
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

    if args.skip_existing:
        filtered = []
        skipped_initial = 0
        for frame in frames:
            png = png_path(outdir, prefix, frame)
            if png.exists():
                print(f"[anim] skip existing frame {frame}: {png}")
                skipped_initial += 1
            else:
                filtered.append(frame)
        frames = filtered
    else:
        skipped_initial = 0

    print(f"[anim] Blender:       {blender_bin}")
    print(f"[anim] Blend:         {blend}")
    print(f"[anim] Driver:        {this_script}")
    print(f"[anim] Loader:        {args.loader if args.loader else 'auto'}")
    print(f"[anim] Outdir:        {outdir}")
    print(f"[anim] Frames:        {args.start_frame} .. {args.end_frame} step {args.step}")
    print(f"[anim] Prefix:        {prefix}")
    print(f"[anim] Skip existing: {args.skip_existing}")
    print(f"[anim] Jobs:          {args.jobs}")

    if args.dry_run:
        for frame in frames:
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
            print("[anim] dry run:", " ".join(cmd))
        return 0

    completed = 0
    completed_lock = threading.Lock()

    def get_completed() -> int:
        with completed_lock:
            return completed

    _STATUS_STOP.clear()
    status_thread = None
    if not args.dry_run:
        status_thread = threading.Thread(
            target=status_heartbeat,
            args=(len(frames), get_completed, args.heartbeat_sec),
            daemon=True,
        )
        status_thread.start()

    try:
        if args.jobs == 1:
            for frame in frames:
                if _STOP_REQUESTED:
                    break
                print(f"[anim] rendering frame {frame}")
                _, rc = render_one_frame_subprocess(
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
                if rc != 0:
                    print(f"[anim] ERROR: Blender failed on frame {frame} with return code {rc}", file=sys.stderr)
                    print("[anim] Completed PNGs remain on disk.", file=sys.stderr)
                    return rc
                with completed_lock:
                    completed += 1
        else:
            # Parallel scheduler with bounded concurrency.
            jobs = min(args.jobs, len(frames)) if frames else 0
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                pending = {}
                frame_iter = iter(frames)

                # Prime the queue.
                for _ in range(jobs):
                    try:
                        frame = next(frame_iter)
                    except StopIteration:
                        break
                    print(f"[anim] queue frame {frame}")
                    fut = pool.submit(
                        render_one_frame_subprocess,
                        blender_bin, this_script, blend, frame, outdir, prefix,
                        args.loader, args.render_engine, args.samples, args.verbose,
                    )
                    pending[fut] = frame

                while pending:
                    done, _ = wait(pending.keys(), return_when=FIRST_COMPLETED)

                    for fut in done:
                        frame = pending.pop(fut)
                        try:
                            finished_frame, rc = fut.result()
                        except KeyboardInterrupt:
                            raise
                        except Exception as e:
                            print(f"[anim] ERROR: worker exception on frame {frame}: {e}", file=sys.stderr)
                            terminate_all_children(reason="worker exception")
                            return 1

                        if rc != 0:
                            print(
                                f"[anim] ERROR: Blender failed on frame {finished_frame} "
                                f"with return code {rc}",
                                file=sys.stderr,
                            )
                            terminate_all_children(reason=f"frame {finished_frame} failure")
                            return rc

                        print(f"[anim] finished frame {finished_frame}")
                        with completed_lock:
                            completed += 1

                        if not _STOP_REQUESTED:
                            try:
                                next_frame = next(frame_iter)
                            except StopIteration:
                                next_frame = None

                            if next_frame is not None:
                                print(f"[anim] queue frame {next_frame}")
                                fut2 = pool.submit(
                                    render_one_frame_subprocess,
                                    blender_bin, this_script, blend, next_frame, outdir, prefix,
                                    args.loader, args.render_engine, args.samples, args.verbose,
                                )
                                pending[fut2] = next_frame

    except KeyboardInterrupt:
        terminate_all_children(reason="Ctrl-C")
        print("[anim] interrupted by user")
        return 130
    finally:
        _STATUS_STOP.set()
        if status_thread is not None:
            status_thread.join(timeout=2.0)
        terminate_all_children(reason="shutdown")

    print(f"[anim] complete: rendered={get_completed()}, skipped={skipped_initial}, outdir={outdir}")
    return 0


# -----------------------------------------------------------------------------
# Inner mode: launched by Blender with -P this_script -- --bsmv-single-frame ...
# -----------------------------------------------------------------------------

def parse_inner_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Internal one-frame Blender render mode.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

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

    # Same folder as this animation script.
    candidates.append(Path(__file__).resolve().parent / "load_vdb_sparse_to_blender.py")

    # Same folder as the opened blend.
    if bpy.data.filepath:
        candidates.append(Path(bpy.data.filepath).resolve().parent / "load_vdb_sparse_to_blender.py")

    # Current working directory.
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

    # Mostly belt-and-suspenders because this process exits after the frame,
    # but it helps with driver-level cleanup before shutdown.
    for name in ("Render Result", "Viewer Node", "Composite"):
        img = bpy.data.images.get(name)
        if img is not None:
            try:
                img.buffers_free()
                if verbose:
                    print(f"[mem] freed image buffers for {name}")
            except Exception:
                pass

    try:
        bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
        if verbose:
            print("[mem] ran orphans_purge")
    except Exception:
        pass

    gc.collect()


def run_inner_blender() -> int:
    import runpy
    import bpy

    args = parse_inner_args()
    loader = blender_find_loader(args.loader)

    if args.verbose:
        print(f"[render] opened blend: {bpy.data.filepath}")
        print(f"[render] using loader: {loader}")

    # Load/refresh the bsmv scene in this fresh Blender process. The loader also
    # registers the VDB frame-change handler.
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

    # Setting the frame after the loader run triggers the bsmv frame handler.
    scene.frame_set(args.frame)
    view_layer.update()

    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.use_file_extension = True
    scene.render.use_overwrite = True
    scene.render.filepath = str(outdir / f"{prefix}_{args.frame:04d}")

    print(f"[render] frame={args.frame} -> {scene.render.filepath}.png")
    bpy.ops.render.render(write_still=True)
    free_blender_render_memory(verbose=args.verbose)
    print(f"[render] done frame={args.frame}")
    return 0


def main() -> int:
    if is_single_frame_mode():
        return run_inner_blender()
    return run_outer()


if __name__ == "__main__":
    raise SystemExit(main())
