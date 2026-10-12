# Changelog

## Unreleased

- **Fix:** Game Mode screen-off time now sets the value on Steam's Power page (backlight dim) instead of
  Steam's screensaver setting.
- `screen_timeouts` now also shows the Game Mode values.
- **Fix:** in Game Mode, `set_tdp` and `set_charge_limit` also store the value in Steam's own settings.
  Before, Steam's Performance and Power pages didn't show it and overrode it as soon as they were touched.
- **Fix:** `set_fps_limit` now sets Steam's own framerate limit slider (it used a gamescope atom that Steam
  ignored, on the wrong X display). With `game` it waits until that game is in front and sets its profile.
- **New:** `set_game_profile` turns Steam's per-game performance profile on or off for a game.
- **Fix:** `add_shortcut` sets the exe, start dir and launch options explicitly.

## 1.0.0 (2026-10-11)

First public release. 53 tools:

- **Status:** system overview (battery health/capacity/time left, temperatures, fan, power draw, disks),
  SteamOS power settings, downloads, audio, network, Bluetooth, top processes.
- **Games:** list/search owned games, install (internal or microSD, dry run, opt-in EULA acceptance),
  uninstall (Steam Cloud warning), move between libraries, launch/stop, update, verify files,
  add/remove non-Steam shortcuts.
- **Proton:** per-game and global Proton version, launch options, GE-Proton install (checksum-verified)
  and removal, game log discovery.
- **Power:** TDP, GPU clock, charge limit, CPU governor/scheduler/boost, fan control, Wi-Fi power saving,
  HDMI-CEC via SteamOS Manager; FPS limit (Game Mode, experimental); brightness and volume.
- **Screen:** screen-off and sleep timeouts for Desktop Mode and Game Mode, per power source.
- **Persistence:** `persist=true` re-applies power settings after reboot via a systemd user timer.
- **System:** Bluetooth connect, airplane mode, Game/Desktop Mode switch, suspend/restart/shutdown.
- **Maintenance:** orphaned prefix/shader cache report and safe deletion.
- Works on SteamOS 3.7 and 3.8; zero dependencies (Python standard library only).
