"""Settings that survive a reboot.

SteamOS Manager does not store values set through it, and Steam re-applies its own on boot. Settings saved
with persist=true are kept in ~/.config/steamdeck-mcp/persistent.json and re-applied after login by a
systemd *user* timer, which lives in the home directory and therefore also survives SteamOS updates.
"""
import json
import os
import subprocess
import sys

CONFIG_DIR = os.path.expanduser("~/.config/steamdeck-mcp")
STATE_FILE = os.path.join(CONFIG_DIR, "persistent.json")
UNIT_DIR = os.path.expanduser("~/.config/systemd/user")
SERVICE = "steamdeck-mcp-restore.service"
TIMER = "steamdeck-mcp-restore.timer"
RESTORE_DELAY_S = 60  # give Steam time to start and apply its own settings first


def load():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write(state):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def _systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)


def _exec_command():
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server.py")
    if os.path.isfile(script):  # git checkout
        return f"{sys.executable} -I {script} --apply-persistent"
    return f"{sys.executable} -I -m steamdeck_mcp --apply-persistent"  # pip/pipx install


def _install_timer():
    os.makedirs(UNIT_DIR, exist_ok=True)
    with open(os.path.join(UNIT_DIR, SERVICE), "w") as f:
        f.write(f"""[Unit]
Description=Re-apply steamdeck-mcp persistent settings

[Service]
Type=oneshot
ExecStart={_exec_command()}
""")
    with open(os.path.join(UNIT_DIR, TIMER), "w") as f:
        f.write(f"""[Unit]
Description=Re-apply steamdeck-mcp persistent settings after login

[Timer]
OnStartupSec={RESTORE_DELAY_S}

[Install]
WantedBy=timers.target
""")
    _systemctl("daemon-reload")
    r = _systemctl("enable", TIMER)
    if r.returncode != 0:
        raise RuntimeError(f"Could not enable {TIMER}: {r.stderr.strip()}")


def _remove_timer():
    _systemctl("disable", TIMER)
    for unit in (SERVICE, TIMER):
        try:
            os.remove(os.path.join(UNIT_DIR, unit))
        except FileNotFoundError:
            pass
    _systemctl("daemon-reload")


def remember(key, value):
    """Store (value) or forget (None) a setting, and keep the boot timer in sync with whether any remain."""
    state = load()
    if value is None:
        state.pop(key, None)
    else:
        state[key] = value
    _write(state)
    if state:
        _install_timer()
    else:
        _remove_timer()
    return state


def _apply_one(key, value):
    from . import hardware, steamos_manager
    if key == "charge_limit":
        return steamos_manager.set_charge_limit(value)
    if key == "tdp":
        return hardware.set_tdp(value)
    if key == "gpu_clock":
        return hardware.set_gpu_clock(value)
    if key.startswith("option:"):
        return steamos_manager.set_option(key.split(":", 1)[1], value)
    raise ValueError(f"unknown persistent setting '{key}'")


def apply_all():
    results = {}
    for key, value in load().items():
        try:
            _apply_one(key, value)
            results[key] = {"value": value, "status": "applied"}
        except Exception as e:  # keep going so one broken setting doesn't block the rest
            results[key] = {"value": value, "status": f"failed: {e}"}
    return results


def status():
    enabled = _systemctl("is-enabled", TIMER).stdout.strip() or "not installed"
    last = _systemctl("show", SERVICE, "-p", "ExecMainExitTimestamp", "-p", "Result").stdout.strip().splitlines()
    return {"settings": load(), "state_file": STATE_FILE, "restore_timer": enabled,
            "last_restore": dict(l.split("=", 1) for l in last if "=" in l) or None,
            "note": f"Saved settings are re-applied {RESTORE_DELAY_S} s after every boot/login."}


def forget(keys):
    state = load()
    if keys == ["all"]:
        keys = list(state)
    for k in keys:
        if k not in state:
            raise ValueError(f"'{k}' is not a saved setting. Saved: {', '.join(state) or 'none'}")
        remember(k, None)
    return status()
