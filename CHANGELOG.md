# Changelog

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
