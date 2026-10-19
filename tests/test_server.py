"""Offline tests: protocol handling and safety guards (no Steam or hardware writes needed)."""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tempfile  # noqa: E402

from steamdeck_mcp import (cdp, display, flatpak, fps, gamepad, health, library, maintenance, persist,  # noqa: E402
                           recording, screen, server, steam, steamos_manager)


def call(name, **args):
    return server._call_tool({"name": name, "arguments": args})


class ProtocolTests(unittest.TestCase):
    def test_initialize_negotiates_version(self):
        r = server._handle({"method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(r["protocolVersion"], "2024-11-05")
        r = server._handle({"method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(r["protocolVersion"], server.PROTOCOL_VERSIONS[0])

    def test_every_tool_has_a_handler(self):
        names = [t["name"] for t in server.TOOLS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), set(server.HANDLERS))

    def test_unknown_tool_is_error(self):
        self.assertTrue(call("nope")["isError"])

    def test_stdio_roundtrip(self):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "bogus"}]
        p = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "server.py")],
                           input="\n".join(map(json.dumps, msgs)) + "\n", capture_output=True, text=True, timeout=30)
        replies = [json.loads(l) for l in p.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertIn("tools", replies[1]["result"])
        self.assertEqual(replies[2]["error"]["code"], -32601)


class RecordingTests(unittest.TestCase):
    def test_recording_arguments_validated(self):
        with self.assertRaises(ValueError):
            recording.set_recording(mode="always")
        with self.assertRaises(ValueError):
            recording.set_recording(keep_minutes=5)
        with self.assertRaises(ValueError):
            recording.set_recording(quality="4k")
        with self.assertRaises(ValueError):
            recording.set_recording()
        with self.assertRaises(ValueError):
            recording.save_clip(2)

    def test_delete_and_export_validated(self):
        self.assertTrue(call("delete_clip", clip_id="clip_1")["isError"])  # no confirm
        with self.assertRaises(ValueError):
            recording.export_clip("../etc/passwd")

    def test_string_and_float_client_settings_encoding(self):
        sent = []
        orig = cdp.evaluate
        cdp.evaluate = lambda expr, **kw: sent.append(expr)
        try:
            cdp.set_client_settings({18202: "120min", 18216: 30.0, 18212: 1})
        finally:
            cdp.evaluate = orig
        import base64, re, struct
        payload = base64.b64decode(re.search(r'SetSetting\("([^"]+)"', sent[0]).group(1))
        fields = cdp.decode_fields(payload)
        self.assertEqual(fields[18202], [b"120min"])
        self.assertEqual(struct.unpack("<f", fields[18216][0])[0], 30.0)
        self.assertEqual(fields[18212], [1])


class PromptTests(unittest.TestCase):
    def test_prompts_listed_without_internal_fields(self):
        prompts = server._handle({"method": "prompts/list"})["prompts"]
        self.assertEqual([p["name"] for p in prompts], ["optimize_game"])
        self.assertTrue(all("file" not in p for p in prompts))

    def test_optimize_prompt_uses_skill_text(self):
        r = server._handle({"method": "prompts/get", "params": {"name": "optimize_game",
                                                               "arguments": {"game": "Hades", "goal": "battery"}}})
        text = r["messages"][0]["content"]["text"]
        self.assertTrue(text.startswith("Optimize Hades for battery."))
        self.assertIn("measure_fps", text)
        self.assertNotIn("description:", text.split("\n\n", 1)[1][:200])  # frontmatter stripped

    def test_skill_only_mentions_existing_tools(self):
        import re
        with open(os.path.join(ROOT, "skills", "optimize-game", "SKILL.md")) as f:
            used = set(re.findall(r"`([a-z_]+)`", f.read()))
        tools = {t["name"] for t in server.TOOLS}
        args = {"game", "persist", "power_w", "cpu_power_w", "gpu_power_w"}
        self.assertFalse({u for u in used if "_" in u} - tools - args - {"capacity_wh_full"})

    def test_unknown_prompt_is_error(self):
        with self.assertRaises(ValueError):
            server._get_prompt({"name": "nope"})


class SafetyTests(unittest.TestCase):
    def test_delete_orphans_requires_confirm(self):
        r = call("delete_orphans", appids=[123], confirm=False)
        self.assertTrue(r["isError"])
        self.assertIn("confirm", r["content"][0]["text"])

    def test_read_log_outside_home_refused(self):
        self.assertTrue(call("read_log", path="/etc/passwd")["isError"])

    def test_remove_proton_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            maintenance.remove_proton("../../steamapps")

    def test_shortcut_reference_is_not_orphan(self):
        blob = b"\x01Exe\x00/home/deck/.local/share/Steam/steamapps/compatdata/42/pfx/drive_c/x.exe\x00"
        self.assertNotEqual(maintenance._classify(42, set(), set(), blob), "orphan")
        self.assertEqual(maintenance._classify(43, set(), set(), blob), "orphan")
        self.assertEqual(maintenance._classify(0, set(), set(), b""), "system")

    def test_tdp_persist_with_game_refused(self):
        r = call("set_tdp", watts=10, persist=True, game="x")
        self.assertTrue(r["isError"])
        self.assertIn("persist", r["content"][0]["text"])

    def test_tdp_and_gpu_ranges_validated(self):
        self.assertTrue(call("set_tdp", watts=50)["isError"])
        self.assertTrue(call("set_gpu_clock", mhz=5000)["isError"])

    def test_power_and_session_require_confirm(self):
        for name, args in (("power_action", {"action": "shutdown"}), ("switch_mode", {"mode": "desktop"}),
                           ("remove_shortcut", {"game": 0})):
            r = call(name, **args)
            self.assertTrue(r["isError"], name)

    def test_unknown_power_option_rejected(self):
        with self.assertRaises(ValueError):
            steamos_manager.set_option("overclock", "max")

    @unittest.skipUnless(steamos_manager.available(), "SteamOS Manager not running")
    def test_manager_ranges_validated(self):
        self.assertTrue(call("set_charge_limit", percent=5)["isError"])
        self.assertTrue(call("set_power_option", option="fan_control", value="turbo")["isError"])

    def test_screen_timeout_needs_a_value(self):
        self.assertTrue(call("set_screen_timeout")["isError"])


class ProtobufTests(unittest.TestCase):
    def test_protobuf_varint(self):
        self.assertEqual(cdp.varint(0), b"\x00")
        self.assertEqual(cdp.varint(300), b"\xac\x02")
        self.assertEqual(len(cdp.varint(-1)), 10)

    def test_steam_field_tag(self):
        # field 24009, wire type 0 -> tag 192072
        self.assertEqual(cdp.varint(24009 << 3), b"\xc8\xdc\x0b")

    def test_backlight_dim_tag(self):
        # idle_backlight_dim_ac_seconds: field 2, wire type 5 (float)
        self.assertEqual(cdp.varint(display.STEAM_DIM_FIELDS["ac"] << 3 | 5), b"\x15")


class PersistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = persist.STATE_FILE, persist.CONFIG_DIR
        persist.CONFIG_DIR = self.tmp.name
        persist.STATE_FILE = os.path.join(self.tmp.name, "persistent.json")

    def tearDown(self):
        persist.STATE_FILE, persist.CONFIG_DIR = self.orig
        self.tmp.cleanup()

    def test_missing_state_is_empty(self):
        self.assertEqual(persist.load(), {})

    def test_forget_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            persist.forget(["tdp"])

    def test_apply_reports_failures_per_setting(self):
        persist._write({"bogus": 1})
        self.assertIn("failed", persist.apply_all()["bogus"]["status"])

    def test_without_persist_flag_nothing_is_saved(self):
        server._persisting({}, "tdp", 10, lambda v: {"tdp": v})
        self.assertEqual(persist.load(), {})


class AccessLevelTests(unittest.TestCase):
    def tearDown(self):
        server.LEVEL = "standard"

    def names(self, level):
        server.LEVEL = level
        return {t["name"] for t in server._visible_tools()}

    def test_levels_are_nested(self):
        ro, std, full = self.names("readonly"), self.names("standard"), self.names("full")
        self.assertTrue(ro < std < full)
        self.assertEqual(full, {t["name"] for t in server.TOOLS})

    def test_readonly_has_only_read_only_tools(self):
        server.LEVEL = "readonly"
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in server._visible_tools()))
        self.assertTrue(call("set_tdp", watts=10)["isError"])

    def test_every_tool_sets_all_four_hints(self):
        hints = {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
        server.LEVEL = "full"
        for t in server._visible_tools():
            with self.subTest(tool=t["name"]):
                self.assertEqual(set(t["annotations"]), hints)
                self.assertTrue(all(isinstance(v, bool) for v in t["annotations"].values()))

    def test_input_tools_only_at_full(self):
        inputs = {"press_key", "press_button", "move_stick", "type_text", "mouse_click", "mouse_move", "mouse_scroll"}
        self.assertFalse(inputs & self.names("standard"))
        self.assertTrue(inputs <= self.names("full"))
        server.LEVEL = "standard"
        self.assertIn("access level", call("click", x=1, y=1)["content"][0]["text"])

    def test_level_from_flags_and_env(self):
        server._set_level(["--full"])
        self.assertEqual(server.LEVEL, "full")
        server._set_level(["--readonly"])
        self.assertEqual(server.LEVEL, "readonly")
        os.environ["STEAMDECK_MCP_LEVEL"] = "bogus"
        try:
            with self.assertRaises(SystemExit):
                server._set_level([])
        finally:
            del os.environ["STEAMDECK_MCP_LEVEL"]

    def test_tools_list_hides_internal_fields(self):
        self.assertTrue(all("_level" not in t for t in server._handle({"method": "tools/list"})["tools"]))


class InputValidationTests(unittest.TestCase):
    def setUp(self):  # pretend a Deck-sized screenshot was taken, so no X display or xdotool is needed
        self.saved_shot = dict(screen._shot)
        screen._shot.update(image=(1280, 800), screen=(1280, 800))

    def tearDown(self):
        screen._shot.clear()
        screen._shot.update(self.saved_shot)

    def test_click_outside_screen_rejected(self):
        with self.assertRaises(ValueError):
            screen.mouse_click(1280, 10)
        with self.assertRaises(ValueError):
            screen.mouse_click(10, 10, button="side")

    def test_click_mapping_on_a_4k_tv_with_black_bars(self):
        # Screenshot scaled 3840x2160 -> 1280x720; a 1280x800 game is fitted into the 16:9 screen with side bars.
        saved = dict(screen._shot), screen._focused_window, screen._run
        screen._shot.update(image=(1280, 720), screen=(3840, 2160))
        screen._focused_window = lambda: (":1", "42")
        screen._run = lambda args, display=":0", timeout=10: subprocess.CompletedProcess(
            args, 0, "WINDOW=42\nX=0\nY=0\nWIDTH=1280\nHEIGHT=800\nSCREEN=0\n", "")
        try:
            self.assertEqual(screen._to_window(640, 360)[2:], (640, 400))   # centre stays centre
            self.assertEqual(screen._to_window(64, 0)[2:], (0, 0))          # inside the left bar -> clamped
            with self.assertRaises(ValueError):
                screen._to_window(1280, 10)                                  # outside the 1280x720 image
        finally:
            screen._shot.clear(); screen._shot.update(saved[0]); screen._focused_window, screen._run = saved[1], saved[2]

    def test_png_size(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (3840).to_bytes(4, "big") + (2160).to_bytes(4, "big")
        self.assertEqual(screen._png_size(png), (3840, 2160))
        with self.assertRaises(RuntimeError):
            screen._png_size(b"not a png at all, nope")

    def test_mouse_move_and_scroll_validated(self):
        with self.assertRaises(ValueError):
            screen.mouse_move()                      # neither x/y nor dx/dy
        with self.assertRaises(ValueError):
            screen.mouse_move(x=10, y=10, dx=5)      # both
        with self.assertRaises(ValueError):
            screen.mouse_move(dx=99999)
        with self.assertRaises(ValueError):
            screen.mouse_move(dx=10, drag="side")
        with self.assertRaises(ValueError):
            screen.mouse_scroll("sideways")
        with self.assertRaises(ValueError):
            screen.mouse_scroll("down", amount=0)

    def test_old_click_name_still_works(self):
        server.LEVEL = "full"
        try:
            r = call("click", x=5000, y=1)  # routed to mouse_click, which rejects the coordinates
            self.assertIn("x/y must be within", r["content"][0]["text"])
        finally:
            server.LEVEL = "standard"

    def test_controller_buttons_validated(self):
        self.assertEqual(gamepad._normalize("A, start up"), ["a", "menu", "up"])
        with self.assertRaises(ValueError):
            gamepad._normalize("a jump")
        with self.assertRaises(ValueError):
            gamepad._normalize("")

    def test_stick_values_validated(self):
        with self.assertRaises(ValueError):
            gamepad.move_stick("middle")
        with self.assertRaises(ValueError):
            gamepad.move_stick("left", x=1.5)

    def test_scaling_and_overlay_validated(self):
        from steamdeck_mcp import steam
        with self.assertRaises(ValueError):
            steam.set_scaling(filter="fsr")
        with self.assertRaises(ValueError):
            steam.set_scaling(sharpness=9)
        with self.assertRaises(ValueError):
            steam.set_scaling()
        with self.assertRaises(ValueError):
            steam.set_perf_overlay(5)

    def test_overlay_levels_follow_steam_menu(self):
        from steamdeck_mcp import steam
        # Steam stores "1" (FPS only) as 4 and shifts the rest down by one.
        self.assertEqual(steam.OVERLAY_LEVELS, {0: 0, 1: 4, 2: 1, 3: 2, 4: 3})

    def test_uinput_structs_match_kernel_sizes(self):
        import struct
        self.assertEqual(struct.calcsize("llHHi"), 24)      # struct input_event (64-bit)
        self.assertEqual(struct.calcsize("HH6i"), 28)       # struct uinput_abs_setup
        self.assertEqual(struct.calcsize("4H80sI"), 92)     # struct uinput_setup

    def test_empty_or_long_input_rejected(self):
        with self.assertRaises(ValueError):
            screen.press_key("  ")
        with self.assertRaises(ValueError):
            screen.type_text("x" * 501)


class FpsTests(unittest.TestCase):
    CSV = ("os,cpu,gpu,ram,kernel,driver,cpuscheduler\nSteamOS,APU,,1,6,,powersave\n"
           "fps,frametime,cpu_load,cpu_power,gpu_load,cpu_temp,gpu_temp,gpu_core_clock,gpu_mem_clock,gpu_vram_used,"
           "gpu_power,ram_used,swap_used,process_rss,cpu_mhz,elapsed\n")

    def test_summary_from_mangohud_log(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [f"60,{ft},10,3,50,60,55,1000,800,1,5,4,0,0,2400,{i * 16_000_000}" for i, ft in
                    enumerate([16.0] * 99 + [50.0])]
            with open(os.path.join(d, "mangoapp_x.csv"), "w") as f:
                f.write(self.CSV + "\n".join(rows) + "\n")
            r = fps._summary(d)
        self.assertEqual(r["frames"], 100)
        self.assertEqual(r["1%_low_fps"], 20.0)  # the one 50 ms frame
        self.assertAlmostEqual(r["average_fps"], 1000 / 16.34, places=0)
        self.assertEqual(r["gpu_power_w"], 5.0)

    def test_added_config_lines_are_removed_exactly(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "mangohud.config")
            original = "control=mangohud\npreset=1\n"
            added = ["output_folder=/x", "autostart_log=1"]
            with open(path, "w") as f:
                f.write(original + "\n" + "\n".join(added) + "\n")
            fps._remove_lines(path, set(added))
            with open(path) as f:
                self.assertEqual(f.read(), original)

    def test_seconds_validated(self):
        with self.assertRaises((ValueError, RuntimeError)):  # RuntimeError outside Game Mode
            fps.measure(1)


class HealthTests(unittest.TestCase):
    def test_used_apis_come_from_the_source(self):
        used = health.used_apis()
        self.assertIn("steam.launch_game", used["SteamClient.Apps.RunGame"])
        self.assertIn("SteamClient.Apps.GetMyAchievementsForApp", used)
        self.assertIn("appStore.allApps", used)
        self.assertFalse([k for k in used if k.startswith("SteamClient.Apps.GetAvailableCompatTools")],
                         "comment lines must not count as API use")

    def test_missing_api_is_reported_with_its_users(self):
        orig_eval, orig_mgr, orig_os = cdp.evaluate, health._manager_interfaces, health.osupdate._get
        cdp.evaluate = lambda expr, **kw: ["SteamClient.Apps.RunGame", "settingsStore.m_ClientSettings"]
        health._manager_interfaces = lambda: (["TdpLimit1"], [])
        health.osupdate._get = lambda prop: "3.8"
        try:
            r = health.check()
        finally:
            cdp.evaluate, health._manager_interfaces, health.osupdate._get = orig_eval, orig_mgr, orig_os
        self.assertFalse(r["ok"])
        self.assertEqual([m["api"] for m in r["steam_client"]["missing"]], ["SteamClient.Apps.RunGame"])
        self.assertIn("settingsStore.m_ClientSettings", r["steam_client"]["not_checked_now"]["apis"])

    def test_changed_steam_api_error_suggests_api_health(self):
        orig = steam.resolve_app
        def boom(game):
            raise cdp.CDPError("TypeError: SteamClient.Apps.Foo is not a function")
        steam.resolve_app = boom
        try:
            r = call("launch_game", game="x")
        finally:
            steam.resolve_app = orig
        self.assertTrue(r["isError"])
        self.assertIn("api_health", r["content"][0]["text"])


class LibraryTests(unittest.TestCase):
    def test_compat_tools_decoded_from_protobuf(self):
        import base64
        def msg(*fields):
            out = b""
            for n, v in fields:
                if isinstance(v, str):
                    v = v.encode()
                if isinstance(v, bytes):
                    out += cdp.varint(n << 3 | 2) + cdp.varint(len(v)) + v
                else:
                    out += cdp.varint(n << 3) + cdp.varint(v)
            return out
        body = msg((1, msg((1, "proton_11"), (2, "Proton 11.0-2"))),
                   (1, msg((1, "scout_ldlp"), (2, "Legacy"), (6, 1))),
                   (2, msg((1, "proton-stable"), (3, "proton_11"))), (3, "proton_11"), (4, "proton_10"))
        sent = []
        orig = steam._js
        steam._js = lambda b, **kw: sent.append(kw) or base64.b64encode(body).decode()
        try:
            r = steam._compat_tools(1145360)
        finally:
            steam._js = orig
        self.assertEqual(sent[0]["BYTES"], list(cdp.varint(8) + cdp.varint(1145360)))
        self.assertEqual([t["name"] for t in r["tools"]], ["proton_11", "scout_ldlp"])
        self.assertTrue(r["tools"][1]["incompatible"])
        self.assertEqual((r["selected"], r["default"], r["aliases"]), ("proton_11", "proton_10",
                                                                        {"proton-stable": "proton_11"}))

    def test_note_text_roundtrip(self):
        bb = library._text_to_bbcode("Line 1\nBoss [phase 2] tips")
        self.assertEqual(bb, "[p]Line 1[/p][p]Boss [phase 2] tips[/p]")
        self.assertEqual(library._bbcode_to_text(bb + "[b]x[/b]"), "Line 1\nBoss [phase 2] tips\nx")

    def test_destructive_library_actions_need_confirm(self):
        self.assertTrue(call("delete_screenshots", items=[{"game_id": "7", "handle": 1}])["isError"])
        with self.assertRaises(ValueError):
            library.delete_screenshots([])

    def test_settings_validated_before_steam_is_called(self):
        orig = steam._js
        steam._js = lambda *a, **kw: self.fail("Steam should not be called")
        try:
            with self.assertRaises(ValueError):
                library.set_system_setting("hostname", "bad name;rm")
            with self.assertRaises(ValueError):
                library.set_system_setting("wallpaper", "x")
            with self.assertRaises(ValueError):
                library.music("explode")
        finally:
            steam._js = orig

    def test_artwork_only_accepts_images(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            f.write(b"not an image")
            f.flush()
            with self.assertRaises(ValueError):
                library._image(f.name)
        with self.assertRaises(ValueError):
            library._image("http://example.com/x.png")  # https only

    def test_flatpak_ids_validated(self):
        for bad in ("rm -rf", "--user", "a.b", "com.example.App; reboot"):
            with self.assertRaises(ValueError):
                flatpak._check_id(bad)
        flatpak._check_id("com.heroicgameslauncher.hgl")

    def test_bool_power_option_validated(self):
        sent = []
        orig = steamos_manager.set_
        steamos_manager.set_ = lambda iface, prop, sig, value: sent.append((prop, sig, value)) or True
        try:
            self.assertEqual(steamos_manager.set_option("cec_wake_tv", "on"), {"cec_wake_tv": "on"})
            with self.assertRaises(ValueError):
                steamos_manager.set_option("cec_wake_tv", "maybe")
        finally:
            steamos_manager.set_ = orig
        self.assertEqual(sent, [("WakeTv", "b", "true")])


if __name__ == "__main__":
    unittest.main()
