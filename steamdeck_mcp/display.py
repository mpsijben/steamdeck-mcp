"""Screen-off and sleep timeouts.

Desktop Mode uses KDE's power management (powerdevilrc); Game Mode uses Steam's own client settings.
Both are stored by KDE/Steam themselves, so they survive reboots without help.
"""
import base64
import os
import subprocess

from .cdp import evaluate

KDE_PROFILES = {"ac": "AC", "battery": "Battery", "low_battery": "LowBattery"}

# Steam client setting field numbers (CMsgClientSettings), taken from Steam's own UI code.
STEAM_FIELDS = {
    ("screen", "battery"): 24008,  # system_idle_screensaver_battery_sec
    ("screen", "ac"): 24009,       # system_idle_screensaver_ac_sec
    ("sleep", "battery"): 24003,   # system_idle_suspend_battery_sec
    ("sleep", "ac"): 24004,        # system_idle_suspend_ac_sec
}


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


def _varint(n):
    n &= (1 << 64) - 1  # int32 negatives are encoded as 10-byte varints
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def set_steam_timeouts(screen_min=None, sleep_min=None, sources=("ac", "battery")):
    """Write Steam's Game Mode idle settings (0 = never), the way Steam's settings page does."""
    msg = b""
    for kind, minutes in (("screen", screen_min), ("sleep", sleep_min)):
        if minutes is None:
            continue
        for source in sources:
            msg += _varint(STEAM_FIELDS[(kind, source)] << 3) + _varint(max(0, int(minutes * 60)))
    if msg:
        payload = base64.b64encode(msg).decode()
        evaluate(f'(async () => {{ await SteamClient.Settings.SetSetting("{payload}"); return true; }})()')
    return {"screen_off_min": screen_min, "sleep_min": sleep_min, "power_sources": list(sources),
            "note": "Game Mode values; 0 means never."}
