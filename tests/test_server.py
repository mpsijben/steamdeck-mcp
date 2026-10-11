"""Offline tests: protocol handling and safety guards (no Steam or hardware writes needed)."""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tempfile  # noqa: E402

from steamdeck_mcp import cdp, display, maintenance, persist, server, steamos_manager  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
