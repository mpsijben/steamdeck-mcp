# Changelog

## Unreleased

- **New (full access):** `mouse_move` moves the mouse to a spot or by an amount, spread out like a trackpad swipe
  (e.g. to turn a camera), optionally dragging; `mouse_scroll` scrolls like the left trackpad.
- `click` is now `mouse_click`; the old name keeps working. 71 tools.

## 1.5.0 (2026-10-14)

- **Fix:** `measure_fps` returned no data when Steam's performance overlay was off (the default): the overlay
  only processes frames while shown. It now shows the FPS counter during the measurement and turns it off again.
- **New:** `set_scaling` sets Steam's scaling filter (linear, pixel, sharp = FSR), FSR sharpness and scaling mode.
- **New:** `set_perf_overlay` sets the performance overlay level, numbered as in Steam's Performance menu.
- **New (full access):** `move_stick` pushes an analog stick of the virtual controller; `press_button` can hold
  buttons for up to 10 seconds.
- `perf_status` also shows the overlay level and scaling settings. The optimize-game method can now try FSR.
- 69 tools.

## 1.4.0 (2026-10-12)

- **New: Steam Game Recording.** `recording_status`, `set_recording` (off / background / manual, how long to keep,
  quality), `save_clip` (the last N seconds, like the clip button), `export_clip` (MP4, default `~/Videos`) and
  `delete_clip`. Background recording uses about 3.5 GB per hour at standard quality. 66 tools.
- Client settings can now also be written as text and decimal values.

## 1.3.0 (2026-10-12)

- **New:** optimize-game method, as the MCP prompt `optimize_game` and as a Claude Code skill
  (`skills/optimize-game`): set a target FPS, lower the TDP step by step while measuring, report before/after with
  estimated playtime.
- `set_tdp` takes `game` (and `wait_seconds`) like `set_fps_limit`, so the TDP lands in that game's profile.

## 1.2.0 (2026-10-12)

Lets an assistant look at and operate a game, so it can tune settings and check the result. 61 tools.

- **New: access levels.** `--readonly` (look only), standard (default, as before) and `--full` (adds keyboard, mouse
  and controller input). Also settable with `STEAMDECK_MCP_LEVEL`. Tools above the chosen level are not offered at all.
- **New:** `measure_fps` measures the game in front with Steam's performance overlay logger: average FPS, 1% low,
  frame time, CPU/GPU load, temperatures, power and GPU clock. With `game` it waits until that game is in front.
- **New:** `screenshot` returns what's on screen as an image (Game Mode, via gamescope).
- **New (full access):** `press_key`, `type_text` and `click` send keyboard and mouse input to the window in front.
  Clicks use screenshot coordinates and are scaled to the game's own resolution.
- **New (full access):** `press_button` presses controller buttons through a virtual Xbox controller (uinput),
  for games and menus that only react to a gamepad. It is unplugged after a minute without presses, and the kernel
  removes it when the server exits.
- The server now reports itself as `steamdeck-mcp`.

## 1.1.1 (2026-10-12)

- **Fix:** `set_fps_limit` and `set_game_profile` failed after a reboot until Steam's Performance menu had been
  opened once (they used a Steam UI object that only exists from then on). They now read Steam's performance
  state directly.
- **New:** `perf_status` shows the current game, which performance profile is active and its framerate limit.

## 1.1.0 (2026-10-12)

Tested in Game Mode. 54 tools.

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
