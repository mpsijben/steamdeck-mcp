"""Flatpak apps (Discover's apps: Heroic, Lutris, emulators, browsers ...) via the `flatpak` command, plus a Steam
shortcut so they show up in Game Mode."""
import re

from . import steam
from .system import _run

APP_ID = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+){2,}$")


def _check_id(app_id):
    if not APP_ID.match(app_id or ""):
        raise ValueError(f"'{app_id}' is not a Flatpak app id (like com.heroicgameslauncher.hgl). Search first.")


def _installed():
    out = _run("flatpak", "list", "--app", "--columns=application,name,version,installation")
    rows = [line.split("\t") for line in out.splitlines() if line.strip()]
    return [{"app_id": r[0], "name": r[1], "version": r[2] or None, "scope": r[3]} for r in rows]


def _in_steam():
    """App ids that already have a Steam shortcut (flatpak run <id>)."""
    try:
        games = steam.list_games()
    except RuntimeError:
        return None
    found = set()
    for g in games:
        if g["type"] == "non-steam" and "flatpak" in (g.get("exe") or ""):
            m = re.search(r"([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){2,})\s*$", g.get("launch_options") or "")
            if m:
                found.add(m.group(1))
    return found


def apps(query=None):
    if query:
        out = _run("flatpak", "search", "--columns=application,name,description", query, timeout=60)
        rows = [line.split("\t") for line in out.splitlines() if line.strip() and "\t" in line]
        installed = {a["app_id"] for a in _installed()}
        return {"results": [{"app_id": r[0], "name": r[1], "description": r[2] if len(r) > 2 else "",
                             "installed": r[0] in installed} for r in rows[:25]]}
    rows, shortcuts = _installed(), _in_steam()
    if shortcuts is not None:
        for r in rows:
            r["in_steam"] = r["app_id"] in shortcuts
    return {"installed": rows}


def install(app_id, add_to_steam=True):
    _check_id(app_id)
    if any(a["app_id"] == app_id for a in _installed()):
        result = {"app_id": app_id, "status": "already installed"}
    else:
        _run("flatpak", "install", "--user", "-y", "--noninteractive", "flathub", app_id, timeout=1800)
        result = {"app_id": app_id, "status": "installed"}
    if add_to_steam:
        shortcuts = _in_steam()
        if shortcuts is not None and app_id in shortcuts:
            result["steam_shortcut"] = "already in Steam"
        else:
            name = next((a["name"] for a in _installed() if a["app_id"] == app_id), app_id)
            sc = steam.add_shortcut(name, "/usr/bin/flatpak", "/usr/bin", f"run {app_id}")
            result["steam_shortcut"] = {"appid": sc["appid"], "name": name}
    return result


def uninstall(app_id, confirm=False, delete_data=False):
    _check_id(app_id)
    app = next((a for a in _installed() if a["app_id"] == app_id), None)
    if not app:
        raise ValueError(f"{app_id} is not installed.")
    if not confirm:
        raise ValueError(f"Pass confirm=true to uninstall {app['name']}"
                         + (" and delete its data (settings, logins, saves inside the app)." if delete_data else
                            " (its data in ~/.var/app is kept unless delete_data=true)."))
    args = ["flatpak", "uninstall", f"--{app['scope']}", "-y", "--noninteractive"]
    _run(*args, *(["--delete-data"] if delete_data else []), app_id, timeout=300)
    return {"app_id": app_id, "status": "uninstalled", "data_deleted": bool(delete_data),
            "note": "A Steam shortcut for it is not removed automatically; use remove_shortcut."}
