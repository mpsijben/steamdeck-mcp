"""OS-level controls: audio, network, bluetooth, processes, power and Game/Desktop Mode."""
import os
import re
import subprocess


def _run(*cmd, timeout=20, check=True):
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError as e:
        raise RuntimeError(f"{cmd[0]} is not installed") from e
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:2])} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout


# ---------- audio ----------

def audio_status():
    vol = _run("pactl", "get-sink-volume", "@DEFAULT_SINK@")
    m = re.search(r"(\d+)%", vol)
    return {
        "output": _run("pactl", "get-default-sink").strip(),
        "volume_percent": int(m.group(1)) if m else None,
        "muted": "yes" in _run("pactl", "get-sink-mute", "@DEFAULT_SINK@"),
    }


def set_volume(percent=None, muted=None):
    if percent is not None:
        if not 0 <= percent <= 100:
            raise ValueError("Volume must be between 0 and 100%.")
        _run("pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{int(percent)}%")
    if muted is not None:
        _run("pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0")
    return audio_status()


# ---------- network ----------

def network_status(scan=False):
    out = {"devices": []}
    for line in _run("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev").splitlines():
        dev, typ, state, conn = (line.split(":", 3) + [""] * 4)[:4]
        if typ in ("wifi", "ethernet"):
            out["devices"].append({"device": dev, "type": typ, "state": state, "connection": conn or None})
    ips = _run("ip", "-4", "-o", "addr", "show", "scope", "global", check=False)
    out["ipv4"] = re.findall(r"^\d+:\s+(\S+)\s+inet\s+(\S+)", ips, re.M)
    networks = []
    args = ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,FREQ,SECURITY", "dev", "wifi", "list"]
    if scan:
        args += ["--rescan", "yes"]
    for line in _run(*args, timeout=30, check=False).splitlines():
        parts = re.split(r"(?<!\\):", line)
        if len(parts) >= 5 and parts[1]:
            networks.append({"connected": parts[0] == "yes", "ssid": parts[1].replace("\\:", ":"),
                             "signal": int(parts[2] or 0), "band": "5 GHz" if parts[3].startswith("5") else
                             "6 GHz" if parts[3].startswith("6") else "2.4 GHz", "security": parts[4] or "open"})
    seen, unique = set(), []
    for n in sorted(networks, key=lambda n: (not n["connected"], -n["signal"])):
        if n["ssid"] not in seen:
            seen.add(n["ssid"])
            unique.append(n)
    out["wifi_networks"] = unique[:15]
    return out


# ---------- bluetooth ----------

def _bt_devices():
    devs = []
    for line in _run("bluetoothctl", "devices").splitlines():
        m = re.match(r"Device (\S+) (.+)", line)
        if m:
            devs.append((m.group(1), m.group(2)))
    return devs


def bluetooth_devices():
    out = []
    for mac, name in _bt_devices():
        info = _run("bluetoothctl", "info", mac, check=False)
        battery = re.search(r"Battery Percentage: \S+ \((\d+)\)", info)
        out.append({"name": name, "mac": mac, "connected": "Connected: yes" in info,
                    "paired": "Paired: yes" in info, "battery_percent": int(battery.group(1)) if battery else None})
    powered = "Powered: yes" in _run("bluetoothctl", "show", check=False)
    return {"bluetooth_on": powered, "devices": sorted(out, key=lambda d: (not d["connected"], d["name"]))}


def bluetooth_connect(device, connect=True):
    devs = _bt_devices()
    s = device.strip().lower()
    matches = [d for d in devs if d[0].lower() == s] or [d for d in devs if s in d[1].lower()]
    if not matches:
        raise ValueError(f"No paired device matches '{device}'. Pair new devices via Steam's Bluetooth settings.")
    if len(matches) > 1:
        raise ValueError(f"Several devices match '{device}': " + ", ".join(f"{n} ({m})" for m, n in matches))
    mac, name = matches[0]
    out = _run("bluetoothctl", "connect" if connect else "disconnect", mac, timeout=30, check=False)
    ok = ("successful" in out.lower()) or (not connect and "disconnected" in out.lower())
    if not ok:
        raise RuntimeError(f"Could not {'connect' if connect else 'disconnect'} {name}: {out.strip()[-300:]}"
                           + (" (is the device switched on and in range?)" if connect else ""))
    return {"device": name, "mac": mac, "connected": connect}


# ---------- processes ----------

def top_processes(count=10, sort="cpu"):
    key = "-%cpu" if sort == "cpu" else "-rss"
    out = _run("ps", "-eo", "pid,%cpu,rss,etime,comm,args", f"--sort={key}", "--no-headers")
    rows = []
    for line in out.splitlines()[: max(1, min(count, 50))]:
        pid, cpu, rss, etime, comm, args = (line.split(None, 5) + [""])[:6]
        rows.append({"pid": int(pid), "cpu_percent": float(cpu), "memory_mb": int(rss) // 1024,
                     "running_for": etime, "name": comm, "command": args[:160]})
    return rows


# ---------- power / session ----------

def switch_mode(mode):
    target = {"game": "gamescope", "desktop": "plasma"}.get(mode)
    if not target:
        raise ValueError("mode must be 'game' or 'desktop'")
    subprocess.Popen(["steamos-session-select", target], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"switching_to": f"{mode.capitalize()} Mode",
            "note": "The current session is closing; this MCP connection will drop if the client runs on the Deck."}
