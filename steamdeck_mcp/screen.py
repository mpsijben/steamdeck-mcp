"""Screenshots and keyboard/mouse input in Game Mode.

Screenshots go through gamescope (the same capture as the Steam screenshot button). Input is sent with
xdotool to the X display (Xwayland) that holds the window gamescope shows in front: Steam runs on one
display, games and apps such as Konsole usually on another.
"""
import base64
import os
import shutil
import struct
import subprocess
import tempfile
import time

from . import hardware

MAX_SHOT_W = 1280  # larger screens (TV, Steam Machine) are scaled down so screenshots stay small
DISPLAYS = (":0", ":1", ":2")
BUTTONS = {"left": 1, "middle": 2, "right": 3}


# Mouse coordinates are in the space of the last screenshot; this maps them back to the real screen.
_shot = {"image": None, "screen": None}  # (w, h) of the image sent, (w, h) of the screen it was taken from


def _png_size(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("gamescope wrote something that is not a PNG.")
    return struct.unpack(">II", data[16:24])


def _screen_size():
    """Size of what gamescope shows (internal screen or TV): from the last screenshot, else the root window."""
    if _shot["screen"]:
        return _shot["screen"]
    out = _run(["xdotool", "getdisplaygeometry"]).stdout.split()
    if len(out) == 2 and all(v.isdigit() for v in out):
        return int(out[0]), int(out[1])
    raise RuntimeError("Cannot tell the screen size; take a screenshot first.")


def _scale_png(path, width):
    """Downscale a PNG with ffmpeg (shipped with SteamOS); returns the new bytes or None if not possible."""
    if not shutil.which("ffmpeg"):
        return None
    out = path + ".small.png"
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", path, "-vf", f"scale={width}:-2", out],
                       capture_output=True, timeout=30)
    if r.returncode != 0 or not os.path.exists(out):
        return None
    with open(out, "rb") as f:
        return f.read()


def _need_game_mode():
    if not hardware.is_game_mode():
        raise RuntimeError("Only works in Game Mode (gamescope).")


def _run(args, display=":0", timeout=10):
    env = {**os.environ, "DISPLAY": display}
    try:
        return subprocess.run(args, capture_output=True, text=True, env=env, timeout=timeout)
    except FileNotFoundError as e:
        raise RuntimeError(f"{args[0]} not found; this tool needs SteamOS in Game Mode.") from e


def screenshot():
    """PNG of what is on screen, as MCP image content."""
    _need_game_mode()
    if not shutil.which("gamescopectl"):
        raise RuntimeError("gamescopectl not found.")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "screen.png")
        r = _run(["gamescopectl", "screenshot", path])
        if r.returncode != 0:
            raise RuntimeError(f"gamescopectl failed: {r.stderr.strip()}")
        # gamescope writes the file asynchronously; wait until it is complete.
        size, deadline = -1, time.time() + 5
        while time.time() < deadline:
            if os.path.exists(path) and os.path.getsize(path) > 0 and os.path.getsize(path) == size:
                break
            size = os.path.getsize(path) if os.path.exists(path) else -1
            time.sleep(0.25)
        else:
            raise RuntimeError("gamescope did not write a screenshot.")
        with open(path, "rb") as f:
            data = f.read()
        screen_w, screen_h = _png_size(data)
        if screen_w > MAX_SHOT_W:
            data = _scale_png(path, MAX_SHOT_W) or data
    image_w, image_h = _png_size(data)
    _shot.update(image=(image_w, image_h), screen=(screen_w, screen_h))
    scaled = f" (scaled down from {screen_w}x{screen_h})" if (image_w, image_h) != (screen_w, screen_h) else ""
    return {"image": base64.b64encode(data).decode(), "mime": "image/png",
            "text": f"Screenshot {image_w}x{image_h}{scaled}. Mouse tools take x/y in this image's pixels."}


def _focused_window():
    """(display, window id) of the window gamescope shows in front."""
    r = _run(["xprop", "-root", "GAMESCOPE_FOCUSED_WINDOW"])
    value = r.stdout.rpartition("=")[2].strip()
    if not value.isdigit():
        raise RuntimeError("Cannot tell which window is in front (is this Game Mode?).")
    window = value
    for display in DISPLAYS:
        if _run(["xdotool", "getwindowname", window], display).returncode == 0:
            return display, window
    raise RuntimeError("The window in front was not found on any X display.")


def _xdotool(args):
    _need_game_mode()
    if not shutil.which("xdotool"):
        raise RuntimeError("xdotool not found.")
    display, window = _focused_window()
    name = _run(["xdotool", "getwindowname", window], display).stdout.strip()
    r = _run(["xdotool", *[a.replace("{window}", window) for a in args]], display)
    if r.returncode != 0:
        raise RuntimeError(f"xdotool failed: {r.stderr.strip()}")
    return display, window, name


def press_key(keys, repeat=1):
    """Press key combinations, e.g. 'Escape', 'Return', 'ctrl+s', 'alt+Return' (xdotool key names)."""
    keys = [k for k in str(keys).split() if k]
    if not keys:
        raise ValueError("Pass at least one key, e.g. 'Escape' or 'ctrl+s'.")
    repeat = max(1, min(int(repeat), 20))
    _, _, name = _xdotool(["key", "--clearmodifiers", "--delay", "80", *(keys * repeat)])
    return {"pressed": keys, "repeat": repeat, "window": name}


def type_text(text):
    if not text:
        raise ValueError("Pass the text to type.")
    if len(text) > 500:
        raise ValueError("Text is limited to 500 characters.")
    _, _, name = _xdotool(["type", "--clearmodifiers", "--delay", "20", "--", text])
    return {"typed": len(text), "window": name}


SCROLL = {"up": 4, "down": 5, "left": 6, "right": 7}  # X11 wheel buttons


def _to_window(x, y):
    """Screenshot coordinates -> (display, window, window x, window y). The screenshot may be scaled down from
    the real screen, and gamescope fits the window into the screen (black bars when the aspect ratio differs)."""
    screen_w, screen_h = _screen_size()
    image_w, image_h = _shot["image"] or (screen_w, screen_h)
    if not (0 <= x < image_w and 0 <= y < image_h):
        raise ValueError(f"x/y must be within the {image_w}x{image_h} screenshot.")
    sx, sy = x * screen_w / image_w, y * screen_h / image_h  # point on the real screen
    display, window = _focused_window()
    geo = _run(["xdotool", "getwindowgeometry", "--shell", window], display).stdout
    vals = dict(line.split("=", 1) for line in geo.split() if "=" in line)
    w, h = int(vals.get("WIDTH", screen_w)), int(vals.get("HEIGHT", screen_h))
    scale = min(screen_w / w, screen_h / h)
    off_x, off_y = (screen_w - w * scale) / 2, (screen_h - h * scale) / 2
    wx = min(max(round((sx - off_x) / scale), 0), w - 1)
    wy = min(max(round((sy - off_y) / scale), 0), h - 1)
    return display, window, wx, wy


def mouse_click(x, y, button="left", double=False):
    """Click at screenshot coordinates, mapped to the window in front."""
    if button not in BUTTONS:
        raise ValueError(f"button must be one of {', '.join(BUTTONS)}.")
    _, _, wx, wy = _to_window(x, y)
    _, _, name = _xdotool(["mousemove", "--window", "{window}", str(wx), str(wy), "click",
                           *(["--repeat", "2"] if double else []), str(BUTTONS[button])])
    return {"clicked": [x, y], "window_coords": [wx, wy], "button": button, "double": double, "window": name}


def mouse_move(x=None, y=None, dx=None, dy=None, duration_ms=300, drag=None):
    """Move the mouse to x/y (screenshot coordinates) or by dx/dy pixels, spread over duration_ms like a swipe
    on the right trackpad (e.g. to turn a camera). drag holds a mouse button during the move."""
    absolute, relative = x is not None or y is not None, dx is not None or dy is not None
    if absolute == relative:
        raise ValueError("Pass either x and y (move to a spot) or dx and/or dy (move by an amount).")
    if drag is not None and drag not in BUTTONS:
        raise ValueError(f"drag must be one of {', '.join(BUTTONS)}.")
    steps = max(1, min(int(duration_ms) // 16, 120))
    args = ["mousedown", str(BUTTONS[drag])] if drag else []
    if absolute:
        if x is None or y is None:
            raise ValueError("Pass both x and y.")
        _, _, wx, wy = _to_window(x, y)
        args += ["mousemove", "--window", "{window}", str(wx), str(wy)]
        result = {"moved_to": [x, y], "window_coords": [wx, wy]}
    else:
        dx, dy = int(dx or 0), int(dy or 0)
        if abs(dx) > 4000 or abs(dy) > 4000:
            raise ValueError("dx/dy are limited to 4000 pixels.")
        done_x = done_y = 0
        for i in range(1, steps + 1):  # small steps, so games see a smooth swipe instead of a jump
            step_x, step_y = round(dx * i / steps) - done_x, round(dy * i / steps) - done_y
            done_x, done_y = done_x + step_x, done_y + step_y
            args += ["mousemove_relative", "--", str(step_x), str(step_y), "sleep", f"{duration_ms / 1000 / steps:.3f}"]
        result = {"moved_by": [dx, dy], "duration_ms": int(duration_ms)}
    if drag:
        args += ["mouseup", str(BUTTONS[drag])]
    _, _, name = _xdotool(args)
    return {**result, "drag": drag, "window": name}


def mouse_scroll(direction="down", amount=3, x=None, y=None):
    """Scroll like a mouse wheel (or the left trackpad): amount notches up, down, left or right, optionally at x/y."""
    if direction not in SCROLL:
        raise ValueError(f"direction must be one of {', '.join(SCROLL)}.")
    amount = int(amount)
    if not 1 <= amount <= 50:
        raise ValueError("amount must be between 1 and 50.")
    args = []
    if x is not None or y is not None:
        _, _, wx, wy = _to_window(x, y)
        args += ["mousemove", "--window", "{window}", str(wx), str(wy)]
    args += ["click", "--repeat", str(amount), "--delay", "40", str(SCROLL[direction])]
    _, _, name = _xdotool(args)
    return {"scrolled": direction, "amount": amount, "window": name}
