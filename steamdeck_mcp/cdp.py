"""Minimal Chrome DevTools Protocol client (stdlib only) for Steam's CEF debugger."""
import base64
import json
import os
import socket
import struct
import urllib.request
from urllib.parse import urlparse

CEF_URL = os.environ.get("STEAM_CEF_URL", "http://127.0.0.1:8080")
TARGET_TITLE = "SharedJSContext"
# Steam only opens its CEF debugger when this (empty) file exists at startup.
DEBUG_FLAG = os.path.expanduser("~/.steam/steam/.cef-enable-remote-debugging")


class CDPError(RuntimeError):
    pass


def _targets():
    try:
        with urllib.request.urlopen(CEF_URL + "/json", timeout=3) as r:
            return json.load(r)
    except OSError as e:
        hint = ("Is Steam running?" if os.path.exists(DEBUG_FLAG) else
                "Steam integration is not enabled yet: run the 'enable_steam_integration' tool, then restart Steam.")
        raise CDPError(f"Steam CEF debugger not reachable at {CEF_URL} ({e}). {hint}") from e


class _WebSocket:
    def __init__(self, url, timeout=15):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("websocket handshake failed")
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise CDPError("websocket handshake rejected: " + resp[:200].decode(errors="replace"))
        self.buf = resp.split(b"\r\n\r\n", 1)[1]

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CDPError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text):
        data = text.encode()
        hdr = bytearray([0x81])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        message = b""
        while True:
            b0, b1 = self._read(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if b1 & 0x80:
                mask = self._read(4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read(n)))
            else:
                payload = self._read(n)
            if opcode == 0x8:
                raise CDPError("websocket closed by Steam")
            if opcode == 0x9:  # ping -> ignore (Steam doesn't require pong for short sessions)
                continue
            message += payload
            if b0 & 0x80:
                return message.decode()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def evaluate(expression, timeout=20):
    """Evaluate JS in Steam's SharedJSContext; awaits promises, returns JSON value."""
    target = next((t for t in _targets() if t.get("title") == TARGET_TITLE), None)
    if not target:
        raise CDPError("SharedJSContext not found; has Steam finished starting?")
    ws = _WebSocket(target["webSocketDebuggerUrl"], timeout=timeout)
    try:
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "awaitPromise": True, "returnByValue": True},
        }))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                break
    finally:
        ws.close()
    if "error" in msg:
        raise CDPError(msg["error"].get("message", str(msg["error"])))
    res = msg["result"]
    if "exceptionDetails" in res:
        d = res["exceptionDetails"]
        raise CDPError(d.get("exception", {}).get("description") or d.get("text", "JavaScript error"))
    return res["result"].get("value")


def integration_status():
    try:
        reachable = bool(_targets())
    except CDPError:
        reachable = False
    return {"flag_file": DEBUG_FLAG, "flag_present": os.path.exists(DEBUG_FLAG), "debugger_reachable": reachable}


def enable_integration():
    """Create Steam's remote-debugging flag file (what Decky Loader also does)."""
    created = not os.path.exists(DEBUG_FLAG)
    if created:
        open(DEBUG_FLAG, "a").close()
    status = integration_status()
    if not status["debugger_reachable"]:
        status["next_step"] = "Restart Steam (or reboot / switch Game Mode <-> Desktop Mode) to activate it."
    status["created"] = created
    return status


def varint(n):
    n &= (1 << 64) - 1  # int32 negatives are encoded as 10-byte varints
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def read_varint(buf, i):
    n = shift = 0
    while True:
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, i


def decode_fields(buf):
    """Top-level fields of a protobuf message: {field_number: [values]}; varints as int, others as bytes."""
    out, i = {}, 0
    while i < len(buf):
        tag, i = read_varint(buf, i)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            value, i = read_varint(buf, i)
        elif wire == 2:
            size, i = read_varint(buf, i)
            value, i = buf[i:i + size], i + size
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value, i = buf[i:i + size], i + size
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        out.setdefault(field, []).append(value)
    return out


def set_client_settings(fields):
    """Write Steam client settings (CMsgClientSettings) the way Steam's own settings pages do.
    fields: {field_number: int}; booleans are written as 0/1."""
    msg = b"".join(varint(n << 3) + varint(int(v)) for n, v in fields.items())
    payload = base64.b64encode(msg).decode()
    evaluate(f'(async () => {{ await SteamClient.Settings.SetSetting("{payload}"); return true; }})()')
