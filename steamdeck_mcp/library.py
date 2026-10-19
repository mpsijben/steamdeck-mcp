"""More of Steam's client API: achievements, Steam Cloud, the download queue, per-game properties (updates, betas,
DLC, language, resolution, overlay), Workshop, custom artwork, screenshots, game notes and client settings.
Call signatures follow Steam's own UI code (steamui/*.js)."""
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request

from . import steam
from .cdp import CDPError

_js = steam._js

# Values Steam's game properties dialog uses (from its UI code).
AUTO_UPDATE = {"default": 0, "on_launch": 1, "high_priority": 2, "always": 3}
BACKGROUND_DOWNLOADS = {"default": 0, "allow": 1, "never": 2}
CLOUD_STATUS = {0: "invalid", 1: "disabled", 2: "unknown", 3: "synchronized", 4: "checking", 5: "out of sync",
                6: "uploading", 7: "downloading", 8: "sync failed", 9: "conflict", 10: "pending on another device"}
ARTWORK = {"grid": 0, "hero": 1, "logo": 2, "wide": 3}  # 600x900 capsule, 3840x1240 banner, logo, 920x430 header
EResultOK, EResultFileNotFound = 1, 9


def _named(mapping, value):
    return {v: k for k, v in mapping.items()}.get(value, value)


def _app(game):
    """Installed game/shortcut, or any owned game (achievements and DLC don't need it installed)."""
    try:
        return steam.resolve_app(game)
    except CDPError:
        g = steam._resolve_owned(game)
        return g["appid"], g["name"], False


def _steam_game(game, what):
    appid, name, is_shortcut = steam.resolve_app(game)
    if is_shortcut:
        raise CDPError(f"{name} is a non-Steam shortcut; {what} is only for Steam games.")
    return appid, name


def _details(appid):
    # Copied through JSON: Steam's details object is observable and can arrive empty when returned by value.
    d = _js("const d = await __details(ID); return d ? JSON.parse(JSON.stringify(d)) : null;", ID=appid)
    if not d:
        raise CDPError("Steam did not return details for this game; try again in a moment.")
    return d


# ---------- achievements ----------

def achievements(game, show="all"):
    appid, name, _ = _app(game)
    r = _js("return await SteamClient.Apps.GetMyAchievementsForApp(String(ID));", ID=appid)
    if not r or r.get("result") != EResultOK:
        raise CDPError(f"Steam could not load achievements for {name} (result {r and r.get('result')}).")
    rows = r["data"]["rgAchievements"]
    got = [a for a in rows if a["bAchieved"]]

    def row(a):
        out = {"name": a["strName"], "description": a["strDescription"] or ("(hidden)" if a["bHidden"] else "")}
        if a["flAchieved"]:  # 0 until Steam has loaded the global stats
            out["global_percent"] = round(a["flAchieved"], 1)
        if a["bAchieved"]:
            out["unlocked"] = time.strftime("%Y-%m-%d", time.localtime(a["rtUnlocked"]))
        elif a["flMaxProgress"]:
            out["progress"] = f"{a['flCurrentProgress']:g}/{a['flMaxProgress']:g}"
        return out
    pick = {"all": rows, "locked": [a for a in rows if not a["bAchieved"]], "unlocked": got}[show]
    if show == "locked":  # easiest (most players have it) first
        pick = sorted(pick, key=lambda a: -a["flAchieved"])
    return {"appid": appid, "name": name, "unlocked": len(got), "total": len(rows),
            "percent": round(100 * len(got) / len(rows)) if rows else None, "achievements": [row(a) for a in pick]}


# ---------- Steam Cloud ----------

def _cloud_row(d):
    return {"status": CLOUD_STATUS.get(d.get("eCloudStatus"), d.get("eCloudStatus")),
            "enabled": bool(d.get("bCloudEnabledForApp") and d.get("bCloudEnabledForAccount")),
            "used": d.get("strCloudBytesUsed"), "available": d.get("strCloudBytesAvailable")}


def cloud_status(game=None):
    if game is not None:
        appid, name = _steam_game(game, "Steam Cloud")
        d = _details(appid)
        if not d.get("bCloudAvailable"):
            return {"appid": appid, "name": name, "steam_cloud": "not supported by this game"}
        pending = _js("return await SteamClient.Apps.GetCloudPendingRemoteOperations(ID);", ID=appid)
        return {"appid": appid, "name": name, **_cloud_row(d),
                "pending_operations": (pending or {}).get("PendingOperations") or None}
    rows = _js(r"""
const out = [];
for (const a of appStore.allApps.filter(a => a.app_type === 1 && __installed(a.appid))) {
  const d = await __details(a.appid);
  if (d?.bCloudAvailable) out.push({appid: a.appid, name: a.display_name, d: {eCloudStatus: d.eCloudStatus,
    bCloudEnabledForApp: d.bCloudEnabledForApp, bCloudEnabledForAccount: d.bCloudEnabledForAccount,
    strCloudBytesUsed: d.strCloudBytesUsed}});
}
return out;""")
    games = [{"appid": r["appid"], "name": r["name"], **_cloud_row(r["d"])} for r in rows]
    problems = [g for g in games if g["status"] in ("out of sync", "sync failed", "conflict", "pending on another device")
                or not g["enabled"]]
    return {"games_with_cloud": len(games), "problems": problems,
            "note": "Games marked 'checking' are synced again when they start." if not problems else None}


def cloud_sync(game, action, confirm=False):
    appid, name = _steam_game(game, "Steam Cloud")
    if action == "retry":
        _js("SteamClient.Cloud.RetryAppSync(ID); return true;", ID=appid)
        return {"appid": appid, "name": name, "status": "sync retried"}
    if action not in ("keep_local", "keep_cloud"):
        raise ValueError("action must be retry, keep_local or keep_cloud.")
    if not confirm:
        other = "cloud" if action == "keep_local" else "local"
        raise ValueError(f"Pass confirm=true to keep the {action[5:]} saves of {name}; the {other} saves are "
                         "overwritten.")
    _js("SteamClient.Cloud.ResolveAppSyncConflict(ID, LOCAL); return true;", ID=appid, LOCAL=action == "keep_local")
    return {"appid": appid, "name": name, "status": f"conflict resolved ({action.replace('_', ' ')})"}


# ---------- download queue ----------

def download_queue(game, action, position=None):
    appid, name = _steam_game(game, "the download queue")
    queued = [q["appid"] for q in steam.download_status()["queued"]]
    if action == "remove":
        if appid not in queued and appid not in [q["appid"] for q in steam.download_status()["not_queued"]]:
            raise ValueError(f"{name} is not in the download list.")
        _js("SteamClient.Downloads.RemoveFromDownloadList(ID, __client()); return true;", ID=appid)
        return {"appid": appid, "name": name, "status": "removed from the download list",
                "note": "The update comes back the next time Steam checks for it (or use update_game)."}
    if appid not in queued:
        raise ValueError(f"{name} is not queued. Queue it first with update_game.")
    index = {"top": 0, "bottom": len(queued) - 1, "up": queued.index(appid) - 1,
             "down": queued.index(appid) + 1}.get(action)
    if action == "position":
        if position is None:
            raise ValueError("Pass position (1 = first).")
        index = int(position) - 1
    if index is None:
        raise ValueError("action must be top, bottom, up, down, position or remove.")
    index = max(0, min(len(queued) - 1, index))
    _js("SteamClient.Downloads.SetQueueIndex(ID, IDX, __client()); return true;", ID=appid, IDX=index)
    time.sleep(1)
    return {"appid": appid, "name": name, "queue": [q["name"] for q in steam.download_status()["queued"]]}


# ---------- per-game properties ----------

def game_properties(appid, is_shortcut=False):
    """Extra fields for game_info: update/download behaviour, language, resolution, overlay, cloud, betas, DLC."""
    d = _details(appid)
    if is_shortcut:
        return {"resolution": d.get("strResolutionOverride") or "Default", "steam_overlay": d.get("bOverlayEnabled")}
    return {
        "auto_update": _named(AUTO_UPDATE, d.get("eAutoUpdateValue")),
        "background_downloads": _named(BACKGROUND_DOWNLOADS, d.get("eBackgroundDownloads")),
        "language": (d.get("selectedLanguage") or {}).get("strShortName"),
        "languages": [l["strShortName"] for l in d.get("vecLanguages") or []],
        "resolution": d.get("strResolutionOverride") or "Default",
        "steam_overlay": d.get("bOverlayEnabled"),
        "steam_cloud": _cloud_row(d) if d.get("bCloudAvailable") else "not supported",
        "branch": d.get("strSelectedBeta") or "public",
        "branches": [b["strName"] for b in d.get("vecBetas") or []],
        "has_password_branches": d.get("bHasLockedPrivateBetas") or False,
        "dlc": [{"appid": x.get("unAppID"), "name": x.get("strName"), "enabled": x.get("bEnabled")}
                for x in d.get("vecDLC") or []],
    }


GAME_OPTIONS = ("auto_update", "background_downloads", "language", "resolution", "steam_overlay", "steam_cloud")


def _on(value):
    v = str(value).strip().lower()
    if v in ("on", "true", "1", "yes", "enabled"):
        return True
    if v in ("off", "false", "0", "no", "disabled"):
        return False
    raise ValueError(f"'{value}' should be on or off.")


def set_game_option(game, option, value):
    appid, name, is_shortcut = steam.resolve_app(game)
    d = _details(appid)
    if option in ("auto_update", "background_downloads", "language", "steam_cloud") and is_shortcut:
        raise CDPError(f"{name} is a non-Steam shortcut; Steam doesn't manage its {option.replace('_', ' ')}.")
    v = str(value).strip()
    if option == "auto_update":
        if v.lower() not in AUTO_UPDATE:
            raise ValueError(f"auto_update: choose {', '.join(AUTO_UPDATE)}.")
        _js("SteamClient.Apps.SetAppAutoUpdateBehavior(ID, V); return true;", ID=appid, V=AUTO_UPDATE[v.lower()])
    elif option == "background_downloads":
        if v.lower() not in BACKGROUND_DOWNLOADS:
            raise ValueError(f"background_downloads: choose {', '.join(BACKGROUND_DOWNLOADS)}.")
        _js("SteamClient.Apps.SetAppBackgroundDownloadsBehavior(ID, V); return true;", ID=appid,
            V=BACKGROUND_DOWNLOADS[v.lower()])
    elif option == "language":
        langs = [l["strShortName"] for l in d.get("vecLanguages") or []]
        if v.lower() not in langs:
            raise ValueError(f"{name} is available in: {', '.join(langs)}.")
        _js("SteamClient.Apps.SetAppCurrentLanguage(ID, V); return true;", ID=appid, V=v.lower())
    elif option == "resolution":
        v = {"default": "Default", "native": "Native"}.get(v.lower(), v.lower())
        if v not in ("Default", "Native") and not re.fullmatch(r"\d{3,4}x\d{3,4}", v):
            raise ValueError("resolution: default, native or WIDTHxHEIGHT such as 1280x800.")
        _js("SteamClient.Apps.SetAppResolutionOverride(ID, V); return true;", ID=appid, V=v)
    elif option in ("steam_overlay", "steam_cloud"):
        want = _on(value)
        field, fn = {"steam_overlay": ("bOverlayEnabled", "ToggleEnableSteamOverlayForApp"),
                     "steam_cloud": ("bCloudEnabledForApp", "ToggleAppSteamCloudEnabled")}[option]
        if option == "steam_cloud" and not d.get("bCloudAvailable"):
            raise CDPError(f"{name} doesn't support Steam Cloud.")
        if bool(d.get(field)) != want:  # Steam only offers a toggle
            _js(f"SteamClient.Apps.{fn}(ID); return true;", ID=appid)
    else:
        raise ValueError(f"option must be one of {', '.join(GAME_OPTIONS)}.")
    time.sleep(0.5)
    props = game_properties(appid, is_shortcut)
    note = {"language": "Steam downloads the language files with the next update.",
            "resolution": "Applies from the next launch (mainly for external displays)."}.get(option)
    return {"appid": appid, "name": name, option: props.get(option), **({"note": note} if note else {})}


def set_game_branch(game, branch=None, password=None):
    appid, name = _steam_game(game, "beta branches")
    d = _details(appid)
    if password:
        r = _js("return await SteamClient.Apps.JoinAppContentBetaByPassword(ID, PW);", ID=appid, PW=password)
    else:
        branches = [b["strName"] for b in d.get("vecBetas") or []]
        b = (branch or "public").strip()
        if b not in branches:
            raise ValueError(f"Unknown branch '{b}' for {name}. Branches: {', '.join(branches)}"
                             + (" (there are also password-protected ones: pass password)."
                                if d.get("bHasLockedPrivateBetas") else ""))
        # Steam's dialog uses an empty name for the default (public) branch.
        r = _js("return await SteamClient.Apps.JoinAppContentBeta(ID, B);", ID=appid, B="" if b == "public" else b)
    time.sleep(1)
    return {"appid": appid, "name": name, "branch": _details(appid).get("strSelectedBeta") or "public",
            "result": r, "note": "Steam downloads the branch's files as an update (see download_status)."}


def set_dlc(game, dlc, enabled):
    appid, name = _steam_game(game, "DLC")
    items = _details(appid).get("vecDLC") or []
    s = str(dlc).strip().lower()
    match = [x for x in items if str(x.get("unAppID")) == s or s in (x.get("strName") or "").lower()]
    if len(match) != 1:
        names = ", ".join(f"{x.get('strName')} ({x.get('unAppID')})" for x in (match or items))
        raise ValueError(f"{'Several' if match else 'No'} DLC of {name} match '{dlc}'. DLC: {names or 'none'}")
    x = match[0]
    _js("SteamClient.Apps.SetDLCEnabled(ID, DLC, ON); return true;", ID=appid, DLC=x["unAppID"], ON=bool(enabled))
    return {"game": name, "dlc": x.get("strName"), "enabled": bool(enabled),
            "note": "Steam installs or removes the DLC's files as an update."}


# ---------- Workshop ----------

def workshop_items(game):
    appid, name = _steam_game(game, "the Workshop")
    items = _js("return await SteamClient.Apps.GetSubscribedWorkshopItems(ID);", ID=appid) or []
    return {"appid": appid, "name": name, "count": len(items), "items": items}


def set_workshop_item(game, item, enabled):
    appid, name = _steam_game(game, "the Workshop")
    _js("SteamClient.Apps.SetWorkshopItemsDisabledLocally(ID, [ITEM], OFF); return true;", ID=appid,
        ITEM=str(item), OFF=not enabled)
    return {"game": name, "item": str(item), "enabled": bool(enabled)}


# ---------- artwork ----------

def _image(source):
    if re.match(r"https?://", source):
        if not source.startswith("https://"):
            raise ValueError("Only https:// image URLs.")
        req = urllib.request.Request(source, headers={"User-Agent": "steamdeck-mcp"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read(20_000_001)
    else:
        with open(os.path.expanduser(source), "rb") as f:
            data = f.read(20_000_001)
    if len(data) > 20_000_000:
        raise ValueError("Image is larger than 20 MB.")
    if data.startswith(b"\x89PNG"):
        return data, "png"
    if data.startswith(b"\xff\xd8"):
        return data, "jpg"
    raise ValueError("Not a PNG or JPEG image.")


def set_artwork(game, kind, source=None, clear=False):
    appid, name, _ = steam.resolve_app(game)
    if kind not in ARTWORK:
        raise ValueError(f"kind must be one of {', '.join(ARTWORK)} (grid = 600x900 cover, wide = 920x430, "
                         "hero = 3840x1240 banner, logo = transparent PNG).")
    if clear:
        _js("await SteamClient.Apps.ClearCustomArtworkForApp(ID, T); return true;", ID=appid, T=ARTWORK[kind])
        return {"appid": appid, "name": name, kind: "back to default"}
    if not source:
        raise ValueError("Pass source (a local file path or https URL of a PNG/JPEG), or clear=true.")
    data, ext = _image(source)
    _js("await SteamClient.Apps.SetCustomArtworkForApp(ID, B64, EXT, T); return true;", ID=appid,
        B64=base64.b64encode(data).decode(), EXT=ext, T=ARTWORK[kind])
    return {"appid": appid, "name": name, kind: "set", "bytes": len(data)}


# ---------- screenshots ----------

def _all_screenshots():
    return _js(r"""
const rows = await SteamClient.Screenshots.GetAllAppsLocalScreenshots();
// Shortcuts are listed by their 64-bit game id, so look the app up by m_gameid as well.
const byGameId = new Map(appStore.allApps.map(a => [String(a.m_gameid || a.appid), a]));
return rows.map(s => { const a = byGameId.get(s.strGameID) || __ov(s.nAppID);
  return {game_id: s.strGameID, appid: a?.appid ?? s.nAppID, handle: s.hHandle, created: s.nCreated,
  width: s.nWidth, height: s.nHeight, caption: s.strCaption || undefined, uploaded: s.bUploaded,
  name: a?.display_name || (s.nAppID === 7 ? "Steam" : s.strGameID)}; });""") or []


def screenshots(game=None, limit=20):
    rows = _all_screenshots()
    if game is not None:
        appid = _app(game)[0]
        rows = [r for r in rows if r["appid"] == appid]
    rows.sort(key=lambda r: -r["created"])
    for r in rows:
        r["created"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created"]))
    per_game = {}
    for r in rows:
        per_game[r["name"]] = per_game.get(r["name"], 0) + 1
    return {"count": len(rows), "per_game": per_game, "latest": rows[:limit],
            "note": "Use view_screenshot with a game_id and handle to see one."}


def _screenshot_path(game_id, handle):
    path = _js("return await SteamClient.Screenshots.GetLocalScreenshotPath(G, H);", G=str(game_id), H=int(handle))
    if not path or not os.path.isfile(path):
        raise ValueError(f"No local screenshot {game_id}/{handle}. See screenshots.")
    return path


def view_screenshot(game_id, handle):
    path = _screenshot_path(game_id, handle)
    with open(path, "rb") as f:
        data = f.read()
    mime = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
    if len(data) > 400_000 and shutil.which("ffmpeg"):  # keep large (4K) shots small, like the screenshot tool
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "small.jpg")
            r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", path, "-vf", "scale='min(1280,iw)':-2",
                                "-q:v", "4", out], capture_output=True, timeout=30)
            if r.returncode == 0 and os.path.exists(out):
                with open(out, "rb") as f:
                    data, mime = f.read(), "image/jpeg"
    return {"image": base64.b64encode(data).decode(), "mime": mime, "text": path}


def delete_screenshots(items, confirm=False):
    """items: [{game_id, handle}]"""
    if not items:
        raise ValueError("Pass the screenshots to delete as [{game_id, handle}] (see screenshots).")
    if not confirm:
        raise ValueError(f"Pass confirm=true to delete {len(items)} screenshot(s) from this Deck "
                         "(uploaded copies on Steam stay).")
    groups = {}
    for it in items:
        groups.setdefault(str(it["game_id"]), []).append(int(it["handle"]))
    req = [{"gameID": g, "rgHandles": h} for g, h in groups.items()]
    r = _js("return await SteamClient.Screenshots.DeleteLocalScreenshots(REQ);", REQ=req)
    if not r or not r.get("bSuccess"):
        failed = [req[i]["gameID"] for i in (r or {}).get("rgFailedRequestIndices", [])]
        raise RuntimeError(f"Steam could not delete some screenshots (games: {', '.join(failed) or 'unknown'}).")
    return {"deleted": len(items)}


# ---------- game notes (Steam overlay notes, synced through Steam Cloud) ----------

def _notes_file(appid, name, is_shortcut):
    # Same file names Steam's notes store uses.
    if is_shortcut:
        return "notes_shortcut_" + re.sub(r"[!-/:-@ \[\\\]^`]", "_", name)
    return f"notes_{appid}"


BBCODE_TAGS = r"p|br|b|i|u|s|strike|h[1-6]|list|olist|\*|url|img|hr|spoiler|code|quote|noparse|table|tr|td|th"


def _bbcode_to_text(s):
    s = re.sub(r"\[/p\]|\[br\]", "\n", s or "")
    return re.sub(r"\[/?(?:" + BBCODE_TAGS + r")(=[^\]]*)?\]", "", s).strip()


def _text_to_bbcode(text):
    return "".join(f"[p]{line}[/p]" for line in text.split("\n"))


def _load_notes(appid, name, is_shortcut):
    fname = _notes_file(appid, name, is_shortcut)
    r = _js(r"""
const s = await SteamClient.GameNotes.SyncToClient();
const g = await SteamClient.GameNotes.GetNotes(F, F + "_images/");
return {sync: s, result: g.result, notes: g.notes};""", F=fname)
    if r["result"] == EResultFileNotFound:
        return fname, []
    if r["result"] != EResultOK:
        raise RuntimeError(f"Steam could not read the notes (result {r['result']}).")
    return fname, json.loads(r["notes"]).get("notes", [])


def game_notes(game, action="list", title=None, text=None, note_id=None):
    appid, name, is_shortcut = _app(game)
    fname, notes = _load_notes(appid, name, is_shortcut)
    if action == "list":
        return {"game": name, "notes": [{"id": n["id"], "title": n.get("title"),
                                         "modified": time.strftime("%Y-%m-%d %H:%M",
                                                                   time.localtime(n.get("time_modified", 0))),
                                         "text": _bbcode_to_text(n.get("content"))} for n in notes]}
    now = int(time.time())
    if action == "add":
        if not text:
            raise ValueError("Pass text (and optionally title).")
        note = {"id": base36(int(time.time() * 1000)), **({"shortcut_name": name} if is_shortcut else {"appid": appid}),
                "ordinal": 0, "time_created": now, "time_modified": now, "title": title or text.split("\n")[0][:60],
                "content": _text_to_bbcode(text)}
        notes = notes + [note]
    elif action in ("update", "delete"):
        match = [n for n in notes if n["id"] == note_id]
        if not match:
            raise ValueError(f"No note with id '{note_id}'. Use action=list.")
        if action == "delete":
            notes = [n for n in notes if n["id"] != note_id]
        else:
            n = match[0]
            if title:
                n["title"] = title
            if text is not None:
                n["content"] = _text_to_bbcode(text)
            n["time_modified"] = now
    else:
        raise ValueError("action must be list, add, update or delete.")
    body = {"notes": notes, **({"shortcut_name": name} if is_shortcut else {})}
    r = _js(r"""
const r = await SteamClient.GameNotes.SaveNotes(F, BODY);
if (r === 1) await SteamClient.GameNotes.SyncToServer();
return r;""", F=fname, BODY=json.dumps(body))
    if r != EResultOK:
        raise RuntimeError(f"Steam could not save the notes (result {r}).")
    return {"game": name, "status": {"add": "note added", "update": "note updated", "delete": "note deleted"}[action],
            "notes": len(notes), "note": "Shown in the Steam overlay's Notes for this game, synced via Steam Cloud."}


def base36(n):
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = chars[r] + out
    return out or "0"


# ---------- client: online state, music, system settings ----------

def set_steam_online(online):
    _js(f"SteamClient.User.{'GoOnline' if online else 'GoOffline'}(); return true;")
    return {"steam": "going online" if online else "going offline",
            "note": "Offline mode: friends see you offline and multiplayer/store don't work; games still start."
                    if not online else None}


MUSIC = {"play_pause": "TogglePlayPause", "next": "PlayNext", "previous": "PlayPrevious",
         "mute": "ToggleMuteVolume", "volume_up": "IncreaseVolume", "volume_down": "DecreaseVolume"}


def music(action):
    if action not in MUSIC:
        raise ValueError(f"action must be one of {', '.join(MUSIC)}.")
    _js(f"SteamClient.Music.{MUSIC[action]}(); return true;")
    return {"music": action, "note": "Controls Steam's own music player (soundtracks you own)."}


def system_settings():
    return _js(r"""
const info = await SteamClient.System.GetSystemInfo();
return {timezone: await SteamClient.Settings.GetTimeZone(), language: await SteamClient.Settings.GetCurrentLanguage(),
        hostname: info.sHostname,
        languages: (await SteamClient.Settings.GetAvailableLanguages()).map(l => l.strShortName).sort()};""")


def set_system_setting(setting, value):
    v = str(value).strip()
    if setting == "timezone":
        zones = _js("return (await SteamClient.Settings.GetAvailableTimeZones()).map(z => z.timezoneID);")
        match = [z for z in zones if z.lower() == v.lower()] or [z for z in zones if v.lower() in z.lower()]
        if len(match) != 1:
            raise ValueError(f"{'Several' if match else 'No'} time zones match '{v}'"
                             + (f": {', '.join(match[:20])}" if match else "; use e.g. Europe/Amsterdam."))
        _js("SteamClient.Settings.SetTimeZone(V); return true;", V=match[0])
        v = match[0]
    elif setting == "language":
        langs = system_settings()["languages"]
        if v.lower() not in langs:
            raise ValueError(f"Languages: {', '.join(langs)}")
        _js("SteamClient.Settings.SetCurrentLanguage(V); return true;", V=v.lower())
        v = v.lower()
    elif setting == "hostname":
        if not re.fullmatch(r"[A-Za-z0-9-]{1,63}", v):
            raise ValueError("hostname: letters, digits and '-' only (max 63).")
        _js("SteamClient.Settings.SetHostname(V); return true;", V=v)
    else:
        raise ValueError("setting must be timezone, language or hostname.")
    note = {"language": "Steam shows the new language after a restart of Steam.",
            "hostname": "The name this Deck uses on your network."}.get(setting)
    return {setting: v, **({"note": note} if note else {})}
