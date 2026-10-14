"""Steam Deck MCP server: JSON-RPC 2.0 over stdio, stdlib only."""
import json
import os
import sys
import traceback

from . import __version__, cdp, display, fps, gamepad, hardware, recording, maintenance, persist, screen, steam, steamos_manager, system

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

WAIT = {"type": "integer", "description": "How long to wait for the game to be in front (default 60)."}
GAME = {"type": ["string", "integer"], "description": "Appid or (part of) the name, e.g. 'Battle.net' or 1086940."}


# Access levels, chosen when the server is registered (--readonly / --full, or STEAMDECK_MCP_LEVEL):
#   readonly: only tools that look; standard (default): also change settings; full: also keyboard/mouse input.
LEVELS = ("readonly", "standard", "full")
LEVEL = "standard"


def _tool(name, description, props=None, required=(), read_only=False, destructive=False, level=None):
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": props or {}, "required": list(required)},
        "annotations": {"readOnlyHint": read_only, "destructiveHint": destructive},
        "_level": level or ("readonly" if read_only else "standard"),
    }


def _visible_tools():
    allowed = LEVELS[:LEVELS.index(LEVEL) + 1]
    return [{k: v for k, v in t.items() if k != "_level"} for t in TOOLS if t["_level"] in allowed]


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
          "restore the default. In Game Mode the value is also stored in Steam's active performance profile, so its "
          "slider shows it; pass 'game' to wait until that game is in front so it lands in the game's profile.",
          {"watts": {"type": "number"}, "persist": {"type": "boolean", "description": "Keep this setting after a reboot (re-applied automatically). Not with 'game'."},
           "game": GAME, "wait_seconds": WAIT}),
    _tool("set_gpu_clock", "Pin the GPU clock (200-1600 MHz), or omit 'mhz' to return to automatic.",
          {"mhz": {"type": "integer"}, "persist": {"type": "boolean", "description": "Keep this setting after a reboot (re-applied automatically)."}}),
    _tool("set_fps_limit", "Game Mode only: set Steam's framerate limit (10-90, 0 = off), like the slider in the "
          "Performance menu. Steam applies it to the app in front: pass 'game' to wait until that game is shown "
          "(ask the user to switch to it). Goes into the game's own profile if it has one (see set_game_profile).",
          {"fps": {"type": "integer"}, "game": GAME, "wait_seconds": WAIT}, ["fps"]),
    _tool("perf_status", "Game Mode only: the game Steam's Performance settings currently apply to, whether it "
          "uses its own profile, its framerate limit, overlay level and scaling settings. Steam does not expose the measured FPS.", read_only=True),
    _tool("measure_fps", "Game Mode only: measure the app in front for N seconds (default 20) with Steam's "
          "performance overlay logger: average FPS, 1% low, frame time, CPU/GPU load, temperatures, power and GPU "
          "clock. Pass 'game' to wait until that game is in front first (ask the user to switch to it and play). "
          "Use it before and after changing settings to compare. If the performance overlay is off, a small FPS "
          "counter is shown during the measurement.",
          {"seconds": {"type": "integer", "description": "5-120, default 20."}, "game": GAME, "wait_seconds": WAIT},
          read_only=True),
    _tool("recording_status", "Steam Game Recording: mode (off/background/manual), how much background video is "
          "kept, quality, what is being recorded now, storage used and the latest clips.", read_only=True),
    _tool("set_recording", "Change Steam Game Recording. mode: off, background (always records the game you play, "
          "so you can clip moments afterwards; about 3.5 GB per hour at standard quality, older video is dropped) "
          "or manual. keep_minutes: how much background video to keep (10-1440). quality: low, medium, standard, "
          "high. If background recording doesn't start, restart Steam once.",
          {"mode": {"enum": list(recording.MODES)}, "keep_minutes": {"type": "integer"},
           "quality": {"enum": list(recording.QUALITIES)}}),
    _tool("save_clip", "Save the last N seconds (default 30) of the background recording as a Steam clip, like the "
          "clip button. Needs background recording on and a game running.",
          {"seconds": {"type": "integer", "description": "5-600, default 30."}, "name": {"type": "string"}}),
    _tool("export_clip", "Export a clip (see recording_status) as an MP4 file, by default into Steam's export folder "
          "(usually ~/Videos), e.g. to share it.",
          {"clip_id": {"type": "string"}, "path": {"type": "string", "description": "Optional .mp4 path."}}, ["clip_id"]),
    _tool("delete_clip", "Delete a Steam clip to free space (exported MP4 files are kept). Needs confirm=true.",
          {"clip_id": {"type": "string"}, "confirm": {"type": "boolean"}}, ["clip_id"], destructive=True),
    _tool("screenshot", "Game Mode only: capture what is on screen (game, Steam or app in front) as a 1280x800 "
          "PNG. Use it to read settings menus or check what a change did.", read_only=True),
    _tool("press_key", "Full access only. Press keys in the window in front, like a keyboard: 'Escape', 'Return', "
          "'Up', 'Tab', 'ctrl+s', 'alt+Return' (xdotool key names; space-separated for a sequence). Real input: "
          "take a screenshot first and make sure the right window is in front.",
          {"keys": {"type": "string"}, "repeat": {"type": "integer", "description": "Press the sequence N times (default 1, max 20)."}},
          ["keys"], level="full"),
    _tool("press_button", "Full access only. Press controller buttons through a virtual Xbox controller, for "
          "games or menus that need a gamepad: a, b, x, y, lb, rb, lt, rt, menu (start), view (select), guide, ls, rs, "
          "up/down/left/right (d-pad). Space-separated buttons are pressed one after another, or together with "
          "together=true. hold_ms (up to 10000) keeps them held, e.g. to sprint. The first press connects the "
          "controller (games see a second controller).",
          {"buttons": {"type": "string"}, "together": {"type": "boolean", "description": "Press all at once (default false)."},
           "hold_ms": {"type": "integer", "description": "How long each press lasts (default 120, max 10000)."}},
          ["buttons"], level="full"),
    _tool("move_stick", "Full access only. Push an analog stick of the virtual controller: x/y from -1 to 1 (x right, "
          "y up) for duration_ms (default 500, max 10000), then release. Left stick usually moves, right stick turns "
          "the camera. Take a screenshot afterwards to see the effect.",
          {"stick": {"enum": ["left", "right"], "description": "Default left."}, "x": {"type": "number"},
           "y": {"type": "number"}, "duration_ms": {"type": "integer"}}, level="full"),
    _tool("type_text", "Full access only. Type text into the window in front (max 500 characters). Real input.",
          {"text": {"type": "string"}}, ["text"], level="full"),
    _tool("mouse_click", "Full access only. Click at x/y in screenshot coordinates (1280x800) in the window in "
          "front. Real input: take a screenshot first to find the spot.",
          {"x": {"type": "integer"}, "y": {"type": "integer"},
           "button": {"enum": ["left", "right", "middle"], "description": "Default left."},
           "double": {"type": "boolean", "description": "Double-click (default false)."}},
          ["x", "y"], level="full"),
    _tool("mouse_move", "Full access only. Move the mouse in the window in front, like the right trackpad: to x/y "
          "(screenshot coordinates) or by dx/dy pixels spread over duration_ms (default 300), e.g. to turn the camera "
          "in games that use the mouse. drag holds a button while moving (to drag items or a map).",
          {"x": {"type": "integer"}, "y": {"type": "integer"}, "dx": {"type": "integer"}, "dy": {"type": "integer"},
           "duration_ms": {"type": "integer"}, "drag": {"enum": ["left", "right", "middle"]}}, level="full"),
    _tool("mouse_scroll", "Full access only. Scroll like a mouse wheel or the left trackpad: direction up, down, "
          "left or right, amount notches (default 3), optionally at x/y (screenshot coordinates).",
          {"direction": {"enum": ["up", "down", "left", "right"]}, "amount": {"type": "integer"},
           "x": {"type": "integer"}, "y": {"type": "integer"}}, ["direction"], level="full"),
    _tool("set_scaling", "Game Mode only: Steam's scaling settings for the active performance profile, like the "
          "Performance menu. filter: linear, pixel (sharp pixels for pixel art) or sharp (AMD FSR upscaling, use with a "
          "lower in-game resolution for more FPS); sharpness 0-5 for FSR; mode: auto, integer, fit, fill, stretch. "
          "Pass 'game' to wait until that game is in front.",
          {"filter": {"enum": ["linear", "pixel", "sharp"]}, "sharpness": {"type": "integer"},
           "mode": {"enum": ["auto", "integer", "fit", "fill", "stretch"]}, "game": GAME, "wait_seconds": WAIT}),
    _tool("set_perf_overlay", "Game Mode only: Steam's performance overlay level as numbered in the Performance "
          "menu: 0 off, 1 FPS counter only, 2-4 more detail (CPU/GPU, temperatures, power, frame times).",
          {"level": {"type": "integer"}}, ["level"]),
    _tool("set_game_profile", "Game Mode only: turn Steam's per-game performance profile on or off for a game, "
          "so its framerate limit, TDP etc. no longer follow the global profile. The game must be running; "
          "the tool waits until it is in front (ask the user to switch to it).",
          {"game": GAME, "enabled": {"type": "boolean", "description": "Default true."}, "wait_seconds": WAIT},
          ["game"]),
    _tool("set_brightness", "Set screen brightness (0-100%).", {"percent": {"type": "number"}}, ["percent"]),
    _tool("set_charge_limit", "Limit battery charging to a percentage (e.g. 80) to reduce battery wear when the "
          "Deck is often plugged in. Omit 'percent' (or use 100) to charge to 100%. In Game Mode the value is also "
          "stored in Steam's Power settings.",
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


def _steam_synced(apply, sync):
    """Apply via SteamOS Manager/sysfs, then in Game Mode also store the value in Steam's own settings;
    otherwise Steam overrides it as soon as its Performance or Power page is touched."""
    def run(value):
        result = apply(value)
        if hardware.is_game_mode():
            try:
                sync(value)
                result = {**result, "steam_ui": "updated"}
            except (RuntimeError, OSError) as e:
                result = {**result, "steam_ui": f"not updated ({e}); Steam may override this value"}
        return result
    return run


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


def _set_tdp(args):
    game = args.get("game")
    if game is not None:
        if args.get("persist"):
            raise ValueError("persist re-applies one TDP for everything after a reboot; don't combine it with 'game' "
                             "(a game's profile keeps its own TDP).")
        name = steam._perf_target(game, args.get("wait_seconds", 60))
        result = _steam_synced(hardware.set_tdp, steam.sync_tdp)(args.get("watts"))
        return {**result, "game": name}
    return _persisting(args, "tdp", args.get("watts"), _steam_synced(hardware.set_tdp, steam.sync_tdp))


def _measure_fps(seconds, game, wait_seconds):
    name = steam._perf_target(game, wait_seconds) if game is not None else None
    # mangoapp only handles frames while the overlay is shown, so show the FPS counter during the measurement.
    overlay_was_off = steam._perf_view(steam._perf_state())["overlay_level"] == 0
    if overlay_was_off:
        steam.set_perf_overlay(1)
    try:
        result = fps.measure(seconds)
    finally:
        if overlay_was_off:
            steam.set_perf_overlay(0)
    note = {"note": "The FPS counter was shown during the measurement and is off again."} if overlay_was_off else {}
    return {"game": name or "app in front", **result, **note}


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
    "set_tdp": lambda a: _set_tdp(a),
    "set_gpu_clock": lambda a: _persisting(a, "gpu_clock", a.get("mhz"), hardware.set_gpu_clock),
    "set_fps_limit": lambda a: steam.set_fps_limit(a["fps"], a.get("game"), a.get("wait_seconds", 60)),
    "perf_status": lambda a: steam.perf_status(),
    "measure_fps": lambda a: _measure_fps(a.get("seconds", 20), a.get("game"), a.get("wait_seconds", 60)),
    "recording_status": lambda a: recording.status(),
    "set_recording": lambda a: recording.set_recording(a.get("mode"), a.get("keep_minutes"), a.get("quality")),
    "save_clip": lambda a: recording.save_clip(a.get("seconds", 30), a.get("name")),
    "export_clip": lambda a: recording.export_clip(a["clip_id"], a.get("path")),
    "delete_clip": lambda a: recording.delete_clip(a["clip_id"], a.get("confirm", False)),
    "screenshot": lambda a: screen.screenshot(),
    "press_key": lambda a: screen.press_key(a["keys"], a.get("repeat", 1)),
    "press_button": lambda a: gamepad.press(a["buttons"], a.get("hold_ms", 120), a.get("together", False)),
    "move_stick": lambda a: gamepad.move_stick(a.get("stick", "left"), a.get("x", 0), a.get("y", 0),
                                               a.get("duration_ms", 500)),
    "type_text": lambda a: screen.type_text(a["text"]),
    "mouse_click": lambda a: screen.mouse_click(a["x"], a["y"], a.get("button", "left"), a.get("double", False)),
    "mouse_move": lambda a: screen.mouse_move(a.get("x"), a.get("y"), a.get("dx"), a.get("dy"),
                                              a.get("duration_ms", 300), a.get("drag")),
    "mouse_scroll": lambda a: screen.mouse_scroll(a.get("direction", "down"), a.get("amount", 3), a.get("x"), a.get("y")),
    "set_scaling": lambda a: steam.set_scaling(a.get("filter"), a.get("sharpness"), a.get("mode"), a.get("game"),
                                               a.get("wait_seconds", 60)),
    "set_perf_overlay": lambda a: steam.set_perf_overlay(a["level"]),
    "set_game_profile": lambda a: steam.set_game_profile(a["game"], a.get("enabled", True), a.get("wait_seconds", 60)),
    "set_brightness": lambda a: steam.set_brightness(a["percent"]),
    "power_settings": lambda a: _power_settings(),
    "download_status": lambda a: steam.download_status(),
    "audio_status": lambda a: system.audio_status(),
    "network_status": lambda a: system.network_status(a.get("scan", False)),
    "bluetooth_devices": lambda a: system.bluetooth_devices(),
    "top_processes": lambda a: system.top_processes(a.get("count") or 10, a.get("sort") or "cpu"),
    "list_global_compat_tools": lambda a: steam.global_compat_tool(),
    "set_charge_limit": lambda a: _persisting(a, "charge_limit", a.get("percent"),
                                              _steam_synced(steamos_manager.set_charge_limit, steam.sync_charge_limit)),
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


SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")
PROMPTS = [{
    "name": "optimize_game",
    "description": "Tune a game for the best balance of frame rate, battery life and temperature: measure, change "
                   "one setting, measure again.",
    "arguments": [{"name": "game", "description": "Game to optimise (default: the one running).", "required": False},
                  {"name": "goal", "description": "battery, balanced or performance (default: ask).",
                   "required": False}],
    "file": "optimize-game/SKILL.md",
}]


def _skill_body(path):
    with open(os.path.join(SKILLS_DIR, path)) as f:
        text = f.read()
    return text.split("---", 2)[2].strip() if text.startswith("---") else text  # drop the skill's frontmatter


def _get_prompt(params):
    prompt = next((p for p in PROMPTS if p["name"] == params.get("name")), None)
    if prompt is None:
        raise ValueError(f"Unknown prompt: {params.get('name')}")
    args = params.get("arguments") or {}
    try:
        text = _skill_body(prompt["file"])
    except OSError as e:
        raise ValueError(f"Prompt text not found ({e.filename}); is the skills folder next to the server?") from e
    request = "Optimize " + (args.get("game") or "the game that is running")
    if args.get("goal"):
        request += f" for {args['goal']}"
    return {"description": prompt["description"],
            "messages": [{"role": "user", "content": {"type": "text", "text": f"{request}.\n\n{text}"}}]}


RENAMED = {"click": "mouse_click"}  # old names keep working for existing clients


def _call_tool(params):
    name, args = params.get("name"), params.get("arguments") or {}
    name = RENAMED.get(name, name)
    if name not in HANDLERS or name not in {t["name"] for t in _visible_tools()}:
        hint = "" if name not in HANDLERS else f" at access level '{LEVEL}'"
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}{hint}"}], "isError": True}
    try:
        result = HANDLERS[name](args)
        if isinstance(result, dict) and "image" in result:
            return {"content": [{"type": "image", "data": result["image"], "mimeType": result["mime"]},
                                {"type": "text", "text": result["text"]}]}
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
            "capabilities": {"tools": {}, "prompts": {}},
            "serverInfo": {"name": "steamdeck-mcp", "version": __version__},
            "instructions": "Controls this Steam Deck: system status, games, per-game and global Proton, "
                            "downloads, TDP/GPU/CPU/charge limit, audio, Wi-Fi, Bluetooth, power, GE-Proton installs, "
                            "cleanup and game logs, plus screenshots (and keyboard/mouse input at full access). "
                            "Game/Proton tools need Steam running with the integration enabled (see "
                            f"steam_integration_status). Access level: {LEVEL}. Reply to the user in their own "
                            "language.",
        }
    if method == "tools/list":
        return {"tools": _visible_tools()}
    if method == "tools/call":
        return _call_tool(params)
    if method == "prompts/list":
        return {"prompts": [{k: v for k, v in p.items() if k != "file"} for p in PROMPTS]}
    if method == "prompts/get":
        return _get_prompt(params)
    if method == "ping":
        return {}
    raise KeyError(method)


def _set_level(argv):
    global LEVEL
    level = os.environ.get("STEAMDECK_MCP_LEVEL", "standard")
    if "--readonly" in argv:
        level = "readonly"
    if "--full" in argv:
        level = "full"
    if level not in LEVELS:
        raise SystemExit(f"Unknown access level '{level}'; use one of {', '.join(LEVELS)}.")
    LEVEL = level


def main():
    if "--apply-persistent" in sys.argv[1:]:  # run by the systemd user timer after boot
        print(json.dumps(persist.apply_all(), indent=2))
        return
    _set_level(sys.argv[1:])
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
