"""Steam Game Recording: recording mode, clips and export.

Settings are Steam client settings (CMsgClientSettings, field numbers from Steam's UI code). Clips go through
Steam's own game recording store (g_GRS), the same object Steam's overlay and settings pages use.
"""
import json
import os

from .cdp import evaluate, set_client_settings

MODE_FIELD, KEEP_FIELD, QUALITY_FIELD = 18212, 18202, 18210  # gamerecording_background_mode / _max_keep / video_bitrate
MODES = {"off": 0, "background": 1, "manual": 2}
QUALITIES = {"low": "preset_low", "medium": "preset_medium", "standard": "preset_default", "high": "preset_ultra"}


def _settings():
    return evaluate("""(() => { const s = settingsStore.m_ClientSettings; return {
      mode: s.gamerecording_background_mode, keep: s.gamerecording_background_max_keep,
      quality: s.gamerecording_video_bitrate, path: s.gamerecording_background_path,
      export_dir: s.gamerecording_export_directory, max_fps: s.gamerecording_max_fps}; })()""")


def _folder_size(path):
    total = 0
    for root, _, files in os.walk(path or ""):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _clips(limit=20):
    return evaluate(f"""(async () => {{
      g_GRS.LazyLoadClips();
      for (let i = 0; i < 20 && g_GRS.BLoadingClips(); i++) await new Promise(r => setTimeout(r, 250));
      const clips = g_GRS.GetClipSummaries(g_GRS.GetClipIDs()).map(c => ({{
        clip_id: c.clip_id, game_id: String(c.game_id), name: c.name || null,
        duration_s: Math.round(Number(c.duration_ms || 0) / 1000), size_mb: Math.round(Number(c.file_size || 0) / 1e5) / 10,
        created: c.date_clipped || c.date_recorded || null }}));
      clips.sort((a, b) => (b.created || 0) - (a.created || 0));
      return clips.slice(0, {int(limit)}); }})()""")


def status():
    s = _settings()
    mode = {v: k for k, v in MODES.items()}.get(s["mode"], s["mode"])
    quality = {v: k for k, v in QUALITIES.items()}.get(s["quality"], s["quality"])
    state = evaluate("(() => { const r = g_GRS.GetRecordingState(); return r ? JSON.parse(JSON.stringify(r)) : null; })()")
    return {"mode": mode, "keep": s["keep"], "quality": quality, "max_fps": s["max_fps"],
            "recording_now": state, "storage_mb": round(_folder_size(s["path"]) / 1e6, 1),
            "recordings_folder": s["path"], "export_folder": s["export_dir"], "clips": _clips()}


def set_recording(mode=None, keep_minutes=None, quality=None):
    """Steam's Game Recording mode, how much background recording to keep, and video quality."""
    fields = {}
    if mode is not None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}.")
        fields[MODE_FIELD] = MODES[mode]
    if keep_minutes is not None:
        keep_minutes = int(keep_minutes)
        if not 10 <= keep_minutes <= 1440:
            raise ValueError("keep_minutes must be between 10 and 1440 (24 h).")
        fields[KEEP_FIELD] = f"{keep_minutes}min"
    if quality is not None:
        if quality not in QUALITIES:
            raise ValueError(f"quality must be one of {', '.join(QUALITIES)}.")
        fields[QUALITY_FIELD] = QUALITIES[quality]
    if not fields:
        raise ValueError("Pass mode, keep_minutes and/or quality.")
    set_client_settings(fields)
    result = status()
    if mode == "background":
        result["note"] = "Background recording starts with the next game launch; a game already running is not recorded."
    return result


def save_clip(seconds=30, name=None):
    """Save the last `seconds` of the running background recording as a clip, like Steam's clip button."""
    seconds = int(seconds)
    if not 5 <= seconds <= 600:
        raise ValueError("seconds must be between 5 and 600.")
    result = evaluate(f"""(async () => {{
      const gid = g_GRS.GetRecordingState()?.m_gameID;
      if (!gid) return {{error: "Nothing is being recorded. Turn on background recording (set_recording) and start a game."}};
      const handle = g_GRS.GetTimelineLoaderForGame(gid), loader = handle.loader;
      try {{
        for (let i = 0; i < 20 && !loader.BInitialized(); i++) await new Promise(r => setTimeout(r, 250));
        const timelines = loader.m_rgTimelineMetadata || [];
        if (!timelines.length) return {{error: "Steam has no timeline for the recorded game yet."}};
        const timeline = timelines[timelines.length - 1].metadata;
        const rec = timeline.recordings[timeline.recordings.length - 1];
        // End inside what is already written to disk (the recording is saved in chunks of a few seconds).
        const end = Number(rec.start_offset_ms) + Number(rec.duration_ms) - 1000;
        const start = Math.max(Number(rec.start_offset_ms), end - {seconds * 1000});
        const r = await g_GRS.SaveClip(gid, undefined, {json.dumps(name or "")} || undefined,
          {{timeline_id: timeline.timeline_id, offset_ms: String(start)}},
          {{timeline_id: timeline.timeline_id, offset_ms: String(end)}}, false, false);
        if (!r.clipSummary) return {{error: `Steam could not save the clip (EResult ${{r.result}}).`}};
        const c = r.clipSummary;
        return {{clip_id: c.clip_id, game_id: String(c.game_id), seconds: Math.round(Number(c.duration_ms) / 1000),
                 size_mb: Math.round(Number(c.file_size) / 1e5) / 10}};
      }} finally {{ handle.release(); }} }})()""", timeout=60)
    if "error" in result:
        raise RuntimeError(result["error"])
    return result


def export_clip(clip_id, path=None):
    """Export a clip as an MP4 file (default: Steam's export folder, usually ~/Videos)."""
    if not clip_id or not str(clip_id).startswith("clip_"):
        raise ValueError("Pass a clip_id from recording_status, e.g. 'clip_1455840_20261012_111901'.")
    path = path or os.path.join(_settings()["export_dir"] or os.path.expanduser("~/Videos"), f"{clip_id}.mp4")
    path = os.path.abspath(os.path.expanduser(path))
    if not path.endswith(".mp4"):
        raise ValueError("path must end in .mp4.")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    status = evaluate(f"""(async () => {{
      const r = await g_GRS.ExportClip({json.dumps(clip_id)}, {json.dumps(path)}, undefined, true);
      return {{result: r, progress: g_GRS.GetClipExportProgress({json.dumps(clip_id)}) || null}}; }})()""", timeout=300)
    progress = status.get("progress") or {}
    if status["result"] != 1 or progress.get("resultStatus") not in (None, 1):
        raise RuntimeError(f"Steam could not export the clip (result {status['result']}).")
    final = progress.get("exportPath") or path
    size = os.path.getsize(final) if os.path.exists(final) else None
    return {"clip_id": clip_id, "file": final, "size_mb": round(size / 1e6, 1) if size else None}


def delete_clip(clip_id, confirm=False):
    if not confirm:
        raise ValueError("Pass confirm=true to delete the clip (exported MP4 files are kept).")
    ok = evaluate(f"""(async () => {{ const before = g_GRS.GetClipIDs().length;
      await g_GRS.DeleteClip({json.dumps(str(clip_id))}); return g_GRS.GetClipIDs().length < before; }})()""")
    if not ok:
        raise RuntimeError(f"Clip {clip_id} was not found or not deleted.")
    return {"deleted": clip_id}
