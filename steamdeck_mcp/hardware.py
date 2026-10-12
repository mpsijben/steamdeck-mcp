"""Steam Deck hardware: sensors (read) and TDP / GPU clock / FPS limit (write via SteamOS helper)."""
import glob
import os
import shutil
import subprocess

from . import steamos_manager

PRIV_WRITE = "/usr/bin/steamos-polkit-helpers/steamos-priv-write"


def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _hwmon(name):
    for p in glob.glob("/sys/class/hwmon/hwmon*/name"):
        if _read(p) == name:
            return os.path.dirname(p)
    return None


def _gpu_device():
    for p in glob.glob("/sys/class/drm/card*/device/pp_od_clk_voltage"):
        return os.path.dirname(p)
    return None


def _priv_write(path, value):
    r = subprocess.run([PRIV_WRITE, path, str(value)], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise RuntimeError(f"steamos-priv-write refused {path}: {(r.stderr or r.stdout).strip()}")


def is_game_mode():
    r = subprocess.run(["pgrep", "-x", "gamescope-wl"], capture_output=True)
    if r.returncode == 0:
        return True
    return subprocess.run(["pgrep", "-x", "gamescope"], capture_output=True).returncode == 0


def _battery():
    b = "/sys/class/power_supply/BAT1"
    if not os.path.isdir(b):
        return None
    out = {"percent": int(_read(f"{b}/capacity", "0")), "status": _read(f"{b}/status")}
    cur, volt = _read(f"{b}/current_now"), _read(f"{b}/voltage_now")
    if cur and volt:
        out["power_w"] = round(int(cur) * int(volt) / 1e12, 1)
    now, full, design = _read(f"{b}/charge_now"), _read(f"{b}/charge_full"), _read(f"{b}/charge_full_design")
    if full and design:
        out["health_percent"] = round(100 * int(full) / int(design))
        out["capacity_mah"] = {"now": int(now or 0) // 1000, "full": int(full) // 1000, "design": int(design) // 1000}
        vmin = _read(f"{b}/voltage_min_design")
        if vmin:
            out["capacity_wh_full"] = round(int(full) * int(vmin) / 1e12, 1)
    if volt:
        out["voltage_v"] = round(int(volt) / 1e6, 2)
    if cur and now and full and int(cur) > 0:
        hours = (int(full) - int(now) if out["status"] == "Charging" else int(now)) / int(cur)
        out["time_to_full_min" if out["status"] == "Charging" else "time_left_min"] = round(hours * 60)
    cycles = _read(f"{b}/cycle_count")
    out["cycles"] = int(cycles) if cycles and cycles != "0" else "not reported by firmware"
    hw = _hwmon("steamdeck_hwmon")
    if hw:
        temp = _read(f"{hw}/temp1_input")
        if temp:
            out["temperature_c"] = round(int(temp) / 1000, 1)
        limit = _read(f"{hw}/max_battery_charge_level")
        if limit is not None:
            out["charge_limit_percent"] = int(limit) if limit not in ("0", "100") else "off (charges to 100%)"
    ac = _read("/sys/class/power_supply/ACAD/online")
    out["charger_connected"] = ac == "1"
    return out


def _temps():
    temps = {}
    for f in sorted(glob.glob("/sys/class/hwmon/hwmon*/temp*_input")):
        d = os.path.dirname(f)
        name = _read(f"{d}/name", "?")
        label = _read(f.replace("_input", "_label"), "") or ""
        key = {"amdgpu": "APU (cpu/gpu)", "acpitz": "mainboard", "nvme": "SSD",
               "steamdeck_hwmon": "", "ath11k_hwmon": "wifi"}.get(name, name)
        key = f"{key} {label}".strip() if label and name not in ("amdgpu",) else key or label
        v = _read(f)
        if v and key not in temps:
            temps[key] = round(int(v) / 1000, 1)
    return temps


def _meminfo():
    info = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        info[k] = int(v.split()[0]) * 1024
    gb = lambda x: round(x / 2**30, 1)
    return {"ram_total_gb": gb(info["MemTotal"]), "ram_available_gb": gb(info["MemAvailable"]),
            "swap_total_gb": gb(info["SwapTotal"]), "swap_used_gb": gb(info["SwapTotal"] - info["SwapFree"])}


def tdp_status():
    h = _hwmon("amdgpu")
    if not h:
        return None
    w = lambda p: round(int(_read(p, "0")) / 1e6, 1)
    return {"tdp_limit_w": w(f"{h}/power1_cap"), "tdp_default_w": w(f"{h}/power1_cap_default"),
            "current_draw_w": w(f"{h}/power1_average"), "gpu_mhz": int(_read(f"{h}/freq1_input", "0")) // 1_000_000}


def gpu_status():
    dev = _gpu_device()
    if not dev:
        return None
    od = _read(f"{dev}/pp_od_clk_voltage", "")
    sclk = []
    in_sclk = False
    for line in od.splitlines():
        if line.startswith("OD_SCLK"):
            in_sclk = True
            continue
        if in_sclk and line.strip()[:2] in ("0:", "1:"):
            sclk.append(line.split(":")[1].strip())
        elif in_sclk:
            break
    return {"performance_level": _read(f"{dev}/power_dpm_force_performance_level"), "gpu_clock_range": sclk}


def _os_info():
    info = {}
    for line in open("/etc/os-release"):
        if "=" in line:
            k, v = line.rstrip().split("=", 1)
            info[k] = v.strip('"')
    board = _read("/sys/devices/virtual/dmi/id/board_name", "")
    model = {"Jupiter": "Steam Deck LCD", "Galileo": "Steam Deck OLED"}.get(board, board or "unknown")
    return {"model": model, "steamos": info.get("VERSION_ID"), "build": info.get("BUILD_ID")}


def _disks(libraries):
    seen, out = set(), []
    for path in ["/home"] + list(libraries):
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        if st.f_fsid in seen:
            continue
        seen.add(st.f_fsid)
        u = shutil.disk_usage(path)
        out.append({"path": path, "total_gb": round(u.total / 1e9), "free_gb": round(u.free / 1e9),
                    "used_percent": round(100 * u.used / u.total)})
    return out


def system_status(libraries=()):
    fan = _read(f"{_hwmon('steamdeck_hwmon')}/fan1_input") if _hwmon("steamdeck_hwmon") else None
    return {
        "device": _os_info(),
        "mode": "Game Mode" if is_game_mode() else "Desktop Mode",
        "battery": _battery(),
        "temperatures_c": _temps(),
        "fan_rpm": int(fan) if fan else None,
        "power": tdp_status(),
        "steamos_manager": steamos_manager.settings(),
        "gpu": gpu_status(),
        "memory": _meminfo(),
        "disks": _disks(libraries),
        "load_avg": [round(x, 2) for x in os.getloadavg()],
    }


def set_tdp(watts=None):
    """watts=None resets to default. Prefers SteamOS Manager (keeps Steam's slider in sync)."""
    if steamos_manager.available():
        steamos_manager.set_tdp(watts)
        return {**tdp_status(), "via": "SteamOS Manager"}
    return _set_tdp_sysfs(watts)


def _set_tdp_sysfs(watts):
    """Fallback for SteamOS versions without SteamOS Manager: slow (power1) and fast (power2) limits."""
    h = _hwmon("amdgpu")
    if not h:
        raise RuntimeError("amdgpu hwmon not found")
    default = int(_read(f"{h}/power1_cap_default"))
    if watts is None:
        value = default
    else:
        if not 3 <= watts <= default / 1e6:
            raise ValueError(f"TDP must be between 3 and {default / 1e6:g} W (same range as Steam).")
        value = int(watts * 1e6)
    _priv_write(f"{h}/power1_cap", value)
    if os.path.exists(f"{h}/power2_cap"):
        _priv_write(f"{h}/power2_cap", value)
    return {**tdp_status(), "via": "steamos-priv-write",
            "note": "Steam's own TDP slider (Quick Access menu) can override this. Resets on reboot."}


def set_gpu_clock(mhz=None):
    """mhz=None returns GPU to automatic clocking. Prefers SteamOS Manager."""
    if steamos_manager.available():
        return {**steamos_manager.set_gpu_clock(mhz), "via": "SteamOS Manager"}
    return _set_gpu_clock_sysfs(mhz)


def _set_gpu_clock_sysfs(mhz):
    dev = _gpu_device()
    if not dev:
        raise RuntimeError("GPU device not found")
    level = f"{dev}/power_dpm_force_performance_level"
    if mhz is None:
        _priv_write(level, "auto")
        return gpu_status()
    if not 200 <= mhz <= 1600:
        raise ValueError("GPU clock must be between 200 and 1600 MHz.")
    _priv_write(level, "manual")
    od = f"{dev}/pp_od_clk_voltage"
    _priv_write(od, f"s 0 {mhz}")
    _priv_write(od, f"s 1 {mhz}")
    _priv_write(od, "c")
    return gpu_status()
