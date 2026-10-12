# steamdeck-mcp

**Let an AI assistant manage your Steam Deck.** steamdeck-mcp is an [MCP](https://modelcontextprotocol.io) server
that gives Claude, Copilot, Cursor or any other MCP client safe, structured control over your Deck: battery and
temperatures, TDP and charge limit, per-game Proton versions, installing and moving games, downloads, Bluetooth,
cleanup and troubleshooting game logs. In Game Mode it can also measure a game's FPS, take screenshots and, if you
allow it, press keys and click, so an assistant can tune a game's settings and check the result.

> *"How much battery do I have left and what's draining it?"*
> *"Put Cyberpunk on the latest GE-Proton and set my TDP to 10 W."*
> *"Install Hades on the SD card."*  ·  *"Why won't Hogwarts Legacy start?"*
> *"Limit charging to 80% and keep it that way after reboots."*
> *"What can I delete to free up space?"*
> *"Measure how Dorfromantik runs, then find the lowest TDP that keeps it at 45 FPS."*

- **Zero dependencies.** Pure Python standard library: no pip, no venv, nothing for a SteamOS update to break.
- **No root, no password.** Uses the same interfaces Steam itself uses (SteamOS Manager, Steam's client API).
- **Safe by default.** Destructive actions need explicit confirmation; nothing is deleted that a game still uses.
- **You choose how much it may do:** look only, change settings, or full control with keyboard and mouse input.
- **Works with any MCP client** and any model with tool calling.

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Connecting your AI client](#connecting-your-ai-client)
- [Access levels](#access-levels)
- [Tools](#tools)
- [Settings that survive a reboot](#settings-that-survive-a-reboot)
- [Safety](#safety)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Requirements

| | |
|---|---|
| Device | Steam Deck (LCD or OLED) on SteamOS 3.7 or newer. Other SteamOS devices may partly work. |
| Python | 3.12+ (SteamOS ships it, nothing to install) |
| Steam | Running, with the Steam integration enabled for game/Proton tools (one-time step below) |

Hardware, battery, audio, network and file tools work without the Steam integration.

## Installation

Run these in **Desktop Mode** (Konsole), on the Deck itself:

```bash
git clone https://github.com/mpsijben/steamdeck-mcp.git ~/steamdeck-mcp
```

That's it, there is nothing to build or install. Then:

1. **Connect your AI client** (next section).
2. **Enable the Steam integration** (once). If you use [Decky Loader](https://decky.xyz) it is already on.
   Otherwise ask your assistant to *"enable the Steam integration"*, or run:
   ```bash
   touch ~/.steam/steam/.cef-enable-remote-debugging
   ```
   and restart Steam (or reboot). This makes Steam open its local debugging port on `127.0.0.1:8080`, which is
   how the game and Proton tools talk to Steam.
3. Ask *"Give me a status overview of my Steam Deck"* to check everything works.

**Updating:** `cd ~/steamdeck-mcp && git pull`, then restart your AI client.

## Connecting your AI client

The server speaks MCP over stdio. The command is always:

```
python3 -I /home/deck/steamdeck-mcp/server.py
```

<details open>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add steamdeck-mcp -s user -- python3 -I /home/deck/steamdeck-mcp/server.py
claude mcp list   # should show: steamdeck-mcp ... ✔ Connected
```

Use the name `steamdeck-mcp`, not `steamdeck`: the [decky-claude](https://github.com/TuxLux40/decky-claude)
plugin registers its own server as `steamdeck`, which would hide this one in its sessions.
</details>

<details>
<summary><b>VS Code (GitHub Copilot)</b></summary>

Add to `.vscode/mcp.json` or your user `mcp.json`:

```json
{
  "servers": {
    "steamdeck-mcp": { "type": "stdio", "command": "python3", "args": ["-I", "/home/deck/steamdeck-mcp/server.py"] }
  }
}
```
</details>

<details>
<summary><b>Cursor, LM Studio, Claude Desktop and most other clients</b></summary>

```json
{
  "mcpServers": {
    "steamdeck-mcp": { "command": "python3", "args": ["-I", "/home/deck/steamdeck-mcp/server.py"] }
  }
}
```
</details>

<details>
<summary><b>Goose</b></summary>

`goose configure` → *Add Extension* → *Command-line Extension* → command
`python3 -I /home/deck/steamdeck-mcp/server.py`
</details>

<details>
<summary><b>Controlling the Deck from your PC (over SSH)</b></summary>

MCP over stdio works through SSH, so an AI client on your PC can manage the Deck.

1. On the Deck: set a password (`passwd`) and enable SSH: `sudo systemctl enable --now sshd`.
2. On your PC: set up key login (`ssh-copy-id deck@steamdeck`) so no password prompt is needed.
3. Use this as the server command in your client:
   ```
   ssh deck@steamdeck python3 -I /home/deck/steamdeck-mcp/server.py
   ```
</details>

## Access levels

Pick a level when you register the server by adding a flag to the command (or set `STEAMDECK_MCP_LEVEL`):

| Level | Flag | What the assistant can do |
|---|---|---|
| Read-only | `--readonly` | Look only: status, games, logs, performance, FPS measurements, screenshots |
| Standard | *(none)* | Also change settings: TDP, FPS limit, Proton, installs, downloads, power, … |
| Full | `--full` | Also **keyboard and mouse input** in Game Mode (`press_key`, `type_text`, `click`), e.g. to change a game's own graphics settings |

```bash
claude mcp add steamdeck-mcp -s user -- python3 -I /home/deck/steamdeck-mcp/server.py --full
```

Tools above your level are not offered to the assistant at all. Input tools send real key presses and clicks to
whatever is in front, so only choose `--full` if you're happy for your assistant to operate the Deck.

> **Model choice:** any model with tool calling works. Small local models (e.g. via Ollama) sometimes pick the
> wrong tool; larger models do much better.

## Tools

60 tools. Games can be referred to by appid or by (part of) their name, e.g. `"hades"`.
✏️ = changes something · ⚠️ = destructive, needs `confirm=true` or is clearly marked · 🖐️ = keyboard/mouse input,
only with `--full`.

### Status

| Tool | What it does |
|---|---|
| `system_status` | Model, SteamOS version, Game/Desktop Mode, battery (charge, health, mAh, time left), temperatures, fan, TDP and power draw, GPU, memory, disk space |
| `power_settings` | Charge limit, TDP, GPU level/clock/profile, CPU governor/scheduler/boost, fan control, Wi-Fi power saving, HDMI-CEC |
| `download_status` | Steam download queue, speed, ETA, whether downloads are paused |
| `audio_status` · `network_status` · `bluetooth_devices` | Volume, Wi-Fi/IP and nearby networks, paired devices with battery levels |
| `top_processes` | What's using CPU or memory right now |
| `steam_integration_status` | Whether the Steam integration is on and reachable |

### Games

| Tool | What it does |
|---|---|
| `list_games` · `game_info` · `running_games` | Installed Steam games and non-Steam shortcuts with Proton version, launch options, size, playtime, Deck compatibility |
| `search_owned_games` | Everything you own, installed or not |
| `install_game` ✏️ | Install on internal storage or microSD; `dry_run` shows the size first. License agreements are only accepted when you explicitly ask (`accept_eula`) |
| `uninstall_game` ⚠️ | Uninstall; warns when saves are not in Steam Cloud |
| `move_game` ✏️ | Move between internal storage and microSD |
| `storage_locations` | Libraries with free space |
| `launch_game` ✏️ · `stop_game` ⚠️ | Start or force-quit a game |
| `update_game` ✏️ · `verify_game` ✏️ · `set_downloads_paused` ✏️ | Updates, file verification, pause/resume downloads |
| `add_shortcut` ✏️ · `remove_shortcut` ⚠️ | Add a non-Steam game (optionally with Proton) or remove one (files are kept) |

### Proton & troubleshooting

| Tool | What it does |
|---|---|
| `list_compat_tools` · `list_global_compat_tools` · `list_proton_installs` | Available Proton versions and which games use them |
| `set_compat_tool` ✏️ · `set_global_compat_tool` ✏️ | Proton version per game, or the default for all Windows games |
| `set_launch_options` ✏️ | E.g. `PROTON_LOG=1 %command%` |
| `check_ge_proton` · `install_ge_proton` ✏️ · `remove_proton` ⚠️ | Latest GE-Proton, checksum-verified install, removal (refused while in use) |
| `find_game_logs` · `read_log` | Find and read a game's logs inside its Proton prefix |

### Power & hardware

| Tool | What it does |
|---|---|
| `set_tdp` ✏️ | TDP limit (3–15 W) |
| `set_gpu_clock` ✏️ | Fixed GPU clock or automatic |
| `set_charge_limit` ✏️ | Stop charging at e.g. 80% to reduce battery wear |
| `set_power_option` ✏️ | CPU governor, CPU scheduler (e.g. `lavd`), CPU boost, GPU performance level/profile, fan control, Wi-Fi power saving, HDMI-CEC (options depend on SteamOS version) |
| `set_brightness` ✏️ · `set_volume` ✏️ | Screen brightness, volume and mute |
| `screen_timeouts` · `set_screen_timeout` ✏️ | When the screen turns off / the Deck sleeps when idle, for Desktop Mode (KDE) and Game Mode (Steam), on battery and charger |
| `persistent_settings` · `forget_persistent_settings` ✏️ | See [Settings that survive a reboot](#settings-that-survive-a-reboot) |

### Performance, screen & input (Game Mode)

| Tool | What it does |
|---|---|
| `measure_fps` | Measure the game in front for N seconds: average FPS, 1% low, frame time, CPU/GPU load, temperatures, power, GPU clock. Use before and after a change |
| `perf_status` | Current game, which performance profile is active and its framerate limit |
| `set_fps_limit` ✏️ · `set_game_profile` ✏️ | Steam's framerate limit, and per-game performance profiles on/off |
| `screenshot` | What's on screen as an image, e.g. to read a game's settings menu |
| `press_key` 🖐️ · `type_text` 🖐️ · `click` 🖐️ | Keyboard and mouse input in the window in front |

Steam applies performance settings to the game that was last in front. When the assistant runs on the Deck
itself, pass `game` and switch to the game within a minute; the tool waits for it. With
[Remote Control](https://code.claude.com/docs/en/remote-control) (Claude app on your phone) the game simply stays
in front.

### System & connectivity

| Tool | What it does |
|---|---|
| `bluetooth_connect` ✏️ | Connect/disconnect a paired device by name |
| `set_airplane_mode` ✏️ | Wi-Fi and Bluetooth off/on |
| `switch_mode` ⚠️ | Switch between Game Mode and Desktop Mode |
| `power_action` ⚠️ | Suspend, restart or shut down |
| `check_os_updates` | SteamOS/Steam update check and branch |
| `enable_steam_integration` ✏️ | One-time setup (creates Steam's debugging flag file) |

### Maintenance

| Tool | What it does |
|---|---|
| `cleanup_report` | Proton prefixes and shader caches of games that are gone, with sizes (deletes nothing) |
| `delete_orphans` ⚠️ | Delete what `cleanup_report` marked as orphaned |

## Settings that survive a reboot

SteamOS forgets TDP, GPU clock, charge limit and CPU options on reboot (Steam re-applies its own values).
Pass `persist=true` to `set_tdp`, `set_gpu_clock`, `set_charge_limit` or `set_power_option`, or just say
*"...and keep it after a reboot"*, and the setting is saved to `~/.config/steamdeck-mcp/persistent.json`.

A small systemd **user** timer (`steamdeck-mcp-restore.timer`) re-applies saved settings 60 seconds after every
boot. It is installed automatically the first time you persist something, removed when nothing is saved anymore,
and lives in your home directory, so SteamOS updates don't remove it. `persistent_settings` shows what's saved;
`forget_persistent_settings` stops it.

Per-game things such as Proton versions and launch options, and screen/sleep timeouts, are stored by Steam or
KDE themselves and always persist.

## Safety

- **No passwords, no root.** The server never runs as root and never asks for or handles a password.
- **Explicit confirmation** (`confirm=true`) for `uninstall_game`, `remove_shortcut`, `switch_mode` and
  `power_action`. Most MCP clients additionally ask you before any tool runs.
- **Nothing in use gets deleted.** `delete_orphans` only removes prefixes/caches that belong to no installed game
  or shortcut, also detects shortcuts that point *into* another prefix (e.g. a launcher installed in one prefix
  and started from another), and skips prefixes with installed programs. `remove_proton` refuses versions in use.
- **License agreements** are never accepted on your behalf unless you explicitly ask for it.
- **Limits come from SteamOS.** TDP, GPU clock and charge limit are validated against the ranges SteamOS Manager
  reports for your device.
- `read_log` only reads files in your home directory or on removable media.
- **Input is opt-in.** Keyboard and mouse tools exist only at the `--full` level and are marked as non-read-only,
  so clients that ask before changes ask for every key press or click. `type_text` is capped at 500 characters.
- **Measurements leave no trace.** `measure_fps` adds its lines to the overlay's config only for the measurement,
  then removes exactly those lines and its temporary log folder.

**About the Steam integration:** Steam's debugging port listens on `127.0.0.1` only, so other devices on your
network can't reach it. Any program running on your Deck can, though; that is also true for Decky Loader, which
uses the same port. Only enable it if you're comfortable with that.

## How it works

| Feature | Interface |
|---|---|
| TDP, GPU, charge limit, CPU, fan, Wi-Fi power saving, HDMI-CEC | **SteamOS Manager** D-Bus service (`com.steampowered.SteamOSManager1`), the API Steam's own Quick Access menu uses |
| Games, installs, Proton, launch options, downloads, shortcuts, power, airplane mode, brightness | Steam's internal `SteamClient` JavaScript API, via its local debugging port |
| TDP/GPU on older SteamOS | `steamos-priv-write` (polkit-allowed helper, no password) |
| Audio, Wi-Fi, Bluetooth, processes, Game/Desktop switch | `pactl`, `nmcli`, `bluetoothctl`, `ps`, `steamos-session-select` |
| Screen/sleep timeouts | KDE `powerdevilrc` (Desktop Mode) and Steam's client settings (Game Mode) |
| Sensors, battery, disks | `/sys`, `/proc` (read-only) |
| FPS limit, performance profiles | Steam's own Performance settings (`SteamClient.System.Perf` and client settings) |
| FPS measurement | Steam's performance overlay (mangoapp) logs frames to a temporary folder for the measurement; its config file is restored afterwards |
| Screenshots | `gamescopectl screenshot` (the same capture as the screenshot button) |
| Keyboard/mouse input | `xdotool` on the Xwayland display of the window gamescope shows in front |
| GE-Proton, cleanup, logs | Files under `~/.local/share/Steam` and your Steam libraries |

**A note on stability:** `SteamClient` is Steam's internal, undocumented API (the same one Decky Loader plugins
use). A Steam update can change it and break a game/Proton tool until it's fixed here. SteamOS Manager is a
versioned interface but also evolves (SteamOS 3.8 removed one interface and added CPU boost/scheduler); the server
adapts to whatever your SteamOS version offers. Hardware, file and system tools don't depend on Steam at all.

## Troubleshooting

| Problem | Fix |
|---|---|
| *"Steam CEF debugger not reachable"* | Steam isn't running, or the integration isn't enabled: run `enable_steam_integration` and restart Steam |
| *"SharedJSContext not found"* | Steam is still starting; try again in a few seconds |
| Power tools say SteamOS Manager is unavailable | Very old SteamOS; TDP/GPU fall back to `steamos-priv-write`, other power options need SteamOS 3.6+ |
| `set_fps_limit`, `measure_fps`, `screenshot` fail | They only work in Game Mode |
| `measure_fps`: *"mangoapp wrote no log"* | Nothing was drawing frames in front (e.g. a paused or minimised game); start playing and try again |
| Input tools are missing | They are only offered with `--full` (see [Access levels](#access-levels)) |
| `move_game` fails with error 19 | Steam queued a file check after the previous move; let downloads run until it finishes |
| A tool broke after a Steam update | Please [open an issue](https://github.com/mpsijben/steamdeck-mcp/issues) with the error text |

Run the server by hand to see errors: `python3 scripts/smoke_test.py` calls every read-only tool and reports
which ones fail.

## Development

```bash
python3 -m unittest discover -s tests -v        # unit tests, run anywhere (also in CI)
python3 scripts/smoke_test.py                   # every read-only tool against a real Deck
python3 scripts/smoke_test.py game_info '{"game": "hades"}'
```

The code is plain standard-library Python:

| File | Contents |
|---|---|
| `steamdeck_mcp/server.py` | MCP protocol (JSON-RPC over stdio), tool definitions |
| `steamdeck_mcp/steam.py` | Steam client control (games, installs, downloads, shortcuts) |
| `steamdeck_mcp/cdp.py` | Minimal Chrome DevTools Protocol client for Steam's debugging port |
| `steamdeck_mcp/steamos_manager.py` | SteamOS Manager D-Bus settings |
| `steamdeck_mcp/hardware.py` | Sensors, battery, TDP/GPU |
| `steamdeck_mcp/fps.py` | FPS measurement via the performance overlay's logger |
| `steamdeck_mcp/screen.py` | Screenshots and keyboard/mouse input |
| `steamdeck_mcp/system.py` | Audio, network, Bluetooth, processes, session switching |
| `steamdeck_mcp/maintenance.py` | Libraries, Proton installs, cleanup, logs |
| `steamdeck_mcp/persist.py` | Settings re-applied after reboot |
| `steamdeck_mcp/display.py` | Screen-off and sleep timeouts |

Contributions are welcome. When adding a tool that changes something, test it on a real Deck with values that
don't change the current state where possible, and add a guard (`confirm=true`) for anything destructive.

## License

[MIT](LICENSE). Not affiliated with or endorsed by Valve. Steam, Steam Deck and SteamOS are trademarks of
Valve Corporation.
