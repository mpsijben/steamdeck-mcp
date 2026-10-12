"""A virtual Xbox 360 controller (Linux uinput) for pressing controller buttons in games.

Games and Steam see it as a second controller, the same kind of device Steam itself creates for games. It is
created on first use, kept while presses follow each other (so it is not re-plugged for every press) and
unplugged after a minute without presses. The kernel also removes it when the server exits, even on a crash.
Needs write access to /dev/uinput, which SteamOS grants the desktop user.
"""
import atexit
import fcntl
import os
import struct
import threading
import time

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0
BUS_USB = 0x03
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT = 0x40045564, 0x40045565, 0x40045567
UI_DEV_SETUP, UI_ABS_SETUP, UI_DEV_CREATE, UI_DEV_DESTROY = 0x405C5503, 0x401C5504, 0x5501, 0x5502

BUTTONS = {
    "a": 0x130, "b": 0x131, "x": 0x133, "y": 0x134, "lb": 0x136, "rb": 0x137,
    "view": 0x13A, "menu": 0x13B, "guide": 0x13C, "ls": 0x13D, "rs": 0x13E,
}
ALIASES = {"start": "menu", "select": "view", "back": "view", "l1": "lb", "r1": "rb", "l3": "ls", "r3": "rs"}
DPAD = {"up": (0x11, -1), "down": (0x11, 1), "left": (0x10, -1), "right": (0x10, 1)}  # ABS_HAT0Y / ABS_HAT0X
TRIGGERS = {"lt": 0x02, "rt": 0x05, "l2": 0x02, "r2": 0x05}  # ABS_Z / ABS_RZ
AXES = {0x00: (-32768, 32767), 0x01: (-32768, 32767), 0x03: (-32768, 32767), 0x04: (-32768, 32767),
        0x02: (0, 255), 0x05: (0, 255), 0x10: (-1, 1), 0x11: (-1, 1)}
NAMES = sorted([*BUTTONS, *ALIASES, *DPAD, "lt", "rt"])

IDLE_SECONDS = 60

_fd = None
_created_at = 0.0
_idle_timer = None
_lock = threading.RLock()


def _event(fd, etype, code, value):
    os.write(fd, struct.pack("llHHi", 0, 0, etype, code, value))


def _open():
    """Create the virtual controller once; give games time to notice a new controller."""
    global _fd, _created_at
    if _fd is not None:
        return _fd
    try:
        fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        raise RuntimeError(f"Cannot open /dev/uinput ({e.strerror}); controller input needs it.") from e
    try:
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
        for code in BUTTONS.values():
            fcntl.ioctl(fd, UI_SET_KEYBIT, code)
        for code, (lo, hi) in AXES.items():
            fcntl.ioctl(fd, UI_SET_ABSBIT, code)
            # struct uinput_abs_setup: u16 code, pad, struct input_absinfo {value, min, max, fuzz, flat, res}
            fcntl.ioctl(fd, UI_ABS_SETUP, struct.pack("HH6i", code, 0, 0, lo, hi, 0, 0, 0))
        name = b"steamdeck-mcp virtual controller"
        # struct uinput_setup: struct input_id {bustype, vendor, product, version}, char name[80], u32 ff_effects_max
        fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack("4H80sI", BUS_USB, 0x045E, 0x028E, 0x0110, name, 0))
        fcntl.ioctl(fd, UI_DEV_CREATE)
    except OSError as e:
        os.close(fd)
        raise RuntimeError(f"Could not create the virtual controller: {e}") from e
    _fd, _created_at = fd, time.time()
    atexit.register(close)
    return fd


def close():
    global _fd
    with _lock:
        if _fd is None:
            return
        try:
            fcntl.ioctl(_fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(_fd)
        _fd = None


def _unplug_when_idle():
    global _idle_timer
    if _idle_timer:
        _idle_timer.cancel()
    _idle_timer = threading.Timer(IDLE_SECONDS, close)
    _idle_timer.daemon = True
    _idle_timer.start()


def _normalize(buttons):
    names = [ALIASES.get(b, b) for b in str(buttons).lower().replace(",", " ").split()]
    unknown = [b for b in names if b not in BUTTONS and b not in DPAD and b not in TRIGGERS]
    if not names:
        raise ValueError("Pass at least one button, e.g. 'a' or 'menu'.")
    if unknown:
        raise ValueError(f"Unknown button(s): {', '.join(unknown)}. Use: {', '.join(NAMES)}.")
    return names


def _set(fd, name, pressed):
    if name in BUTTONS:
        _event(fd, EV_KEY, BUTTONS[name], 1 if pressed else 0)
    elif name in DPAD:
        code, direction = DPAD[name]
        _event(fd, EV_ABS, code, direction if pressed else 0)
    else:
        _event(fd, EV_ABS, TRIGGERS[name], 255 if pressed else 0)
    _event(fd, EV_SYN, SYN_REPORT, 0)


def press(buttons, hold_ms=120, together=False):
    """Press buttons one after another (or together, e.g. 'lb rb'), like a controller."""
    names = _normalize(buttons)
    hold = max(50, min(int(hold_ms), 3000)) / 1000
    with _lock:
        if _idle_timer:
            _idle_timer.cancel()
        plugged_in = _fd is None
        fd = _open()
        wait = 1.5 - (time.time() - _created_at)  # a freshly plugged controller is picked up after a moment
        if wait > 0:
            time.sleep(wait)
        groups = [names] if together else [[n] for n in names]
        for group in groups:
            for n in group:
                _set(fd, n, True)
            time.sleep(hold)
            for n in group:
                _set(fd, n, False)
            time.sleep(0.15)
        _unplug_when_idle()
    result = {"pressed": names, "together": together, "hold_ms": int(hold * 1000)}
    if plugged_in:
        result["note"] = f"Virtual controller plugged in; it is unplugged after {IDLE_SECONDS} s without presses."
    return result
