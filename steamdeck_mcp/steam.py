"""Steam client control via the CEF debugger (SteamClient JS API in SharedJSContext)."""
import json
import os
import time

from . import maintenance
from .cdp import CDPError, evaluate

SHORTCUT_TYPE = 1073741824
TOOL_TYPE = 4

# Waits for app details (compat tool, launch options, size) which Steam loads lazily.
_JS_HELPERS = r"""
const __details = async (id) => {
  let d = appDetailsStore.GetAppDetails(id);
  if (d) return d;
  return await new Promise((resolve) => {
    let done = false, h = null;
    const finish = (x) => { if (done) return; done = true; try { h && h.unregister(); } catch (e) {} resolve(x); };
    try { h = SteamClient.Apps.RegisterForAppDetails(id, finish); } catch (e) { finish(null); }
    setTimeout(() => finish(appDetailsStore.GetAppDetails(id) || null), 4000);
  });
};
const __ov = (id) => appStore.GetAppOverviewByAppID(id);
const __client = () => downloadsStore.CurrentViewingRemoteClientID || "0";  // "0" = this Deck
const __installed = (id) => !!__ov(id)?.per_client_data?.some(c => c.installed && c.clientid === "0");
"""


def _js(body, **args):
    """Run an async JS body; `args` are injected as JSON constants. Body must `return` JSON-able."""
    decl = "".join(f"const {k} = {json.dumps(v)};\n" for k, v in args.items())
    return evaluate(f"(async () => {{ {_JS_HELPERS}{decl}{body} }})()")


def list_games(include_details=True):
    """Installed Steam games + non-Steam shortcuts."""
    return _js(r"""
const apps = appStore.allApps.filter(a =>
  a.app_type === SHORTCUT ||
  (a.app_type !== TOOL && a.per_client_data?.some(c => c.installed && c.clientid === "0")));
const running = new Set(SteamUIStore.RunningApps.map(a => a.appid));
const out = [];
for (const a of apps) {
  const row = {
    appid: a.appid,
    name: a.display_name,
    type: a.app_type === SHORTCUT ? "non-steam" : "steam",
    running: running.has(a.appid),
    last_played: a.rt_last_time_played ? new Date(a.rt_last_time_played * 1000).toISOString().slice(0, 10) : null,
    playtime_hours: a.minutes_playtime_forever ? Math.round(a.minutes_playtime_forever / 6) / 10 : 0,
  };
  if (WITH_DETAILS) {
    const d = await __details(a.appid);
    if (d) {
      row.compat_tool = d.strCompatToolName || "(default)";
      row.launch_options = d.strLaunchOptions || d.strShortcutLaunchOptions || "";
      if (d.lDiskUsageBytes) row.size_gb = Math.round(Number(d.lDiskUsageBytes) / 1e8) / 10;
      if (d.strInstallFolder) row.install_folder = d.strInstallFolder;
      if (a.app_type === SHORTCUT) row.exe = d.strShortcutExe;
    }
  }
  out.push(row);
}
out.sort((x, y) => x.name.localeCompare(y.name));
return out;
""", SHORTCUT=SHORTCUT_TYPE, TOOL=TOOL_TYPE, WITH_DETAILS=include_details)


def resolve_app(game):
    """Accept an appid (int/str) or a (partial) name; return (appid, name, is_shortcut)."""
    games = list_games(include_details=False)
    s = str(game).strip()
    if s.isdigit():
        for g in games:
            if g["appid"] == int(s):
                return g["appid"], g["name"], g["type"] == "non-steam"
        raise CDPError(f"No installed game or shortcut with appid {s}")
    exact = [g for g in games if g["name"].lower() == s.lower()]
    matches = exact or [g for g in games if s.lower() in g["name"].lower()]
    if not matches:
        raise CDPError(f"No game matches '{s}'. Use list_games.")
    if len(matches) > 1:
        names = ", ".join(f"{g['name']} ({g['appid']})" for g in matches)
        raise CDPError(f"Several games match '{s}': {names}. Pass the appid instead.")
    g = matches[0]
    return g["appid"], g["name"], g["type"] == "non-steam"


def game_info(game):
    appid, _, _ = resolve_app(game)
    return _js(r"""
const a = __ov(ID), d = await __details(ID);
return {
  appid: ID, name: a.display_name,
  type: a.app_type === SHORTCUT ? "non-steam" : "steam",
  running: SteamUIStore.RunningApps.some(r => r.appid === ID),
  compat_tool: d?.strCompatToolName || "(default)",
  compat_tool_display: d?.strCompatToolDisplayName || null,
  launch_options: d?.strLaunchOptions || d?.strShortcutLaunchOptions || "",
  exe: d?.strShortcutExe || null, start_dir: d?.strShortcutStartDir || null,
  install_folder: d?.strInstallFolder || null,
  size_gb: d?.lDiskUsageBytes ? Math.round(Number(d.lDiskUsageBytes) / 1e8) / 10 : null,
  build_id: d?.nBuildID || null,
  deck_compat: d?.vecDeckCompatTestResults?.length ? d.vecDeckCompatTestResults : undefined,
  playtime_hours: a.minutes_playtime_forever ? Math.round(a.minutes_playtime_forever / 6) / 10 : 0,
};
""", ID=appid, SHORTCUT=SHORTCUT_TYPE)


def list_compat_tools(game=None):
    appid = resolve_app(game)[0] if game is not None else 0
    tools = _js("return await SteamClient.Apps.GetAvailableCompatTools(ID);", ID=appid)
    return [{"name": t["strToolName"], "display_name": t["strDisplayName"]} for t in tools]


def set_compat_tool(game, tool):
    appid, name, _ = resolve_app(game)
    tool = tool or ""
    if tool:
        valid = {t["name"]: t["display_name"] for t in list_compat_tools(appid)}
        if tool not in valid:
            by_display = {v.lower(): k for k, v in valid.items()}
            if tool.lower() in by_display:
                tool = by_display[tool.lower()]
            else:
                raise CDPError(f"Unknown compat tool '{tool}'. Available: {', '.join(valid)}")
    _js("SteamClient.Apps.SpecifyCompatTool(ID, TOOL); return true;", ID=appid, TOOL=tool)
    return {"appid": appid, "name": name, "compat_tool": tool or "(default)",
            "note": "Takes effect the next time the game is launched."}


def set_launch_options(game, options):
    appid, name, is_shortcut = resolve_app(game)
    fn = "SetShortcutLaunchOptions" if is_shortcut else "SetAppLaunchOptions"
    _js(f"SteamClient.Apps.{fn}(ID, OPTS); return true;", ID=appid, OPTS=options or "")
    return {"appid": appid, "name": name, "launch_options": options or ""}


def running_games():
    return _js("return SteamUIStore.RunningApps.map(a => ({appid: a.appid, name: a.display_name}));")


def launch_game(game):
    appid, name, _ = resolve_app(game)
    _js(r"""
const a = __ov(ID);
SteamClient.Apps.RunGame(String(a.m_gameid || ID), "", -1, 100);
return true;
""", ID=appid)
    return {"appid": appid, "name": name, "status": "launch requested"}


def stop_game(game):
    appid, name, _ = resolve_app(game)
    ok = _js(r"""
const a = __ov(ID);
if (!SteamUIStore.RunningApps.some(r => r.appid === ID)) return false;
SteamClient.Apps.TerminateApp(String(a.m_gameid || ID), false);
return true;
""", ID=appid)
    return {"appid": appid, "name": name, "status": "stopped" if ok else "was not running"}


def check_os_updates():
    return _js(r"""
const r = {};
try { r.branch = await SteamClient.Updates.GetCurrentOSBranch(); } catch (e) { r.branch_error = String(e); }
try { r.check = await SteamClient.Updates.CheckForUpdates(); } catch (e) { r.check_error = String(e); }
return r;
""")


def set_brightness(percent):
    _js("SteamClient.System.Display.SetBrightness(V); return true;", V=max(0.0, min(1.0, percent / 100)))
    return {"brightness_percent": percent}


# ---------- downloads & updates ----------

def download_status():
    return _js(r"""
const o = downloadsStore.LocalDownloadOverview || {};
const name = (id) => __ov(id)?.display_name || String(id);
const item = (t) => ({
  appid: t.appid, name: name(t.appid), active: t.active, paused: t.paused,
  percent: t.overall_percent_complete ?? null,
  update_size_gb: Math.round((t.update_type_info || []).reduce((s, u) =>
    s + Math.max(0, ...(u.progress || []).map(p => Number(p.bytes_total) || 0)), 0) / 1e8) / 10,
  error: t.update_error || undefined,
});
return {
  all_downloads_paused: !!o.paused,
  current: o.update_appid ? {appid: o.update_appid, name: name(o.update_appid), state: o.update_state,
    percent: o.overall_percent_complete, speed_mb_s: Math.round((o.update_network_bytes_per_second || 0) / 1e5) / 10,
    eta_min: o.overall_estimated_time_remaining_sec > 0 ? Math.round(o.overall_estimated_time_remaining_sec / 60) : null} : null,
  throttle_kb_s: o.download_throttle_rate || 0,
  queued: downloadsStore.QueuedTransfers.map(item),
  not_queued: downloadsStore.UnqueuedTransfers.map(item),
  recently_completed: downloadsStore.CompletedTransfers.length,
};
""")


def set_downloads_paused(paused):
    _js("SteamClient.Downloads.EnableAllDownloads(!PAUSED, __client()); return true;", PAUSED=bool(paused))
    time.sleep(2)
    return {"all_downloads_paused": download_status()["all_downloads_paused"]}


def update_game(game):
    appid, name, is_shortcut = resolve_app(game)
    if is_shortcut:
        raise CDPError(f"{name} is a non-Steam shortcut; Steam cannot update it.")
    _js("SteamClient.Downloads.QueueAppUpdate(ID, __client()); SteamClient.Downloads.ResumeAppUpdate(ID, __client()); "
        "return true;", ID=appid)
    return {"appid": appid, "name": name, "status": "update queued",
            "note": "Downloads only run while downloads are not paused (see download_status)."}


def verify_game(game):
    appid, name, is_shortcut = resolve_app(game)
    if is_shortcut:
        raise CDPError(f"{name} is a non-Steam shortcut; Steam cannot verify its files.")
    _js("await SteamClient.Apps.VerifyApp(ID); return true;", ID=appid)
    return {"appid": appid, "name": name, "status": "file verification started",
            "note": "Progress shows in Steam's downloads page; broken files are re-downloaded."}


# ---------- compat / shortcuts ----------

def global_compat_tool():
    tools = _js("return await SteamClient.Settings.GetGlobalCompatTools();")
    return [{"name": t["strToolName"], "display_name": t["strDisplayName"]} for t in tools]


def set_global_compat_tool(tool):
    valid = {t["name"]: t["display_name"] for t in global_compat_tool()}
    if tool not in valid:
        by_display = {v.lower(): k for k, v in valid.items()}
        if tool.lower() not in by_display:
            raise CDPError(f"Unknown compat tool '{tool}'. Available: {', '.join(valid)}")
        tool = by_display[tool.lower()]
    _js("SteamClient.Settings.SpecifyGlobalCompatTool(T); return true;", T=tool)
    return {"global_compat_tool": tool, "note": "Used for Windows games that have no per-game override."}


def add_shortcut(name, exe, start_dir=None, launch_options="", compat_tool=None):
    exe_path = exe.strip('"')
    if not os.path.isfile(exe_path):
        raise ValueError(f"Executable not found: {exe_path}")
    start_dir = (start_dir or os.path.dirname(exe_path)).strip('"')
    # AddShortcut's extra arguments are not applied reliably across Steam versions, so set every field
    # explicitly afterwards, the way Steam's own properties dialog does, and read back what was stored.
    stored = _js(r"""
const id = await SteamClient.Apps.AddShortcut(NAME, EXE, DIR, OPTS);
for (let i = 0; i < 50 && !__ov(id); i++) await new Promise(r => setTimeout(r, 100));  // wait until listed
SteamClient.Apps.SetShortcutName(id, NAME);
SteamClient.Apps.SetShortcutExe(id, '"' + EXE + '"');
SteamClient.Apps.SetShortcutStartDir(id, '"' + DIR + '"');
SteamClient.Apps.SetShortcutLaunchOptions(id, OPTS);
if (TOOL) SteamClient.Apps.SpecifyCompatTool(id, TOOL);
await new Promise(r => setTimeout(r, 500));
const d = await __details(id);
return {appid: id, exe: d?.strShortcutExe, start_dir: d?.strShortcutStartDir,
        launch_options: d?.strLaunchOptions || d?.strShortcutLaunchOptions || ""};
""", NAME=name, EXE=exe_path, DIR=start_dir, OPTS=launch_options or "", TOOL=compat_tool or "")
    return {**stored, "name": name, "compat_tool": compat_tool or "(none / native)"}


def remove_shortcut(game, confirm=False):
    appid, name, is_shortcut = resolve_app(game)
    if not is_shortcut:
        raise CDPError(f"{name} is a Steam game, not a shortcut. Uninstall it from Steam instead.")
    if not confirm:
        raise ValueError(f"Pass confirm=true to remove the shortcut '{name}' (its files and prefix are kept).")
    _js("SteamClient.Apps.RemoveShortcut(ID); return true;", ID=appid)
    return {"removed_shortcut": name, "appid": appid, "note": "Program files and Proton prefix were not touched."}


# ---------- power / radios ----------

def power_action(action, confirm=False):
    fn = {"suspend": "SuspendPC", "restart": "RestartPC", "shutdown": "ShutdownPC"}.get(action)
    if not fn:
        raise ValueError("action must be 'suspend', 'restart' or 'shutdown'")
    if not confirm:
        raise ValueError(f"Pass confirm=true to {action} the Deck (unsaved game progress may be lost).")
    _js(f"SteamClient.System.{fn}(); return true;")
    return {"action": action, "status": "requested"}


def set_airplane_mode(enabled):
    _js("SteamClient.System.SetAirplaneMode(ON); return true;", ON=bool(enabled))
    return {"airplane_mode": bool(enabled),
            "note": "Wi-Fi and Bluetooth are off while enabled." if enabled else "Radios restored."}


# ---------- install / uninstall / move ----------

# EInstallMgrState values used by Steam's install wizard.
_WIZ = {0: "none", 1: "setup", 2: "waiting for license", 3: "free license", 4: "cd key", 5: "loading app info",
        6: "password", 7: "choose location", 8: "eula", 9: "creating apps", 10: "reading media",
        11: "change media", 12: "legacy cd keys", 13: "signup", 14: "complete", 15: "failed", 16: "canceled"}


def storage_locations():
    return _js(r"""
const fs = await SteamClient.InstallFolder.GetInstallFolders();
return fs.map(f => ({
  index: f.nFolderIndex, path: f.strFolderPath, label: f.strUserLabel || f.strDriveName || null,
  default: f.bIsDefaultFolder, mounted: f.bIsMounted, removable: !f.bIsFixed,
  free_gb: Math.round(Number(f.nFreeSpace) / 1e8) / 10, capacity_gb: Math.round(Number(f.nCapacity) / 1e8) / 10,
  games: (f.vecApps || []).length,
}));
""")


def _location_index(location):
    """'internal' / 'sdcard' / folder index / path -> Steam install folder index."""
    folders = storage_locations()
    if location is None:
        return next((f for f in folders if f["default"]), folders[0])
    loc = str(location).strip().lower()
    for f in folders:
        if loc == str(f["index"]) or loc == (f["path"] or "").lower():
            return f
    if loc in ("internal", "ssd", "intern"):
        f = next((f for f in folders if not f["removable"] and f["path"].startswith("/home")), None)
    elif loc in ("sdcard", "sd", "microsd", "external"):
        f = next((f for f in folders if f["path"].startswith("/run/media")), None)
    else:
        f = None
    if not f:
        raise ValueError(f"Unknown location '{location}'. Use 'internal', 'sdcard' or an index from storage_locations.")
    if not f["mounted"]:
        raise RuntimeError(f"{f['path']} is not mounted.")
    return f


def search_owned_games(query="", installed=None, limit=50):
    rows = _js(r"""
const q = QUERY.toLowerCase();
return appStore.allApps
  .filter(a => a.app_type === 1 && a.display_name.toLowerCase().includes(q))
  .map(a => ({appid: a.appid, name: a.display_name, installed: __installed(a.appid),
              playtime_hours: a.minutes_playtime_forever ? Math.round(a.minutes_playtime_forever / 6) / 10 : 0}))
  .sort((x, y) => x.name.localeCompare(y.name));
""", QUERY=query or "")
    if installed is not None:
        rows = [r for r in rows if r["installed"] == installed]
    return {"count": len(rows), "games": rows[:limit]}


def _resolve_owned(game):
    s = str(game).strip()
    games = search_owned_games("" if s.isdigit() else s, limit=10_000)["games"]
    if s.isdigit():
        match = [g for g in games if g["appid"] == int(s)]
    else:
        match = [g for g in games if g["name"].lower() == s.lower()] or games
    if not match:
        raise CDPError(f"No owned game matches '{s}'. Use search_owned_games.")
    if len(match) > 1:
        raise CDPError(f"Several owned games match '{s}': "
                       + ", ".join(f"{g['name']} ({g['appid']})" for g in match[:15]) + ". Pass the appid instead.")
    return match[0]


def _wizard_state():
    return _js(r"""
const x = await SteamClient.Installs.GetInstallManagerInfo();
return {state: x.eInstallState, appid: x.currentAppID, required: Number(x.nDiskSpaceRequired),
        error: x.eAppError, detail: x.errorDetail};
""")


def _wait_wizard(states, timeout=30, leave=None):
    """Wait until the wizard reaches one of `states` (after first leaving state `leave`, if given)."""
    end = time.time() + timeout
    while True:
        w = _wizard_state()
        if (w["state"] in states and w["state"] != leave) or time.time() > end:
            return w
        time.sleep(0.5)


def _accept_eulas(appid):
    """Accept every EULA Steam lists for the game (what Steam's own dialog does on 'I agree')."""
    return _js(r"""
const eulas = (await SteamClient.Apps.LoadEula(ID)) || [];
for (const e of eulas) SteamClient.Apps.MarkEulaAccepted(ID, e.id, e.version);
return eulas.map(e => ({id: e.id, version: e.version, url: (e.url || "").replace("http://", "https://")}));
""", ID=appid)


def install_game(game, location=None, dry_run=False, accept_eula=False):
    g = _resolve_owned(game)
    if g["installed"]:
        raise CDPError(f"{g['name']} is already installed.")
    folder = _location_index(location)
    result = {"appid": g["appid"], "name": g["name"], "location": folder["path"], "free_gb": folder["free_gb"]}
    _js("SteamClient.Installs.OpenInstallWizard([ID]); return true;", ID=g["appid"])
    # Steam's wizard steps (location choice, EULA) can come in either order; handle each until it completes.
    w, previous = _wait_wizard({7, 8, 14, 15, 16}), None
    for _ in range(10):
        if w["state"] == 14:
            break
        if w["state"] == 7:
            result["required_gb"] = round(w["required"] / 1e9, 2)
            if dry_run or w["required"] > folder["free_gb"] * 1e9:
                _js("SteamClient.Installs.CancelInstall(); return true;")
                if not dry_run:
                    raise RuntimeError(f"Not enough space on {folder['path']}: needs {result['required_gb']} GB, "
                                       f"{folder['free_gb']} GB free.")
                eulas = _js("return ((await SteamClient.Apps.LoadEula(ID)) || []).length;", ID=g["appid"])
                return {**result, "status": "dry run, nothing installed", "has_eula": bool(eulas)}
            _js("await SteamClient.Installs.SetInstallFolder(IDX); await SteamClient.Installs.ContinueInstall(); "
                "return true;", IDX=folder["index"])
        elif w["state"] == 8:
            if dry_run:
                _js("SteamClient.Installs.CancelInstall(); return true;")
                return {**result, "status": "dry run, nothing installed", "has_eula": True,
                        "required_gb": round(w["required"] / 1e9, 2) if w["required"] else None}
            if not accept_eula:
                urls = _js("return ((await SteamClient.Apps.LoadEula(ID)) || []).map(e => e.url);", ID=g["appid"])
                return {**result, "status": "needs EULA", "eula_urls": urls,
                        "note": "This game asks you to accept a license agreement. The install window is open in "
                                "Steam: accept or decline it there, or ask again with accept_eula=true."}
            result["eulas_accepted"] = _accept_eulas(g["appid"])
            _js("await SteamClient.Installs.ContinueInstall(); return true;")
        else:
            if w["state"] != 16:
                _js("SteamClient.Installs.CancelInstall(); return true;")
            raise RuntimeError(f"Install stopped at '{_WIZ.get(w['state'], w['state'])}' "
                               f"(error {w['error']}: {w['detail'] or 'none'}).")
        previous = w["state"]
        w = _wait_wizard({7, 8, 14, 15, 16}, leave=previous)
    if w["state"] != 14:
        raise RuntimeError(f"Install wizard did not finish (last step: '{_WIZ.get(w['state'], w['state'])}').")
    paused = download_status()["all_downloads_paused"]
    return {**result, "status": "queued for download",
            "note": "Downloads are paused; resume them with set_downloads_paused(false)." if paused else
                    "Downloading; follow progress with download_status."}


def uninstall_game(game, confirm=False):
    appid, name, is_shortcut = resolve_app(game)
    if is_shortcut:
        raise CDPError(f"{name} is a non-Steam shortcut; use remove_shortcut instead.")
    info = _js(r"""
const d = await __details(ID);
return {running: SteamUIStore.RunningApps.some(r => r.appid === ID),
        cloud: !!(d?.bCloudAvailable && d?.bCloudEnabledForApp && d?.bCloudEnabledForAccount),
        size_gb: d?.lDiskUsageBytes ? Math.round(Number(d.lDiskUsageBytes) / 1e8) / 10 : null};
""", ID=appid)
    if info["running"]:
        raise RuntimeError(f"{name} is running; stop it first.")
    warning = None if info["cloud"] else ("Steam Cloud is not active for this game: local save files may be deleted "
                                          "together with the game.")
    if not confirm:
        raise ValueError(f"Pass confirm=true to uninstall {name} ({info['size_gb']} GB)."
                         + (f" WARNING: {warning}" if warning else " Saves are backed up in Steam Cloud."))
    _js("SteamClient.Installs.OpenUninstallWizard([ID], true); return true;", ID=appid)
    for _ in range(120):
        time.sleep(0.5)
        if not _js("return __installed(ID);", ID=appid):
            return {"appid": appid, "name": name, "status": "uninstalled", "freed_gb": info["size_gb"],
                    "warning": warning}
    return {"appid": appid, "name": name, "status": "uninstall requested (still in progress)", "warning": warning}


def _manifest_libraries(appid):
    """Library roots that hold this game's appmanifest (Steam's own record of where it is installed)."""
    return [lib for lib in maintenance.libraries()
            if os.path.isfile(os.path.join(lib, "steamapps", f"appmanifest_{appid}.acf"))]


def move_game(game, location, wait_seconds=120):
    appid, name, is_shortcut = resolve_app(game)
    if is_shortcut:
        raise CDPError(f"{name} is a non-Steam shortcut; Steam can only move its own games.")
    target = _location_index(location)
    target_lib = os.path.realpath(target["path"])
    if _manifest_libraries(appid) == [target_lib]:
        return {"appid": appid, "name": name, "status": f"already on {target['path']}"}
    if _js("return SteamUIStore.RunningApps.some(r => r.appid === ID);", ID=appid):
        raise RuntimeError(f"{name} is running; stop it first.")
    # Steam reports move progress through a callback: eError 20 = in progress, 0 = done, anything else = failed.
    # The listener stores events on window so progress can be polled across short CDP calls.
    _js(r"""
window.__mcpMoves = window.__mcpMoves || {};
if (!window.__mcpMoveHook)
  window.__mcpMoveHook = SteamClient.InstallFolder.RegisterForMoveContentProgress(p => {
    if (p.appid) window.__mcpMoves[p.appid] = {error: p.eError, percent: Math.round(p.flProgress)};
  });
delete window.__mcpMoves[ID];
await SteamClient.InstallFolder.MoveInstallFolderForApp(ID, IDX);
return true;
""", ID=appid, IDX=target["index"])
    start, last = time.time(), None
    while time.time() - start < wait_seconds:
        time.sleep(1)
        last = _js("return (window.__mcpMoves || {})[ID] || null;", ID=appid)
        if last is None and time.time() - start > 15:
            raise RuntimeError(f"Steam did not start moving {name}. It may still be finishing an install/update; "
                               "try again in a minute.")
        if last and last["error"] != 20:
            if last["error"] == 0:
                return {"appid": appid, "name": name, "status": "moved", "location": target["path"],
                        "note": "Steam may queue a quick file check afterwards; it runs when downloads are not "
                                "paused."}
            queued = any(q["appid"] == appid for q in download_status()["queued"])
            raise RuntimeError(f"Steam failed to move {name} (error code {last['error']})."
                               + (" It has a pending update/file check in the download queue (Steam adds one "
                                  "after every move); resume downloads, let it finish, then try again."
                                  if queued else ""))
    return {"appid": appid, "name": name, "status": "move still in progress",
            "percent": last["percent"] if last else 0, "location": target["path"],
            "note": "Large games take a while; call move_game again later to check (it reports 'already on ...')."}
