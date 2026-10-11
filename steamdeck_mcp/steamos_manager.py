"""SteamOS Manager D-Bus service: the official, unprivileged interface Steam itself uses for
TDP, GPU, charge limit, CPU governor, fan control, etc. Accessed through `busctl --user`."""
import json
import os
import subprocess

SERVICE = "com.steampowered.SteamOSManager1"
PATH = "/com/steampowered/SteamOSManager1"
IFACE = SERVICE + "."


class ManagerUnavailable(RuntimeError):
    pass


def _busctl(*args):
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        r = subprocess.run(["busctl", "--user", *args], capture_output=True, text=True, timeout=15, env=env)
    except FileNotFoundError as e:
        raise ManagerUnavailable("busctl not found") from e
    if r.returncode != 0:
        msg = r.stderr.strip()
        if "not found" in msg.lower() or "unknown" in msg.lower() or "not activatable" in msg.lower():
            raise ManagerUnavailable(f"SteamOS Manager unavailable: {msg}")
        raise RuntimeError(f"SteamOS Manager refused: {msg}")
    return r.stdout


def get(interface, prop):
    out = _busctl("get-property", "--json=short", SERVICE, PATH, IFACE + interface, prop)
    return json.loads(out)["data"]


def set_(interface, prop, signature, value):
    _busctl("set-property", "--", SERVICE, PATH, IFACE + interface, prop, signature, str(value))
    return get(interface, prop)


def available():
    # Interfaces come and go between SteamOS releases (e.g. "Manager" was dropped in 3.8), so only
    # check that the service object exists.
    try:
        _busctl("introspect", "--xml-interface", SERVICE, PATH)
        return True
    except (ManagerUnavailable, RuntimeError):
        return False


def _try(interface, prop):
    try:
        return get(interface, prop)
    except (ManagerUnavailable, RuntimeError):
        return None


def _named(mapping, value):
    """Human-readable name for a numeric state; unknown (newer) values are returned as-is."""
    return None if value is None else mapping.get(value, value)


FAN_CONTROL = {0: "bios", 1: "os"}
WIFI_POWER = {0: "disabled", 1: "enabled"}
HDMI_CEC = {0: "disabled", 1: "control only", 2: "control and wake"}


def settings():
    """Snapshot of every SteamOS Manager setting this device exposes (missing ones are omitted)."""
    if not available():
        return None
    charge = _try("BatteryChargeLimit1", "MaxChargeLevel")
    raw = {
        "charge_limit_percent": None if charge is None else ("off" if charge in (-1, 100) else charge),
        "tdp_limit_w": _try("TdpLimit1", "TdpLimit"),
        "tdp_range_w": [_try("TdpLimit1", "TdpLimitMin"), _try("TdpLimit1", "TdpLimitMax")],
        "gpu_performance_level": _try("GpuPerformanceLevel1", "GpuPerformanceLevel"),
        "gpu_performance_levels": _try("GpuPerformanceLevel1", "AvailableGpuPerformanceLevels"),
        "gpu_manual_clock_mhz": _try("GpuPerformanceLevel1", "ManualGpuClock"),
        "gpu_power_profile": _try("GpuPowerProfile1", "GpuPowerProfile"),
        "gpu_power_profiles": _try("GpuPowerProfile1", "AvailableGpuPowerProfiles"),
        "cpu_governor": _try("CpuScaling1", "CpuScalingGovernor"),
        "cpu_governors": _try("CpuScaling1", "AvailableCpuScalingGovernors"),
        "cpu_scheduler": _try("CpuScheduler1", "CpuScheduler"),
        "cpu_schedulers": _try("CpuScheduler1", "AvailableCpuSchedulers"),
        "cpu_boost": _try("CpuBoost1", "CpuBoostState"),
        "fan_control": _named(FAN_CONTROL, _try("FanControl1", "FanControlState")),
        "wifi_power_management": _named(WIFI_POWER, _try("WifiPowerManagement1", "WifiPowerManagementState")),
        "hdmi_cec": _named(HDMI_CEC, _try("HdmiCec1", "HdmiCecState")),
    }
    return {k: v for k, v in raw.items() if v is not None and v != [None, None]}


def set_charge_limit(percent):
    """percent=None (or 100) disables the limit."""
    if percent is None or percent >= 100:
        set_("BatteryChargeLimit1", "MaxChargeLevel", "i", -1)
        return {"charge_limit_percent": "off (charges to 100%)"}
    minimum = _try("BatteryChargeLimit1", "SuggestedMinimumLimit") or 10
    if not minimum <= percent < 100:
        raise ValueError(f"Charge limit must be between {minimum} and 100%.")
    return {"charge_limit_percent": set_("BatteryChargeLimit1", "MaxChargeLevel", "i", int(percent))}


def set_tdp(watts):
    lo, hi = get("TdpLimit1", "TdpLimitMin"), get("TdpLimit1", "TdpLimitMax")
    if watts is None:
        watts = hi
    if not lo <= watts <= hi:
        raise ValueError(f"TDP must be between {lo} and {hi} W.")
    return set_("TdpLimit1", "TdpLimit", "u", int(round(watts)))


def set_gpu_clock(mhz):
    if mhz is None:
        set_("GpuPerformanceLevel1", "GpuPerformanceLevel", "s", "auto")
        return {"gpu_performance_level": "auto"}
    lo, hi = get("GpuPerformanceLevel1", "ManualGpuClockMin"), get("GpuPerformanceLevel1", "ManualGpuClockMax")
    if not lo <= mhz <= hi:
        raise ValueError(f"GPU clock must be between {lo} and {hi} MHz.")
    set_("GpuPerformanceLevel1", "GpuPerformanceLevel", "s", "manual")
    return {"gpu_performance_level": "manual",
            "gpu_manual_clock_mhz": set_("GpuPerformanceLevel1", "ManualGpuClock", "u", int(mhz))}


# option name -> (interface, property, signature, value map or available-list property)
OPTIONS = {
    "cpu_governor": ("CpuScaling1", "CpuScalingGovernor", "s", "AvailableCpuScalingGovernors"),
    "cpu_scheduler": ("CpuScheduler1", "CpuScheduler", "s", "AvailableCpuSchedulers"),
    "cpu_boost": ("CpuBoost1", "CpuBoostState", "u", {}),
    "gpu_performance_level": ("GpuPerformanceLevel1", "GpuPerformanceLevel", "s", "AvailableGpuPerformanceLevels"),
    "gpu_power_profile": ("GpuPowerProfile1", "GpuPowerProfile", "s", "AvailableGpuPowerProfiles"),
    "fan_control": ("FanControl1", "FanControlState", "u", FAN_CONTROL),
    "wifi_power_management": ("WifiPowerManagement1", "WifiPowerManagementState", "u", WIFI_POWER),
    "hdmi_cec": ("HdmiCec1", "HdmiCecState", "u", HDMI_CEC),
}


def set_option(option, value):
    if option not in OPTIONS:
        raise ValueError(f"Unknown option '{option}'. Options: {', '.join(OPTIONS)}")
    iface, prop, sig, allowed = OPTIONS[option]
    value = str(value).strip()
    if isinstance(allowed, dict):
        by_name = {v: k for k, v in allowed.items()}
        if value.lower() in by_name:
            raw = by_name[value.lower()]
        elif value.isdigit():  # raw numeric state; SteamOS Manager rejects values it doesn't support
            raw = int(value)
        else:
            names = ", ".join(allowed.values()) or "a number"
            raise ValueError(f"'{value}' is not valid for {option}. Choose from: {names}")
        return {option: _named(allowed, set_(iface, prop, sig, raw))}
    choices = get(iface, allowed)
    match = next((c for c in choices if c.lower() == value.lower()), None)
    if match is None:
        raise ValueError(f"'{value}' is not valid for {option}. Choose from: {', '.join(choices)}")
    return {option: set_(iface, prop, sig, match)}
