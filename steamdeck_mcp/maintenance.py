"""File-level maintenance: libraries, Proton versions, prefix/cache cleanup, game logs."""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request

HOME = os.path.expanduser("~")
STEAM = os.path.realpath(os.path.join(HOME, ".local/share/Steam"))
COMPAT_TOOLS_DIR = os.path.join(STEAM, "compatibilitytools.d")
GE_REPO = "GloriousEggroll/proton-ge-custom"


# ---------- libraries / shortcuts ----------

def libraries():
    """Steam library roots (folders that contain steamapps/)."""
    paths = [STEAM]
    vdf = os.path.join(STEAM, "steamapps/libraryfolders.vdf")
    try:
        for m in re.finditer(r'"path"\s+"([^"]+)"', open(vdf).read()):
            paths.append(m.group(1).replace("\\\\", "\\"))
    except OSError:
        pass
    out = []
    for p in paths:
        rp = os.path.realpath(p)
        if os.path.isdir(os.path.join(rp, "steamapps")) and rp not in out:
            out.append(rp)
    return out


def _shortcuts_raw():
    data = b""
    for f in glob.glob(os.path.join(STEAM, "userdata/*/config/shortcuts.vdf")):
        data += open(f, "rb").read()
    return data


def shortcut_appids():
    ids = set()
    for m in re.finditer(rb"\x02appid\x00(.{4})", _shortcuts_raw(), re.S | re.I):
        ids.add(int.from_bytes(m.group(1), "little"))
    return ids


def installed_appids():
    ids = set()
    for lib in libraries():
        for f in glob.glob(os.path.join(lib, "steamapps/appmanifest_*.acf")):
            ids.add(int(re.search(r"appmanifest_(\d+)\.acf", f).group(1)))
    return ids


def _compat_tool_mapping():
    """appid -> tool name from config.vdf (as last written by Steam)."""
    try:
        txt = open(os.path.join(STEAM, "config/config.vdf"), errors="replace").read()
    except OSError:
        return {}
    i = txt.find('"CompatToolMapping"')
    if i < 0:
        return {}
    block, depth = txt[i:], 0
    for j, ch in enumerate(block):
        depth += ch == "{"
        depth -= ch == "}"
        if ch == "}" and depth == 0:
            block = block[:j]
            break
    return {int(a): t for a, t in re.findall(r'"(\d+)"\s*\{\s*"name"\s*"([^"]*)"', block)}


def _du(path):
    r = subprocess.run(["du", "-sb", path], capture_output=True, text=True)
    try:
        return int(r.stdout.split()[0])
    except (IndexError, ValueError):
        return 0


# ---------- Proton ----------

def list_proton():
    custom = []
    if os.path.isdir(COMPAT_TOOLS_DIR):
        for d in sorted(os.listdir(COMPAT_TOOLS_DIR)):
            if os.path.isfile(os.path.join(COMPAT_TOOLS_DIR, d, "proton")):
                custom.append(d)
    official = []
    for lib in libraries():
        for d in glob.glob(os.path.join(lib, "steamapps/common/Proton*")):
            official.append(os.path.basename(d))
    usage = {}
    for appid, tool in _compat_tool_mapping().items():
        if tool:
            usage.setdefault(tool, []).append(appid)
    return {"custom_compatibilitytools.d": [{"name": c, "used_by_appids": usage.get(c, [])} for c in custom],
            "official_steam": sorted(set(official)),
            "global_default": _compat_tool_mapping().get(0) or "(Steam default)"}


def _github(url):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "steamdeck-mcp"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def latest_ge_proton():
    rel = _github(f"https://api.github.com/repos/{GE_REPO}/releases/latest")
    return {"tag": rel["tag_name"], "published": rel["published_at"][:10],
            "installed": os.path.isdir(os.path.join(COMPAT_TOOLS_DIR, rel["tag_name"]))}


def install_ge_proton(tag=None):
    url = f"https://api.github.com/repos/{GE_REPO}/releases/" + (f"tags/{tag}" if tag else "latest")
    rel = _github(url)
    tag = rel["tag_name"]
    dest = os.path.join(COMPAT_TOOLS_DIR, tag)
    if os.path.isdir(dest):
        return {"tag": tag, "status": "already installed", "path": dest}
    assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
    tar_name = next((n for n in assets if n.endswith(".tar.gz")), None)
    sum_name = next((n for n in assets if n.endswith(".sha512sum")), None)
    if not tar_name or not sum_name:
        raise RuntimeError(f"Release {tag} has no .tar.gz + .sha512sum assets")
    os.makedirs(COMPAT_TOOLS_DIR, exist_ok=True)
    work = os.path.join(COMPAT_TOOLS_DIR, f".install-{tag}")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        with urllib.request.urlopen(assets[sum_name], timeout=30) as r:
            expected = r.read().decode().split()[0].lower()
        tar_path = os.path.join(work, tar_name)
        h = hashlib.sha512()
        with urllib.request.urlopen(assets[tar_name], timeout=60) as r, open(tar_path, "wb") as f:
            while chunk := r.read(1 << 20):
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest() != expected:
            raise RuntimeError("Checksum mismatch; download discarded.")
        extract = os.path.join(work, "x")
        with tarfile.open(tar_path) as t:
            t.extractall(extract, filter="data")
        tops = os.listdir(extract)
        if len(tops) != 1 or not os.path.isfile(os.path.join(extract, tops[0], "proton")):
            raise RuntimeError(f"Unexpected archive layout: {tops}")
        os.rename(os.path.join(extract, tops[0]), dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"tag": tag, "status": "installed", "path": dest,
            "note": "Restart Steam (or switch Game/Desktop Mode) to see it in the compatibility list."}


def remove_proton(name):
    path = os.path.join(COMPAT_TOOLS_DIR, name)
    if "/" in name or name.startswith(".") or not os.path.isfile(os.path.join(path, "proton")):
        raise ValueError(f"'{name}' is not a custom Proton in compatibilitytools.d")
    users = [a for a, t in _compat_tool_mapping().items() if t == name]
    if users:
        raise RuntimeError(f"{name} is still assigned to appid(s) {users}. Switch them to another Proton first.")
    if subprocess.run(["pgrep", "-f", path], capture_output=True).returncode == 0:
        raise RuntimeError(f"{name} is in use by a running game.")
    size = _du(path)
    shutil.rmtree(path)
    return {"removed": name, "freed_gb": round(size / 1e9, 2)}


# ---------- cleanup ----------

def _prefix_contents(prefix):
    found = []
    for pf in ("Program Files", "Program Files (x86)"):
        d = os.path.join(prefix, "pfx/drive_c", pf)
        if os.path.isdir(d):
            found += [x for x in os.listdir(d) if x not in (
                "Common Files", "Internet Explorer", "Windows Media Player", "Windows NT", "Steam", "Microsoft.NET",
                "Windows Mail", "Windows Photo Viewer", "Windows Sidebar", "WindowsPowerShell", "Windows Defender")]
    return sorted(set(found))


def _classify(appid, installed, shortcuts, shortcut_blob):
    if appid == 0:
        return "system"
    if appid in installed:
        return "in use (Steam game)"
    if appid in shortcuts:
        return "in use (non-Steam shortcut)"
    if f"compatdata/{appid}/".encode() in shortcut_blob:
        return "in use (referenced by a shortcut)"
    return "orphan"


def cleanup_report():
    installed, shortcuts, blob = installed_appids(), shortcut_appids(), _shortcuts_raw()
    rows = []
    for lib in libraries():
        for kind in ("compatdata", "shadercache"):
            for d in glob.glob(os.path.join(lib, "steamapps", kind, "*")):
                name = os.path.basename(d)
                if not name.isdigit():
                    continue
                status = _classify(int(name), installed, shortcuts, blob)
                row = {"kind": kind, "appid": int(name), "status": status, "path": d,
                       "size_gb": round(_du(d) / 1e9, 2)}
                if kind == "compatdata" and status == "orphan":
                    row["contains"] = _prefix_contents(d)
                rows.append(row)
    orphans = [r for r in rows if r["status"] == "orphan"]
    return {
        "orphans": sorted(orphans, key=lambda r: -r["size_gb"]),
        "orphan_total_gb": round(sum(r["size_gb"] for r in orphans), 2),
        "in_use_total_gb": round(sum(r["size_gb"] for r in rows if r["status"] != "orphan"), 2),
        "note": "'orphan' = belongs to no installed game or shortcut. Check 'contains': if a program is "
                "installed there (e.g. via Battle.net/EA), it is NOT safe to delete.",
    }


def delete_orphans(appids, kinds=("compatdata", "shadercache"), confirm=False):
    if not confirm:
        raise ValueError("Pass confirm=true to actually delete (this cannot be undone).")
    installed, shortcuts, blob = installed_appids(), shortcut_appids(), _shortcuts_raw()
    done, skipped = [], []
    for appid in appids:
        status = _classify(int(appid), installed, shortcuts, blob)
        if status != "orphan":
            skipped.append({"appid": appid, "reason": status})
            continue
        for lib in libraries():
            for kind in kinds:
                d = os.path.join(lib, "steamapps", kind, str(appid))
                if os.path.isdir(d):
                    if kind == "compatdata" and _prefix_contents(d):
                        skipped.append({"appid": appid, "path": d,
                                        "reason": f"contains programs: {_prefix_contents(d)}"})
                        continue
                    size = _du(d)
                    shutil.rmtree(d)
                    done.append({"appid": appid, "path": d, "freed_gb": round(size / 1e9, 2)})
    return {"deleted": done, "skipped": skipped, "freed_total_gb": round(sum(x["freed_gb"] for x in done), 2)}


# ---------- logs ----------

def _prefixes_for(appid):
    out = []
    for lib in libraries():
        p = os.path.join(lib, "steamapps/compatdata", str(appid))
        if os.path.isdir(p):
            out.append(p)
    return out


_SKIP_LOG_DIRS = {"windows", "Cache", "Cache_Data", "GPUCache", "Code Cache", "BrowserCaches", "CEF",
                  "Local Storage", "Session Storage", "IndexedDB", "leveldb", "Telemetry"}


def find_logs(appid, days=14, extra_prefixes=()):
    cutoff = time.time() - days * 86400
    files = []
    roots = _prefixes_for(appid) + [p for p in extra_prefixes if os.path.isdir(p)]
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(os.path.join(root, "pfx/drive_c")):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_LOG_DIRS]
            for fn in filenames:
                if fn.lower().endswith((".log", ".txt")) and ("log" in fn.lower() or "log" in dirpath.lower()):
                    p = os.path.join(dirpath, fn)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    if st.st_mtime >= cutoff:
                        files.append((st.st_mtime, st.st_size, p))
    proton_log = os.path.join(HOME, f"steam-{appid}.log")
    if os.path.isfile(proton_log):
        st = os.stat(proton_log)
        files.append((st.st_mtime, st.st_size, proton_log))
    files.sort(reverse=True)
    return {
        "logs": [{"path": p, "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(m)), "size_kb": s // 1024}
                 for m, s, p in files[:40]],
        "tip": None if os.path.isfile(proton_log) else
        "No Proton log. Set launch options to 'PROTON_LOG=1 %command%' and start the game to get ~/steam-<appid>.log.",
    }


def read_log(path, lines=150, contains=None):
    rp = os.path.realpath(path)
    if not (rp.startswith(HOME + "/") or rp.startswith("/run/media/")):
        raise ValueError("Only files in your home directory or on removable media (/run/media) can be read.")
    with open(rp, errors="replace") as f:
        content = f.readlines()
    if contains:
        rx = re.compile(contains, re.I)
        content = [l for l in content if rx.search(l)]
    return "".join(content[-lines:]) or "(no matching lines)"
