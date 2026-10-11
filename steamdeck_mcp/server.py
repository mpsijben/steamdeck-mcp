"""Steam Deck MCP server: JSON-RPC 2.0 over stdio, stdlib only."""
import json
import sys
import traceback

from . import __version__, cdp, display, hardware, maintenance, persist, steam, steamos_manager, system

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

GAME = {"type": ["string", "integer"], "description": "Appid or (part of) the name, e.g. 'Battle.net' or 1086940."}


def _tool(name, description, props=None, required=(), read_only=False, destructive=False):
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": props or {}, "required": list(required)},
        "annotations": {"readOnlyHint": read_only, "destructiveHint": destructive},
    }


TOOLS = [
    # --- setup ---
    _tool("steam_integration_status", "Check whether the Steam client integration (CEF remote debugging) is enabled "
          "and reachable. Game/Proton tools need it; hardware and file tools do not.", read_only=True),
    _tool("enable_steam_integration", "Enable the Steam client integration by creating "
          "~/.steam/steam/.cef-enable-remote-debugging (the same thing Decky Loader does). Steam must be restarted "
          "afterwards. The debugger only listens on 127.0.0.1."),
    # --- info ---
    _tool("system_status", "Steam Deck overview: model, SteamOS version, Game/Desktop Mode, battery, temperatures, "
          "fan, TDP and power draw, GPU, memory and free disk space.", read_only=True),
    _tool("list_games", "All installed Steam games and non-Steam shortcuts with Proton version, launch options, "
          "size, playtime and whether they are running. Requires Steam to be running.",
          {"details": {"type": "boolean", "description": "Fetch Proton/launch options/size (default true)."}},
          read_only=True),
    _tool("game_info", "Details for one game: Proton version, launch options, install folder, size, "
          "Steam Deck compatibility results.", {"game": GAME}, ["game"], read_only=True),
    _tool("running_games", "Games/apps that are currently running.", read_only=True),
    _tool("list_compat_tools", "Available Proton/compatibility tools (optionally for a specific game).",
          {"game": GAME}, read_only=True),
    _tool("list_proton_installs", "Installed Proton versions (official and custom/GE) and which games use them.",
          read_only=True),
    _tool("check_ge_proton", "Latest GE-Proton release on GitHub and whether it is installed.", read_only=True),
    _tool("check_os_updates", "Check for SteamOS/Steam client updates and show the current update branch.",
          read_only=True),
    _tool("cleanup_report", "Find orphaned Proton prefixes and shader caches of games that are no longer installed, "
          "with sizes. Deletes nothing.", read_only=True),
    _tool("find_game_logs", "Find recent log files for a game (inside its Proton prefix, plus ~/steam-<appid>.log).",
          {"game": GAME, "days": {"type": "integer", "description": "How many days back (default 14)."}},
          ["game"], read_only=True),
    _tool("read_log", "Read the last lines of a log file, optionally filtered by a regex.",
          {"path": {"type": "string"}, "lines": {"type": "integer", "description": "Default 150."},
           "contains": {"type": "string", "description": "Regex filter, e.g. 'error|fail'."}},
          ["path"], read_only=True),
    _tool("power_settings", "SteamOS power settings via SteamOS Manager: charge limit, TDP, GPU performance "
          "level/clock/power profile, CPU governor, fan control, Wi-Fi power saving, HDMI-CEC.", read_only=True),
    _tool("download_status", "Steam download queue: current download, speed, ETA, queued updates and whether "
          "downloads are paused.", read_only=True),
    _tool("audio_status", "Current audio output, volume and mute state.", read_only=True),
    _tool("network_status", "Wi-Fi/Ethernet connection, IP address and nearby Wi-Fi networks with signal strength.",
          {"scan": {"type": "boolean", "description": "Force a fresh Wi-Fi scan (slower)."}}, read_only=True),
    _tool("bluetooth_devices", "Paired Bluetooth devices (controllers, headphones, ...) with connection state and "
          "battery level where reported.", read_only=True),
    _tool("top_processes", "Processes using the most CPU or memory, useful when a game stutters.",
          {"count": {"type": "integer", "description": "Default 10."},
           "sort": {"enum": ["cpu", "memory"], "description": "Default cpu."}}, read_only=True),
    _tool("storage_locations", "Steam library locations (internal SSD, microSD) with free space and game count.",
          read_only=True),
    _tool("search_owned_games", "Search all games you own, installed or not (e.g. to find something to install).",
          {"query": {"type": "string", "description": "Part of the name; empty = all."},
           "installed": {"type": "boolean", "description": "Only installed (true) or not installed (false)."},
           "limit": {"type": "integer", "description": "Default 50."}}, read_only=True),
    _tool("list_global_compat_tools", "Compat tools available as the global default for Windows games.",
          read_only=True),
    # --- actions ---
    _tool("set_compat_tool", "Set the Proton version for a game. Use a 'name' from list_compat_tools "
          "(e.g. 'proton_experimental', 'GE-Proton10-20'); empty string = Steam default.",
          {"game": GAME, "tool": {"type": "string"}}, ["game", "tool"]),
    _tool("set_launch_options", "Set launch options for a game (e.g. 'PROTON_LOG=1 %command%'). "
          "Empty string clears them.", {"game": GAME, "options": {"type": "string"}}, ["game", "options"]),
    _tool("launch_game", "Launch a game.", {"game": GAME}, ["game"]),
    _tool("stop_game", "Force-quit a running game (unsaved progress is lost).",
          {"game": GAME}, ["game"], destructive=True),
    _tool("set_tdp", "Set the TDP limit (3-15 W). Lower = longer battery life and cooler. Omit 'watts' to "
          "restore the default. Uses SteamOS Manager, so Steam's own slider stays in sync.",
          {"watts": {"type": "number"}, "persist": {"type": "boolean", "description": "Keep this setting after a reboot (re-applied automatically)."}}),
    _tool("set_gpu_clock", "Pin the GPU clock (200-1600 MHz), or omit 'mhz' to return to automatic.",
          {"mhz": {"type": "integer"}, "persist": {"type": "boolean", "description": "Keep this setting after a reboot (re-applied automatically)."}}),
    _tool("set_fps_limit", "Experimental, Game Mode only: frame limit via gamescope (0 = off).",
          {"fps": {"type": "integer"}}, ["fps"]),
    _tool("set_brightness", "Set screen brightness (0-100%).", {"percent": {"type": "number"}}, ["percent"]),
    _tool("set_charge_limit", "Limit battery charging to a percentage (e.g. 80) to reduce battery wear when the "
          "Deck is often plugged in. Omit 'percent' (or use 100) to charge to 100%.",
          {"percent": {"type": "integer"}, "persist": {"type": "boolean", "description": "Keep this setting after a reboot (re-applied automatically)."}}),
    _tool("set_power_option", "Change a SteamOS power setting. Options: cpu_governor (powersave/performance), "
          "cpu_scheduler (e.g. none/lavd), cpu_boost (number), gpu_performance_level (auto/low/high/manual/"
          "profile_peak), gpu_power_profile, fan_control (os/bios), wifi_power_management (enabled/disabled), "
          "hdmi_cec (number or disabled/control only/control and wake). Available options depend on the SteamOS "
          "version. "
          "See power_settings for the values this Deck supports.",
          {"option": {"enum": list(steamos_manager.OPTIONS)}, "value": {"type": "string"}, "persist": {"type": "boolean", "description": "Keep this setting after a reboot (re-applied automatically)."}},
          ["option", "value"]),
    _tool("screen_timeouts", "When the screen turns off and the Deck goes to sleep when idle, in Desktop Mode "
          "(KDE) and Game Mode (Steam) per power source.", read_only=True),
    _tool("set_screen_timeout", "Set after how many idle minutes the screen turns off and/or the Deck sleeps. "
          "0 = never. Applies to Desktop Mode (KDE), Game Mode (Steam, needs Steam running) or both, on battery, "
          "charger or both. Settings are saved by KDE/Steam and survive reboots.",
          {"screen_minutes": {"type": "number"}, "sleep_minutes": {"type": "number"},
           "mode": {"enum": ["both", "desktop", "game"], "description": "Default both."},
           "power_source": {"enum": ["both", "battery", "ac"], "description": "Default both."}}),
    _tool("persistent_settings", "Settings saved with persist=true, which are re-applied after every reboot, and "
          "whether the restore timer is active.", read_only=True),
    _tool("forget_persistent_settings", "Stop re-applying saved settings after reboot. Pass setting keys from "
          "persistent_settings (e.g. 'charge_limit', 'tdp', 'option:cpu_governor') or ['all'].",
          {"keys": {"type": "array", "items": {"type": "string"}}}, ["keys"]),
    _tool("set_volume", "Set the output volume (0-100%) and/or mute.",
          {"percent": {"type": "number"}, "muted": {"type": "boolean"}}),
    _tool("bluetooth_connect", "Connect or disconnect a paired Bluetooth device by name or MAC address.",
          {"device": {"type": "string"}, "connect": {"type": "boolean", "description": "false = disconnect."}},
          ["device"]),
    _tool("set_airplane_mode", "Turn airplane mode on/off (disables Wi-Fi and Bluetooth; a remote/SSH MCP "
          "connection will drop).", {"enabled": {"type": "boolean"}}, ["enabled"]),
    _tool("set_downloads_paused", "Pause or resume all Steam downloads.", {"paused": {"type": "boolean"}}, ["paused"]),
    _tool("update_game", "Queue a Steam game update and start it.", {"game": GAME}, ["game"]),
    _tool("verify_game", "Verify a Steam game's files and re-download broken ones (fixes many crashes).",
          {"game": GAME}, ["game"]),
    _tool("install_game", "Install an owned Steam game on 'internal' or 'sdcard' (default: Steam's default "
          "library). dry_run=true only reports the download size. If the game has a license agreement (EULA), the "
          "install window is left open for the user, unless accept_eula=true. Only set accept_eula=true when the "
          "user explicitly asked to accept license agreements automatically.",
          {"game": GAME, "location": {"type": "string"}, "dry_run": {"type": "boolean"},
           "accept_eula": {"type": "boolean", "description": "Accept the game's EULA(s) on the user's behalf. "
                           "Only when the user explicitly asked for it."}}, ["game"]),
    _tool("uninstall_game", "Uninstall a Steam game. Deletes its files and Proton prefix; local saves without "
          "Steam Cloud can be lost (the tool warns). Requires confirm=true.",
          {"game": GAME, "confirm": {"type": "boolean"}}, ["game", "confirm"], destructive=True),
    _tool("move_game", "Move an installed Steam game between internal storage and the microSD card.",
          {"game": GAME, "location": {"type": "string", "description": "'internal', 'sdcard' or a library index."}},
          ["game", "location"]),
    _tool("set_global_compat_tool", "Set the default Proton version for all Windows games without a per-game "
          "override (Steam Play setting).", {"tool": {"type": "string"}}, ["tool"]),
    _tool("add_shortcut", "Add a non-Steam game/app to the library, optionally running it with Proton "
          "(compat_tool, e.g. 'proton_experimental') for Windows .exe files.",
          {"name": {"type": "string"}, "exe": {"type": "string", "description": "Absolute path to the executable."},
           "start_dir": {"type": "string"}, "launch_options": {"type": "string"},
           "compat_tool": {"type": "string"}}, ["name", "exe"]),
    _tool("remove_shortcut", "Remove a non-Steam shortcut from the library (files and prefix are kept). "
          "Requires confirm=true.", {"game": GAME, "confirm": {"type": "boolean"}}, ["game", "confirm"],
          destructive=True),
    _tool("switch_mode", "Switch between Game Mode and Desktop Mode. Closes running apps. Requires confirm=true.",
          {"mode": {"enum": ["game", "desktop"]}, "confirm": {"type": "boolean"}}, ["mode", "confirm"],
          destructive=True),
    _tool("power_action", "Suspend, restart or shut down the Deck. Requires confirm=true.",
          {"action": {"enum": ["suspend", "restart", "shutdown"]}, "confirm": {"type": "boolean"}},
          ["action", "confirm"], destructive=True),
    _tool("install_ge_proton", "Download and install GE-Proton (latest or a specific tag) with checksum "
          "verification. Takes a few minutes.", {"tag": {"type": "string", "description": "e.g. GE-Proton10-20"}}),
    _tool("remove_proton", "Remove a custom Proton version from compatibilitytools.d (refuses if a game still "
          "uses it).", {"name": {"type": "string"}}, ["name"], destructive=True),
    _tool("delete_orphans", "Delete orphaned prefixes/shader caches (only appids that cleanup_report marks as "
          "'orphan' and that contain no installed programs). Requires confirm=true.",
          {"appids": {"type": "array", "items": {"type": "integer"}},
           "kinds": {"type": "array", "items": {"enum": ["compatdata", "shadercache"]},
                     "description": "Default: both."},
           "confirm": {"type": "boolean"}}, ["appids", "confirm"], destructive=True),
]


def _logs_for(game, days=14):
    appid, _, is_shortcut = steam.resolve_app(game)
    extra = []
    if is_shortcut:
        # Shortcuts may run an exe that lives in another prefix (e.g. Battle.net); search that one too.
        info = steam.game_info(appid)
        for p in (info.get("exe") or "", info.get("start_dir") or ""):
            if "/compatdata/" in p:
                extra.append(p.strip('"').split("/pfx/")[0])
    return {"appid": appid, **maintenance.find_logs(appid, days or 14, extra)}


def _switch_mode(mode, confirm):
    if not confirm:
        raise ValueError(f"Pass confirm=true to switch to {mode} mode (running apps will close).")
    return system.switch_mode(mode)


def _power_settings():
    settings = steamos_manager.settings()
    if settings is None:
        raise RuntimeError("SteamOS Manager is not available on this system (older SteamOS?).")
    return settings


def _persisting(args, key, value, apply):
    """Apply a setting; with persist=true also save it for re-applying after reboot.
    Resetting to default (value None) while persisting forgets the saved value."""
    result = apply(value)
    if args.get("persist"):
        persist.remember(key, value)
        result = {**result, "persisted": value is not None,
                  "note": "Saved: re-applied automatically after every reboot." if value is not None else
                          "Back to default; no longer re-applied after reboot."}
    return result


def _set_screen_timeout(screen, sleep, mode, source):
    if screen is None and sleep is None:
        raise ValueError("Pass screen_minutes and/or sleep_minutes (0 = never).")
    sources = ("ac", "battery") if source == "both" else (source,)
    result, warnings = {}, []
    if mode in ("both", "desktop"):
        profiles = sources + (("low_battery",) if "battery" in sources else ())
        result["desktop_mode"] = display.set_kde_timeouts(screen, sleep, profiles)
        if screen:
            for name in profiles:
                s = result["desktop_mode"][name]["sleep_min"]
                if isinstance(s, (int, float)) and s < screen:
                    warnings.append(f"Desktop Mode ({name}): the Deck sleeps after {s} min, before the screen "
                                    f"timeout of {screen} min; raise sleep_minutes too.")
    if mode in ("both", "game"):
        try:
            result["game_mode"] = display.set_steam_timeouts(screen, sleep, sources)
        except RuntimeError as e:  # Steam not running: still report the Desktop Mode part
            result["game_mode"] = f"not changed: {e}"
    if warnings:
        result["warnings"] = warnings
    return result


def _remove_proton(name):
    # config.vdf is only flushed when Steam exits, so also check Steam's live settings.
    try:
        users = [g["name"] for g in steam.list_games() if g.get("compat_tool") == name]
    except RuntimeError:
        users = []
    if users:
        raise RuntimeError(f"{name} is still assigned to: {', '.join(users)}. Switch them to another Proton first.")
    return maintenance.remove_proton(name)


HANDLERS = {
    "steam_integration_status": lambda a: cdp.integration_status(),
    "enable_steam_integration": lambda a: cdp.enable_integration(),
    "system_status": lambda a: hardware.system_status(maintenance.libraries()),
    "list_games": lambda a: steam.list_games(a.get("details", True)),
    "game_info": lambda a: steam.game_info(a["game"]),
    "running_games": lambda a: steam.running_games(),
    "list_compat_tools": lambda a: steam.list_compat_tools(a.get("game")),
    "list_proton_installs": lambda a: maintenance.list_proton(),
    "check_ge_proton": lambda a: maintenance.latest_ge_proton(),
    "check_os_updates": lambda a: steam.check_os_updates(),
    "cleanup_report": lambda a: maintenance.cleanup_report(),
    "find_game_logs": lambda a: _logs_for(a["game"], a.get("days")),
    "read_log": lambda a: maintenance.read_log(a["path"], a.get("lines") or 150, a.get("contains")),
    "set_compat_tool": lambda a: steam.set_compat_tool(a["game"], a["tool"]),
    "set_launch_options": lambda a: steam.set_launch_options(a["game"], a["options"]),
    "launch_game": lambda a: steam.launch_game(a["game"]),
    "stop_game": lambda a: steam.stop_game(a["game"]),
    "set_tdp": lambda a: _persisting(a, "tdp", a.get("watts"), hardware.set_tdp),
    "set_gpu_clock": lambda a: _persisting(a, "gpu_clock", a.get("mhz"), hardware.set_gpu_clock),
    "set_fps_limit": lambda a: hardware.set_fps_limit(a["fps"]),
    "set_brightness": lambda a: steam.set_brightness(a["percent"]),
    "power_settings": lambda a: _power_settings(),
    "download_status": lambda a: steam.download_status(),
    "audio_status": lambda a: system.audio_status(),
    "network_status": lambda a: system.network_status(a.get("scan", False)),
    "bluetooth_devices": lambda a: system.bluetooth_devices(),
    "top_processes": lambda a: system.top_processes(a.get("count") or 10, a.get("sort") or "cpu"),
    "list_global_compat_tools": lambda a: steam.global_compat_tool(),
    "set_charge_limit": lambda a: _persisting(a, "charge_limit", a.get("percent"), steamos_manager.set_charge_limit),
    "set_power_option": lambda a: _persisting(a, f"option:{a['option']}", a["value"],
                                              lambda v: steamos_manager.set_option(a["option"], v)),
    "screen_timeouts": lambda a: {"desktop_mode": display.kde_timeouts(), "game_mode": display.steam_timeouts()},
    "set_screen_timeout": lambda a: _set_screen_timeout(a.get("screen_minutes"), a.get("sleep_minutes"),
                                                        a.get("mode") or "both", a.get("power_source") or "both"),
    "persistent_settings": lambda a: persist.status(),
    "forget_persistent_settings": lambda a: persist.forget(a["keys"]),
    "set_volume": lambda a: system.set_volume(a.get("percent"), a.get("muted")),
    "bluetooth_connect": lambda a: system.bluetooth_connect(a["device"], a.get("connect", True)),
    "set_airplane_mode": lambda a: steam.set_airplane_mode(a["enabled"]),
    "set_downloads_paused": lambda a: steam.set_downloads_paused(a["paused"]),
    "update_game": lambda a: steam.update_game(a["game"]),
    "verify_game": lambda a: steam.verify_game(a["game"]),
    "storage_locations": lambda a: steam.storage_locations(),
    "search_owned_games": lambda a: steam.search_owned_games(a.get("query", ""), a.get("installed"),
                                                             a.get("limit") or 50),
    "install_game": lambda a: steam.install_game(a["game"], a.get("location"), a.get("dry_run", False),
                                                 a.get("accept_eula", False)),
    "uninstall_game": lambda a: steam.uninstall_game(a["game"], a.get("confirm", False)),
    "move_game": lambda a: steam.move_game(a["game"], a["location"]),
    "set_global_compat_tool": lambda a: steam.set_global_compat_tool(a["tool"]),
    "add_shortcut": lambda a: steam.add_shortcut(a["name"], a["exe"], a.get("start_dir"), a.get("launch_options"),
                                                 a.get("compat_tool")),
    "remove_shortcut": lambda a: steam.remove_shortcut(a["game"], a.get("confirm", False)),
    "switch_mode": lambda a: _switch_mode(a["mode"], a.get("confirm", False)),
    "power_action": lambda a: steam.power_action(a["action"], a.get("confirm", False)),
    "install_ge_proton": lambda a: maintenance.install_ge_proton(a.get("tag")),
    "remove_proton": lambda a: _remove_proton(a["name"]),
    "delete_orphans": lambda a: maintenance.delete_orphans(
        a["appids"], tuple(a.get("kinds") or ("compatdata", "shadercache")), a.get("confirm", False)),
}


def _call_tool(params):
    name, args = params.get("name"), params.get("arguments") or {}
    if name not in HANDLERS:
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
    try:
        result = HANDLERS[name](args)
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
        return {"content": [{"type": "text", "text": text}]}
    except Exception as e:  # report to the model instead of crashing the server
        if not isinstance(e, (ValueError, RuntimeError)):  # CDPError is a RuntimeError
            print(traceback.format_exc(), file=sys.stderr)
        return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}


def _handle(msg):
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        return {
            "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "steamdeck", "version": __version__},
            "instructions": "Controls this Steam Deck: system status, games, per-game and global Proton, "
                            "downloads, TDP/GPU/CPU/charge limit, audio, Wi-Fi, Bluetooth, power, GE-Proton installs, "
                            "cleanup and game logs. Game/Proton tools need Steam running "
                            "with the integration enabled (see steam_integration_status). Reply to the user in "
                            "their own language.",
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        return _call_tool(params)
    if method == "ping":
        return {}
    raise KeyError(method)


def main():
    if "--apply-persistent" in sys.argv[1:]:  # run by the systemd user timer after boot
        print(json.dumps(persist.apply_all(), indent=2))
        return
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in msg:  # notification (e.g. notifications/initialized)
            continue
        try:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": _handle(msg)}
        except KeyError as e:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"Method not found: {e}"}}
        except Exception as e:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
