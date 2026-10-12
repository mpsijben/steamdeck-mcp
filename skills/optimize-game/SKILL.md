---
name: optimize-game
description: Tune a Steam Deck game for the best balance of frame rate, battery life and temperature using the steamdeck-mcp tools. Use when the user asks to optimize a game, make it run smoother, save battery while playing, find the right TDP/FPS limit, or "make it run well" on their Deck. Needs Game Mode with the game running.
---

# Optimize a game on the Steam Deck

Work in measured steps: measure, change one thing, measure again. Never guess from specs. Talk to the user in
their own language and keep them posted in short lines ("Baseline: 52 FPS at 14 W. Trying 10 W...").

## 0. Before you start

1. `perf_status`: which game is current, and does it have its own performance profile? If no game is running,
   ask the user to start it (or `launch_game`) and get into real gameplay, not a menu.
2. Find out whether the game stays in front while you work:
   - **You run on the user's phone or another device** (Remote Control): the game stays in front. Good.
   - **You run on the Deck itself** (e.g. a Claude Code tile in Game Mode): Steam only applies performance
     settings to the game in front. Pass `game` to `set_fps_limit`, `set_tdp`, `set_game_profile` and
     `measure_fps`; they wait until the user has switched to the game. Tell the user to switch right after you
     call them, and to keep playing during measurements.
3. Ask for the goal if the user didn't give one, and offer a default:
   - **Battery** (default for handheld play): lowest power that holds a steady target FPS.
   - **Balanced**: a steady 40 or 45 FPS with some headroom.
   - **Performance**: highest steady FPS, power is secondary (plugged in).
   Target FPS: use the user's, otherwise 45 on OLED/90 Hz (30 or 40 for demanding games), 40 on LCD/60 Hz
   (30 for demanding games). A steady lower FPS feels better than a fluctuating higher one.
4. Note the starting point so you can restore it: `perf_status` (FPS limit, profile), `power_settings` (TDP,
   GPU clock).

## 1. Baseline

1. Ask the user to play a representative, fairly demanding scene and to stay in it for the whole session; the
   same scene each time keeps measurements comparable.
2. `measure_fps` for 20 seconds. Right after, `system_status` for total battery draw (`battery.power_w` while
   unplugged) and temperatures.
3. If two baseline runs differ by more than ~10%, the scene is too variable: ask for a steadier scene or use
   30-second measurements.

Record each run in a table: setting, average FPS, 1% low, frame time, CPU/GPU load, GPU clock, APU power
(cpu_power_w + gpu_power_w), battery draw, temperatures.

## 2. Give the game its own profile

`set_game_profile` (enabled) so changes don't affect other games. Tell the user; it is what Steam's "Use
per-game profile" toggle does.

## 3. Lock the frame rate

`set_fps_limit` to the target, then measure. Judge the result:
- **Holds the target** (average within ~3% of the limit and 1% low not far below): continue with step 4.
- **Below the target at 15 W**: the target is too high. Offer a lower target (e.g. 45 → 40 → 30) or lower
  in-game graphics (step 5).

## 4. Lower the TDP until it no longer holds

Only for the Battery and Balanced goals. With the FPS locked, lower `set_tdp` in steps and measure each step:
15 → 12 → 10 → 8 → 7 → 6 → 5 W (smaller steps near the end). Stop at the first step where the average drops
more than ~3% below the target, or the 1% low drops clearly (stutter), then go back up one step and add 1 W
of headroom for heavier scenes. Don't go below 4 W.

GPU clock: leave it on automatic. Only try `set_gpu_clock` if the user asks, and return to automatic if it
doesn't help.

## 5. In-game graphics (only with the user's consent)

If the target can't be reached, or the user wants more headroom, lower in-game settings. This needs the full
access level (input tools). Ask first, and tell the user what you'll change.
1. `screenshot` to see where you are; open the game's options with `press_button` (menu) or `press_key`
   (Escape). Prefer controller buttons; many games are made for them.
2. Change one setting at a time (resolution scale/FSR, shadows, effects first), take a `screenshot` to
   confirm, return to gameplay, and measure again.
3. Never change account, online, purchase or save/load options. If something unexpected appears, stop and ask.

## 6. Finish

1. Summarise the before/after table and the final settings. Estimate playtime:
   `battery.capacity_wh_full × (percent / 100) ÷ battery draw (W)`, e.g. "about 3 h 40 m from 85%".
2. Mention the settings live in the game's own profile, so other games keep theirs.
3. If the user doesn't like the result, restore the starting point from step 0.

## Safety and limits

- Stop and back off if APU temperature goes above 90 °C or the fan stays at maximum during a battery run.
- `measure_fps` measures whatever is in front. If the game was paused, in a menu or not in front, the numbers
  are useless: check with `screenshot` or `perf_status` and measure again.
- Don't use `persist` with TDP here: it would re-apply one TDP to everything after a reboot. The game's profile
  already remembers it.
- Steam doesn't expose a game's measured FPS outside the overlay logger, so always use `measure_fps`, not the
  FPS limit, as the result.
