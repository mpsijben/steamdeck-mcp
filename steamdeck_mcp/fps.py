"""Measured FPS in Game Mode, from the performance overlay (mangoapp).

gamescope hands every frame to mangoapp, which can write MangoHud's CSV log. To get one, we add a log
folder and a timed autostart to mangoapp's config file (it reloads that file on change), wait, and take the
lines out again. Steam owns that file, so only our own lines are removed afterwards, whatever Steam wrote.
"""
import csv
import os
import shutil
import subprocess
import tempfile
import time

from . import hardware


def _mangoapp_config():
    pid = subprocess.run(["pgrep", "-x", "mangoapp"], capture_output=True, text=True).stdout.split()
    if not pid:
        raise RuntimeError("mangoapp (Steam's performance overlay) is not running.")
    try:
        with open(f"/proc/{pid[0]}/environ", "rb") as f:
            env = dict(item.split(b"=", 1) for item in f.read().split(b"\0") if b"=" in item)
    except OSError as e:
        raise RuntimeError(f"Cannot read mangoapp's environment: {e}") from e
    path = env.get(b"MANGOHUD_CONFIGFILE", b"").decode()
    if not path or not os.path.isfile(path):
        raise RuntimeError("mangoapp's config file was not found.")
    return path


def _remove_lines(path, lines):
    try:
        with open(path) as f:
            current = f.read().splitlines()
        kept = [line for line in current if line not in lines]
        while kept and not kept[-1].strip():  # drop the blank line we added
            kept.pop()
        with open(path, "w") as f:
            f.write("\n".join(kept) + "\n")
    except OSError:
        pass


def _avg(values):
    return round(sum(values) / len(values), 1) if values else None


def _summary(folder):
    logs = sorted(f for f in os.listdir(folder) if f.endswith(".csv") and not f.endswith("_summary.csv"))
    if not logs:
        return None
    with open(os.path.join(folder, logs[-1]), newline="") as f:
        rows = list(csv.reader(f))
    header_at = next((i for i, r in enumerate(rows) if r and r[0] == "fps"), None)
    if header_at is None:
        return None
    header = rows[header_at]
    frames = [dict(zip(header, r)) for r in rows[header_at + 1:] if len(r) == len(header)]
    if not frames:
        return None

    def col(name):
        out = []
        for fr in frames:
            try:
                out.append(float(fr[name]))
            except (KeyError, ValueError):
                pass
        return out

    frametimes = sorted(col("frametime"), reverse=True)  # ms, slowest first
    worst = lambda pct: round(1000 / _avg(frametimes[:max(1, len(frametimes) * pct // 100)]), 1)  # noqa: E731
    elapsed_ms = (int(frames[-1]["elapsed"]) - int(frames[0]["elapsed"])) / 1e6 if len(frames) > 1 else 0
    return {
        "frames": len(frames),
        "average_fps": round(1000 / _avg(frametimes), 1) if frametimes else None,
        "1%_low_fps": worst(1) if frametimes else None,
        "average_frametime_ms": _avg(frametimes),
        "cpu_load_%": _avg(col("cpu_load")),
        "gpu_load_%": _avg(col("gpu_load")),
        "cpu_temp_c": _avg([t for t in col("cpu_temp") if t > 0]),
        "gpu_temp_c": _avg(col("gpu_temp")),
        "cpu_power_w": _avg(col("cpu_power")),
        "gpu_power_w": _avg(col("gpu_power")),
        "gpu_clock_mhz": _avg(col("gpu_core_clock")),
        "measured_seconds": round(elapsed_ms / 1000, 1),
    }


def measure(seconds=20):
    """Log the app in front for `seconds` and summarise frame rate, load, temperatures and power."""
    if not hardware.is_game_mode():
        raise RuntimeError("Only works in Game Mode.")
    seconds = int(seconds)
    if not 5 <= seconds <= 120:
        raise ValueError("seconds must be between 5 and 120.")
    config = _mangoapp_config()
    folder = tempfile.mkdtemp(prefix="steamdeck-mcp-fps-", dir=os.environ.get("XDG_RUNTIME_DIR") or None)
    added = [f"output_folder={folder}", "autostart_log=1", f"log_duration={seconds}", "log_interval=0"]
    try:
        with open(config, "a") as f:
            f.write("\n" + "\n".join(added) + "\n")
        deadline = time.time() + seconds + 15
        while time.time() < deadline:
            time.sleep(1)
            if any(n.endswith("_summary.csv") for n in os.listdir(folder)):
                break
        else:
            raise RuntimeError("mangoapp wrote no log. Is a game or app shown in front?")
        result = _summary(folder)
        if not result:
            raise RuntimeError("mangoapp's log had no frames.")
        return result
    finally:
        _remove_lines(config, set(added))
        shutil.rmtree(folder, ignore_errors=True)
