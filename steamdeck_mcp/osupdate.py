"""SteamOS updates through atomupd (com.steampowered.Atomupd1 on the system bus), the update service Steam's
own System settings use. Accessed through `busctl --system`."""
import json
import subprocess

SERVICE = "com.steampowered.Atomupd1"
PATH = "/com/steampowered/Atomupd1"
IFACE = SERVICE
STATUS = {0: "idle", 1: "in progress", 2: "paused", 3: "done, restart to finish", 4: "failed", 5: "cancelled"}


def _busctl(*args, timeout=60):
    try:
        r = subprocess.run(["busctl", "--system", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise RuntimeError("busctl not found") from e
    if r.returncode != 0:
        raise RuntimeError(f"SteamOS updater refused: {r.stderr.strip()}")
    return json.loads(r.stdout)["data"] if r.stdout.strip() else None


def _get(prop):
    return _busctl("get-property", "--json=short", SERVICE, PATH, IFACE, prop)


def status():
    code = _get("UpdateStatus")
    out = {"version": _get("CurrentVersion"), "build": _get("CurrentBuildID"), "branch": _get("Branch"),
           "known_branches": _get("KnownBranches"), "update_status": STATUS.get(code, code)}
    if code in (1, 2, 3):
        out.update(update_version=_get("UpdateVersion"), update_build=_get("UpdateBuildID"),
                   percent=round(_get("ProgressPercentage"), 1))
        eta = _get("EstimatedCompletionTime")
        if code == 1 and eta:
            out["estimated_completion"] = eta
    if code == 4:
        out.update(failure_code=_get("FailureCode"), failure_message=_get("FailureMessage"))
    return out


def check():
    """Ask the update server; returns the newest update on this branch (and later ones, if any)."""
    available, later = _busctl("call", "--json=short", "--timeout=60", SERVICE, PATH, IFACE, "CheckForUpdates",
                               "a{sv}", "0")

    def rows(d):
        return [{"id": k, **{f: v.get("data") for f, v in info.items()}} for k, info in d.items()]
    return {**status(), "available": rows(available), "later": rows(later),
            "note": None if available else "SteamOS is up to date on this branch."}


def install(confirm=False):
    found = check()
    if not found["available"]:
        return found
    update = found["available"][0]
    if not confirm:
        raise ValueError(f"Pass confirm=true to download and install SteamOS {update.get('version') or update['id']}. "
                         "It installs in the background; a restart finishes it.")
    _busctl("call", SERVICE, PATH, IFACE, "StartUpdate", "s", update["id"])
    return {**status(), "installing": update,
            "note": "Downloading and installing in the background; follow it with os_update action=status, then "
                    "restart (power_action) when it says done."}


def cancel():
    _busctl("call", SERVICE, PATH, IFACE, "CancelUpdate")
    return status()


def switch_branch(branch, confirm=False):
    branches = _get("KnownBranches")
    if branch not in branches:
        raise ValueError(f"Unknown branch '{branch}'. Branches: {', '.join(branches)}")
    if not confirm:
        raise ValueError(f"Pass confirm=true to switch SteamOS to the '{branch}' branch. Branches other than "
                         "'stable' get less tested updates; switching back only takes effect with the next update.")
    _busctl("call", SERVICE, PATH, IFACE, "SwitchToBranch", "s", branch)
    return {**status(), "note": "Use os_update action=check to see the update this branch offers."}
