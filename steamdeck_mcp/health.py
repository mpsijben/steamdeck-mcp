"""Self-check: are the undocumented Steam client APIs, SteamOS services and commands this server relies on still
there? The list is read from this package's own source, so it never falls behind the code."""
import glob
import os
import re
import shutil

from . import cdp, hardware, osupdate, steamos_manager

PKG = os.path.dirname(os.path.abspath(__file__))
STORES = r"appStore|appDetailsStore|downloadsStore|SteamUIStore|settingsStore|g_GRS"
# Objects Steam only creates once their page was opened; the code using them handles their absence.
LAZY = ("settingsStore",)
GAME_MODE_ONLY = ("SteamClient.System.Perf",)  # Steam's Performance menu API exists only in Game Mode
COMMANDS = ("busctl", "flatpak", "pactl", "nmcli", "bluetoothctl", "steamos-session-select", "gamescopectl",
            "xdotool", "ffmpeg", "mangoapp")


def _sources():
    for path in sorted(glob.glob(os.path.join(PKG, "*.py"))):
        with open(path) as f:  # comment lines may name APIs that are no longer used
            yield os.path.basename(path)[:-3], "".join(l for l in f if not l.lstrip().startswith("#"))


def used_apis():
    """{'SteamClient.Apps.RunGame': ['steam.launch_game', ...], 'appStore.allApps': [...]}"""
    used = {}
    for module, text in _sources():
        if module == "health":
            continue
        # attribute each reference to the Python function it appears in
        for chunk in re.split(r"\n(?=def )", text):
            m = re.match(r"def (\w+)", chunk)
            where = f"{module}.{m.group(1)}" if m else module
            refs = re.findall(r"\bSteamClient\.\w+\.\w+(?:\.\w+)?", chunk)
            refs += re.findall(rf"\b(?:{STORES})(?:\.\w+)?\b", chunk)
            for ref in refs:
                used.setdefault(ref, [])
                if where not in used[ref]:
                    used[ref].append(where)
    return used


def _check_steam(apis):
    names = sorted(apis)
    # Each name is checked by walking window.<a>.<b>... and testing that the last part exists.
    missing = cdp.evaluate("(() => { const names = " + repr(names).replace("'", '"') + """;
      return names.filter(n => { let o = window;
        for (const p of n.split(".")) { try { o = o == null ? undefined : o[p]; } catch (e) { o = undefined; } }
        return o === undefined; }); })()""")
    return missing


def _manager_interfaces():
    with open(os.path.join(PKG, "steamos_manager.py")) as f:
        wanted = sorted(set(re.findall(r'"([A-Z][A-Za-z]+\d)"', f.read())))
    xml = steamos_manager._busctl("introspect", "--xml-interface", steamos_manager.SERVICE, steamos_manager.PATH)
    present = set(re.findall(r'interface name="com\.steampowered\.SteamOSManager1\.(\w+)"', xml))
    return wanted, [w for w in wanted if w not in present]


def check():
    report, problems = {}, []
    apis = used_apis()
    try:
        missing = _check_steam(apis)
        optional = LAZY + (() if hardware.is_game_mode() else GAME_MODE_ONLY)
        hard = [m for m in missing if not m.startswith(optional)]
        report["steam_client"] = {"apis_checked": len(apis), "missing": [
            {"api": m, "used_by": apis[m]} for m in hard]}
        if hard:
            problems.append(f"{len(hard)} Steam client API(s) are gone or renamed after a Steam update; the tools "
                            "using them will fail until steamdeck-mcp is updated.")
        lazy = [m for m in missing if m.startswith(optional)]
        if lazy:
            report["steam_client"]["not_checked_now"] = {"apis": lazy, "why": "only exist in Game Mode or after "
                                                         "Steam opened the page that uses them"}
    except RuntimeError as e:
        report["steam_client"] = f"not checked: {e}"
    try:
        wanted, gone = _manager_interfaces()
        report["steamos_manager"] = {"interfaces_used": len(wanted), "missing": gone}
        if gone:
            problems.append("SteamOS Manager no longer offers: " + ", ".join(gone) + " (changed in a SteamOS "
                            "update); the matching power settings are skipped.")
    except RuntimeError as e:
        report["steamos_manager"] = f"not available: {e}"
    try:
        report["os_updater"] = {"version": osupdate._get("CurrentVersion")}
    except RuntimeError as e:
        report["os_updater"] = f"not available: {e}"
        problems.append("The SteamOS update service (atomupd) did not answer; os_update won't work.")
    absent = [c for c in COMMANDS if not shutil.which(c)]
    report["commands_missing"] = absent
    return {"ok": not problems, "problems": problems, **report}
