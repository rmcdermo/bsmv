#!/usr/bin/env python3
"""
bsmv_make_animation.py

Parallel-capable Blender animation renderer for bsmv VDB scenes.

Design goals:
  * stable serial mode: --jobs 1 --frames-per-process 1
  * real parallel mode: limit each Blender process with --blender-threads
  * cluster-friendly: process pool, per-chunk logs, clean Ctrl-C kill
  * no second Python file: this script calls itself inside Blender

Recommended Linux cluster test:
  python3 bsmv_make_animation.py \
    --blend case.blend \
    --start-frame 1 \
    --end-frame 191 \
    --outdir animation \
    --jobs 8 \
    --blender-threads 1 \
    --frames-per-process 1 \
    --skip-existing

For better startup amortization on a machine with enough memory:
  --frames-per-process 2
or
  --frames-per-process 4

Notes:
  --jobs controls number of simultaneous Blender processes.
  --blender-threads controls Blender/Cycles CPU threads per process.
  Use --frames-per-process 1 for maximum memory isolation.
"""

from __future__ import annotations

import argparse
import gc
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path


# -----------------------------------------------------------------------------
# General helpers
# -----------------------------------------------------------------------------

def argv_after_double_dash() -> list[str]:
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1 :]
    return sys.argv[1:]


def is_inner_mode() -> bool:
    return "--bsmv-render-frames" in argv_after_double_dash()


def default_blender_bin() -> str:
    system = platform.system()
    if system == "Darwin":
        return "/Applications/Blender.app/Contents/MacOS/Blender"
    if system == "Windows":
        candidates = [
            r"C:\Program Files\Blender Foundation\Blender\blender.exe",
            r"C:\Program Files\Blender Foundation\Blender 4.3\blender.exe",
            r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe",
        ]
        for c in candidates:
            if Path(c).exists():
                return c
        return candidates[0]
    return "blender"


def positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def require_file(path: Path, label: str) -> None:
    if not path.exists():
        raise SystemExit(f"ERROR: {label} not found: {path}")


def frame_png(outdir: Path, prefix: str, frame: int) -> Path:
    return outdir / f"{prefix}_{frame:04d}.png"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(max(0, seconds))
    mm, ss = divmod(seconds, 60)
    hh, mm = divmod(mm, 60)
    if hh:
        return f"{hh:d}:{mm:02d}:{ss:02d}"
    return f"{mm:02d}:{ss:02d}"


def tail_last_line(path: Path, max_bytes: int = 12000) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes), 0)
            text = f.read().decode("utf-8", errors="replace")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return ""
        for ln in reversed(lines):
            low = ln.lower()
            if (
                "sample" in low
                or "remaining" in low
                or "rendering" in low
                or "time:" in low
                or ln.startswith("[render]")
                or ln.startswith("[inner]")
            ):
                return ln[-160:]
        return lines[-1][-160:]
    except Exception:
        return ""


def parse_frame_list(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return out


def chunks(items: list[int], n: int) -> list[list[int]]:
    n = max(1, int(n))
    return [items[i:i+n] for i in range(0, len(items), n)]


# -----------------------------------------------------------------------------
# Outer process pool mode
# -----------------------------------------------------------------------------

ACTIVE: dict[subprocess.Popen, dict] = {}
STOP_REQUESTED = False


def terminate_active(reason: str = "shutdown") -> None:
    procs = list(ACTIVE.keys())
    if procs:
        print(f"[anim] terminating {len(procs)} active Blender process(es): {reason}", flush=True)

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

    deadline = time.time() + 5.0
    for proc in procs:
        try:
            proc.wait(timeout=max(0.0, deadline - time.time()))
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


def handle_signal(signum, frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    try:
        signame = signal.Signals(signum).name
    except Exception:
        signame = str(signum)
    print(f"\n[anim] caught {signame}", flush=True)
    terminate_active(signame)
    raise KeyboardInterrupt


def parse_outer_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Render Blender frames using a controlled pool of Blender subprocesses.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    p.add_argument("--blend", required=True, help="Path to .blend file")
    p.add_argument("--start-frame", "--start", type=int, default=1, help="First frame")
    p.add_argument("--end-frame", "--end", type=int, default=191, help="Last frame, inclusive")
    p.add_argument("--step", type=positive_int, default=1, help="Frame step")
    p.add_argument("--frames", default=None, help="Explicit comma-separated frame list, overrides start/end/step")

    p.add_argument("--outdir", default="animation", help="Output PNG directory")
    p.add_argument("--prefix", default=None, help="PNG filename prefix; default is blend filename stem")
    p.add_argument("--logdir", default=None, help="Log directory; default is <outdir>/logs")

    p.add_argument("--blender-bin", default=default_blender_bin(), help="Blender executable")
    p.add_argument("--loader", default=None, help="Explicit load_vdb_sparse_to_blender.py path")

    p.add_argument("--jobs", "-j", type=positive_int, default=1, help="Number of simultaneous Blender processes")
    p.add_argument("--blender-threads", type=positive_int, default=1,
                   help="Threads per Blender process; passed as Blender -t and thread env vars")
    p.add_argument("--frames-per-process", type=positive_int, default=1,
                   help="Frames each Blender process renders before exiting")

    skip = p.add_mutually_exclusive_group()
    skip.add_argument("--skip-existing", action="store_true", default=False, help="Skip frames whose PNG already exists")
    skip.add_argument("--overwrite-existing", dest="skip_existing", action="store_false", help="Overwrite existing PNGs")
    p.set_defaults(skip_existing=False)

    p.add_argument("--samples", type=int, default=None, help="Override Cycles samples")
    p.add_argument("--render-engine", default=None, help="Override render engine, e.g. CYCLES")
    p.add_argument("--heartbeat-sec", type=float, default=10.0, help="Seconds between status lines")
    p.add_argument("--verbose", action="store_true", help="Verbose inner Blender output")
    p.add_argument("--dry-run", action="store_true", help="Print commands without rendering")

    return p.parse_args(argv_after_double_dash())


def make_child_env(threads: int) -> dict[str, str]:
    env = os.environ.copy()
    # Avoid oversubscription. This is the main reason the first parallel version
    # behaved almost serially: every Blender child was allowed to use all cores.
    for name in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "TBB_NUM_THREADS",
    ):
        env[name] = str(threads)
    return env


def build_cmd(
    blender_bin: str,
    this_script: Path,
    blend: Path,
    frame_chunk: list[int],
    outdir: Path,
    prefix: str,
    loader: str | None,
    render_engine: str | None,
    samples: int | None,
    verbose: bool,
    blender_threads: int,
) -> list[str]:
    cmd = [
        blender_bin,
        "-b", str(blend),
        "-t", str(blender_threads),
        "-P", str(this_script),
        "--",
        "--bsmv-render-frames", ",".join(str(f) for f in frame_chunk),
        "--outdir", str(outdir),
        "--prefix", prefix,
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


def start_chunk(
    args: argparse.Namespace,
    this_script: Path,
    blend: Path,
    outdir: Path,
    prefix: str,
    logdir: Path,
    frame_chunk: list[int],
    chunk_index: int,
) -> subprocess.Popen:
    cmd = build_cmd(
        blender_bin=args.blender_bin,
        this_script=this_script,
        blend=blend,
        frame_chunk=frame_chunk,
        outdir=outdir,
        prefix=prefix,
        loader=args.loader,
        render_engine=args.render_engine,
        samples=args.samples,
        verbose=args.verbose,
        blender_threads=args.blender_threads,
    )

    log_name = f"{prefix}_chunk_{chunk_index:04d}_frames_{frame_chunk[0]:04d}-{frame_chunk[-1]:04d}.log"
    log_path = logdir / log_name

    if args.dry_run:
        print("[anim] dry run:", " ".join(cmd))
        class DummyProc:
            pid = -1
            def poll(self): return 0
            def wait(self, timeout=None): return 0
        proc = DummyProc()  # type: ignore
    else:
        log = log_path.open("w", encoding="utf-8", errors="replace")
        log.write("[cmd] " + " ".join(cmd) + "\n")
        log.flush()

        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=make_child_env(args.blender_threads),
            start_new_session=(os.name == "posix"),
        )
        # Keep log handle alive by attaching it; close after process ends.
        proc._bsmv_log_handle = log  # type: ignore[attr-defined]

    ACTIVE[proc] = {
        "frames": frame_chunk,
        "chunk_index": chunk_index,
        "log_path": log_path,
        "start_time": time.time(),
    }

    print(
        f"[anim] start chunk {chunk_index:04d} pid={proc.pid} "
        f"frames={frame_chunk[0]}..{frame_chunk[-1]} log={log_path}",
        flush=True,
    )
    return proc


def finish_proc(proc: subprocess.Popen) -> tuple[int, dict]:
    rc = proc.poll()
    if rc is None:
        rc = proc.wait()

    try:
        log = getattr(proc, "_bsmv_log_handle", None)
        if log is not None:
            log.close()
    except Exception:
        pass

    meta = ACTIVE.pop(proc)
    return int(rc), meta


def print_status(total_frames: int, completed_frames: int, started_frames: int, t0: float) -> None:
    elapsed = time.time() - t0
    rate = completed_frames / elapsed if elapsed > 0 else 0.0
    remaining = max(0, total_frames - completed_frames)
    eta = remaining / rate if rate > 1.0e-12 else None

    print(
        f"[status] frames done={completed_frames}/{total_frames} started={started_frames} "
        f"active_chunks={len(ACTIVE)} rate={rate:.3f} frame/s ETA={fmt_duration(eta)}",
        flush=True,
    )

    for proc, meta in sorted(ACTIVE.items(), key=lambda kv: kv[1]["frames"][0]):
        age = fmt_duration(time.time() - meta["start_time"])
        frames = meta["frames"]
        tail = tail_last_line(meta["log_path"])
        msg = f"[status]   chunk={meta['chunk_index']:04d} pid={proc.pid} frames={frames[0]}..{frames[-1]} age={age}"
        if tail:
            msg += f" | {tail}"
        print(msg, flush=True)


def run_outer() -> int:
    signal.signal(signal.SIGINT, handle_signal)
    try:
        signal.signal(signal.SIGTERM, handle_signal)
    except Exception:
        pass

    args = parse_outer_args()

    blend = Path(args.blend).expanduser().resolve()
    this_script = Path(__file__).expanduser().resolve()
    require_file(blend, "blend file")
    require_file(this_script, "this script")
    if args.blender_bin != "blender":
        require_file(Path(args.blender_bin).expanduser(), "Blender executable")

    if args.frames:
        all_frames = parse_frame_list(args.frames)
    else:
        if args.end_frame < args.start_frame:
            raise SystemExit("ERROR: --end-frame must be >= --start-frame")
        all_frames = list(range(args.start_frame, args.end_frame + 1, args.step))

    outdir = Path(args.outdir).expanduser()
    if not outdir.is_absolute():
        outdir = (blend.parent / outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    prefix = args.prefix or blend.stem

    logdir = Path(args.logdir).expanduser() if args.logdir else outdir / "logs"
    if not logdir.is_absolute():
        logdir = (blend.parent / logdir).resolve()
    logdir.mkdir(parents=True, exist_ok=True)

    if args.skip_existing:
        render_frames = []
        skipped = 0
        for f in all_frames:
            if frame_png(outdir, prefix, f).exists():
                skipped += 1
            else:
                render_frames.append(f)
    else:
        render_frames = list(all_frames)
        skipped = 0

    frame_chunks = chunks(render_frames, args.frames_per_process)

    print(f"[anim] Blender:             {args.blender_bin}", flush=True)
    print(f"[anim] Blend:               {blend}", flush=True)
    print(f"[anim] Driver:              {this_script}", flush=True)
    print(f"[anim] Loader:              {args.loader if args.loader else 'auto'}", flush=True)
    print(f"[anim] Outdir:              {outdir}", flush=True)
    print(f"[anim] Logdir:              {logdir}", flush=True)
    print(f"[anim] Frames requested:    {len(all_frames)}", flush=True)
    print(f"[anim] Frames to render:    {len(render_frames)}", flush=True)
    print(f"[anim] Chunks to render:    {len(frame_chunks)}", flush=True)
    print(f"[anim] Prefix:              {prefix}", flush=True)
    print(f"[anim] Skip existing:       {args.skip_existing} skipped={skipped}", flush=True)
    print(f"[anim] Jobs:                {args.jobs}", flush=True)
    print(f"[anim] Blender threads/job: {args.blender_threads}", flush=True)
    print(f"[anim] Frames/process:      {args.frames_per_process}", flush=True)

    next_chunk = 0
    completed_frames = 0
    started_frames = 0
    t0 = time.time()
    last_status = 0.0

    try:
        while completed_frames < len(render_frames):
            if STOP_REQUESTED:
                break

            # Fill process slots.
            while len(ACTIVE) < args.jobs and next_chunk < len(frame_chunks):
                fc = frame_chunks[next_chunk]
                next_chunk += 1
                started_frames += len(fc)
                start_chunk(args, this_script, blend, outdir, prefix, logdir, fc, next_chunk)

            # Poll for finished chunks.
            finished: list[subprocess.Popen] = []
            for proc in list(ACTIVE.keys()):
                if proc.poll() is not None:
                    finished.append(proc)

            if not finished:
                now = time.time()
                if now - last_status >= args.heartbeat_sec:
                    print_status(len(render_frames), completed_frames, started_frames, t0)
                    last_status = now
                time.sleep(0.5)
                continue

            for proc in finished:
                rc, meta = finish_proc(proc)
                frames = meta["frames"]
                if rc != 0:
                    print(
                        f"[anim] ERROR: chunk {meta['chunk_index']:04d} failed rc={rc} "
                        f"frames={frames[0]}..{frames[-1]} log={meta['log_path']}",
                        file=sys.stderr,
                        flush=True,
                    )
                    terminate_active("chunk failure")
                    return rc

                completed_frames += len(frames)
                print(
                    f"[anim] finished chunk {meta['chunk_index']:04d} "
                    f"frames={frames[0]}..{frames[-1]} "
                    f"({completed_frames}/{len(render_frames)} frames complete)",
                    flush=True,
                )

        return 0

    except KeyboardInterrupt:
        terminate_active("Ctrl-C")
        print("[anim] interrupted by user", flush=True)
        return 130
    finally:
        terminate_active("shutdown")
        print(
            f"[anim] complete or stopped: rendered_frames={completed_frames}, "
            f"skipped={skipped}, outdir={outdir}",
            flush=True,
        )


# -----------------------------------------------------------------------------
# Inner Blender mode
# -----------------------------------------------------------------------------

def parse_inner_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Internal Blender frame renderer")
    p.add_argument("--bsmv-render-frames", required=True, help="Comma-separated frame list")
    p.add_argument("--outdir", required=True, help="Output PNG directory")
    p.add_argument("--prefix", required=True, help="PNG prefix")
    p.add_argument("--loader", default=None, help="load_vdb_sparse_to_blender.py")
    p.add_argument("--render-engine", default=None, help="Render engine override")
    p.add_argument("--samples", type=int, default=None, help="Cycles samples override")
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


def run_inner() -> int:
    import bpy
    import runpy

    args = parse_inner_args()
    frames = parse_frame_list(args.bsmv_render_frames)
    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    loader = blender_find_loader(args.loader)

    print(f"[inner] frames={frames}", flush=True)
    print(f"[inner] loader={loader}", flush=True)

    # Load the bsmv scene once per Blender process/chunk.
    runpy.run_path(str(loader), run_name="__main__")

    scene = bpy.context.scene
    view_layer = bpy.context.view_layer

    if args.render_engine:
        scene.render.engine = args.render_engine

    if args.samples is not None and scene.render.engine == "CYCLES":
        scene.cycles.samples = args.samples
        scene.cycles.preview_samples = args.samples

    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.use_file_extension = True
    scene.render.use_overwrite = True

    for frame in frames:
        scene.frame_set(frame)
        view_layer.update()

        scene.render.filepath = str(outdir / f"{args.prefix}_{frame:04d}")
        print(f"[render] frame={frame} -> {scene.render.filepath}.png", flush=True)

        t0 = time.time()
        bpy.ops.render.render(write_still=True)
        print(f"[render] done frame={frame} elapsed={fmt_duration(time.time() - t0)}", flush=True)

        free_blender_render_memory(verbose=args.verbose)

    return 0


def main() -> int:
    if is_inner_mode():
        return run_inner()
    return run_outer()


if __name__ == "__main__":
    raise SystemExit(main())
