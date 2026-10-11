"""Screen-off and sleep timeouts.

Desktop Mode uses KDE's power management (powerdevilrc); Game Mode uses Steam's own client settings.
Both are stored by KDE/Steam themselves, so they survive reboots without help.
"""
import base64
import os
import re
import struct
import subprocess

from .cdp import evaluate, set_client_settings, varint

KDE_PROFILES = {"ac": "AC", "battery": "Battery", "low_battery": "LowBattery"}

STEAM_CONFIG = os.path.expanduser("~/.local/share/Steam/config/config.vdf")

# Field numbers taken from Steam's own UI code. The screen-off time on Steam's Power page is the backlight
# dim setting (CMsgSystemSettings, float seconds); sleep is a client setting (CMsgClientSettings, int32 seconds).
STEAM_DIM_FIELDS = {"battery": 1, "ac": 2}       # idle_backlight_dim_{battery,ac}_seconds
STEAM_SUSPEND_FIELDS = {"battery": 24003, "ac": 24004}  # system_idle_suspend_{battery,ac}_sec
STEAM_CONFIG_KEYS = {("screen", "battery"): "IdleBacklightDimBatterySeconds",
                     ("screen", "ac"): "IdleBacklightDimACSeconds",
                     ("sleep", "battery"): "IdleSuspendBatterySeconds",
                     ("sleep", "ac"): "IdleSuspendACSeconds"}


def _kread(file, groups, key):
    args = ["kreadconfig6", "--file", file]
    for g in groups:
        args += ["--group", g]
    r = subprocess.run(args + ["--key", key], capture_output=True, text=True, timeout=10)
    return r.stdout.strip() or None


def _kwrite(file, groups, key, value):
    args = ["kwriteconfig6", "--file", file]
    for g in groups:
        args += ["--group", g]
    subprocess.run(args + ["--key", key, str(value)], check=True, capture_output=True, timeout=10)


def _minutes(sec):
    if sec is None:
        return "KDE default"
    sec = int(sec)
    return "never" if sec <= 0 else round(sec / 60, 1)


def kde_timeouts():
    out = {}
    for name, group in KDE_PROFILES.items():
        off_enabled = _kread("powerdevilrc", [group, "Display"], "TurnOffDisplayWhenIdle")
        off = _kread("powerdevilrc", [group, "Display"], "TurnOffDisplayIdleTimeoutSec")
        action = _kread("powerdevilrc", [group, "SuspendAndShutdown"], "AutoSuspendAction")
        sleep = _kread("powerdevilrc", [group, "SuspendAndShutdown"], "AutoSuspendIdleTimeoutSec")
        out[name] = {
            "screen_off_min": "never" if off_enabled == "false" else _minutes(off),
            "sleep_min": "never" if action == "0" else _minutes(sleep),
        }
    return out


def set_kde_timeouts(screen_min=None, sleep_min=None, profiles=("ac", "battery", "low_battery")):
    for name in profiles:
        group = KDE_PROFILES[name]
        if screen_min is not None:
            _kwrite("powerdevilrc", [group, "Display"], "TurnOffDisplayWhenIdle", "false" if screen_min <= 0 else "true")
            if screen_min > 0:
                _kwrite("powerdevilrc", [group, "Display"], "TurnOffDisplayIdleTimeoutSec", int(screen_min * 60))
        if sleep_min is not None:
            _kwrite("powerdevilrc", [group, "SuspendAndShutdown"], "AutoSuspendAction", 0 if sleep_min <= 0 else 1)
            if sleep_min > 0:
                _kwrite("powerdevilrc", [group, "SuspendAndShutdown"], "AutoSuspendIdleTimeoutSec", int(sleep_min * 60))
    # Tell the running power manager to pick up the change (only exists inside a Plasma session).
    env = {**os.environ, "DBUS_SESSION_BUS_ADDRESS": os.environ.get(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")}
    subprocess.run(["qdbus", "org.kde.Solid.PowerManagement", "/org/kde/Solid/PowerManagement",
                    "reparseConfiguration"], capture_output=True, timeout=10, env=env)
    return kde_timeouts()


def steam_timeouts():
    """Game Mode idle settings as last saved by Steam in config.vdf."""
    try:
        txt = open(STEAM_CONFIG, errors="replace").read()
    except OSError:
        return "unknown: Steam's config.vdf not found"
    out = {}
    for (kind, source), key in STEAM_CONFIG_KEYS.items():
        m = re.search(rf'"{key}"\s+"([\d.]+)"', txt)
        out.setdefault(source, {})[f"{'screen_off' if kind == 'screen' else 'sleep'}_min"] = (
            _minutes(float(m.group(1))) if m else "default")
    return out


def set_steam_timeouts(screen_min=None, sleep_min=None, sources=("ac", "battery")):
    """Write Steam's Game Mode idle settings (0 = never), the way Steam's Power settings page does."""
    if screen_min is not None:
        msg = b"".join(varint(STEAM_DIM_FIELDS[s] << 3 | 5) + struct.pack("<f", max(0, screen_min * 60))
                       for s in sources)
        payload = base64.b64encode(msg).decode()
        evaluate(f'(async () => {{ await SteamClient.System.UpdateSettings("{payload}"); return true; }})()')
    if sleep_min is not None:
        set_client_settings({STEAM_SUSPEND_FIELDS[s]: max(0, int(sleep_min * 60)) for s in sources})
    return {"screen_off_min": screen_min, "sleep_min": sleep_min, "power_sources": list(sources),
            "note": "Game Mode values; 0 means never."}
