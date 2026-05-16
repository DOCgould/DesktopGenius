#!/usr/bin/env python3
"""Workspace tool: screenshot, click, drag, and type on the X11 desktop."""
from __future__ import annotations

import argparse
import atexit
import collections
import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

import mss
from PIL import Image
from Xlib import X, XK, display
from Xlib.ext import xtest

import mpx as _mpx_mod

BUTTONS = {"left": 1, "middle": 2, "right": 3, "scrollup": 4, "scrolldown": 5}
BUTTON_NAME = {v: k for k, v in BUTTONS.items()}

SHOT_DIR = Path("/tmp/wstool-shots")
SHOT_TAR = Path("/tmp/wstool-shots.tar")
SHOT_TAR_LOCK = Path("/tmp/wstool-shots.tar.lock")
SHOT_DEFAULT_QUALITY = 40

SHIFT_MAP = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "minus", "+": "equal",
    "{": "bracketleft", "}": "bracketright", "|": "backslash",
    ":": "semicolon", '"': "apostrophe",
    "<": "comma", ">": "period", "?": "slash", "~": "grave",
}
CHAR_TO_KEYSYM = {
    " ": "space", "\t": "Tab", "\n": "Return",
    "-": "minus", "=": "equal",
    "[": "bracketleft", "]": "bracketright", "\\": "backslash",
    ";": "semicolon", "'": "apostrophe",
    ",": "comma", ".": "period", "/": "slash", "`": "grave",
}
MODS = {
    "ctrl": "Control_L", "control": "Control_L",
    "shift": "Shift_L",
    "alt": "Alt_L", "meta": "Alt_L",
    "super": "Super_L", "win": "Super_L",
}


def keysym_for_char(ch: str) -> tuple[int, bool]:
    if ch in SHIFT_MAP:
        return XK.string_to_keysym(SHIFT_MAP[ch]), True
    if ch in CHAR_TO_KEYSYM:
        return XK.string_to_keysym(CHAR_TO_KEYSYM[ch]), False
    if ch.isalpha() and ch.isupper():
        return XK.string_to_keysym(ch.lower()), True
    ks = XK.string_to_keysym(ch)
    if ks != 0:
        return ks, False
    # Fall back to the X11 keysym conventions for Unicode. The Latin-1
    # block (U+00A0–U+00FF) maps 1:1 to keysym = codepoint; above U+00FF
    # we use 0x01000000 | codepoint. The server may still have no
    # keycode bound — _tap_keysym handles that by remapping a spare.
    cp = ord(ch)
    if 0xA0 <= cp <= 0xFF:
        return cp, False
    if cp >= 0x100:
        return 0x01000000 | cp, False
    raise ValueError(f"no X keysym for character {ch!r}")


def keysym_for_name(name: str) -> int:
    ks = XK.string_to_keysym(name)
    if ks == 0:
        alias = {
            "ctrl": "Control_L", "control": "Control_L",
            "shift": "Shift_L",
            "alt": "Alt_L", "meta": "Alt_L",
            "super": "Super_L", "win": "Super_L",
            "enter": "Return", "return": "Return", "newline": "Return",
            "esc": "Escape", "escape": "Escape",
            "space": "space", "tab": "Tab",
            "backspace": "BackSpace", "bs": "BackSpace",
            "delete": "Delete", "del": "Delete",
            "up": "Up", "down": "Down", "left": "Left", "right": "Right",
            "home": "Home", "end": "End",
            "pageup": "Page_Up", "pagedown": "Page_Down",
        }
        ks = XK.string_to_keysym(alias.get(name.lower(), name))
    if ks == 0:
        raise ValueError(f"unknown key name: {name!r}")
    return ks


# --- Trace infrastructure ---------------------------------------------------

WIDTH = 78


def format_row(ts_rel: float, ev: dict) -> str:
    kv = ev.get("kv") or {}
    details = " ".join(f"{k}={v}" for k, v in kv.items())
    return f"[{ts_rel:8.4f}] {ev['category']:<8} {ev['action']:<20}{details}"


def _mono() -> float:
    return time.clock_gettime(time.CLOCK_MONOTONIC)


class Sink:
    def on_begin(self, focus: str) -> None: ...
    def on_event(self, ev: dict) -> None: ...
    def on_end(self, counters: dict) -> None: ...


class StdoutSink(Sink):
    """Renders the agent-trace UI inline: header, events, footer."""

    def __init__(self, stream=sys.stdout) -> None:
        self.stream = stream
        self.t0: float | None = None
        self.focus = ""
        self.total = 0

    def on_begin(self, focus: str) -> None:
        self.focus = focus
        title = "agent-trace v0.1"
        status = f"live  focus={focus}"
        pad = WIDTH - len(title) - len(status)
        print(title + " " * max(1, pad) + status, file=self.stream)
        print("─" * WIDTH, file=self.stream)
        self.stream.flush()

    def on_event(self, ev: dict) -> None:
        if self.t0 is None:
            self.t0 = ev["ts"]
        self.total += 1
        print(format_row(ev["ts"] - self.t0, ev), file=self.stream)
        self.stream.flush()

    def on_end(self, counters: dict) -> None:
        dt = (_mono() - self.t0) if self.t0 is not None else 0.0
        rate = self.total / dt if dt > 0 else 0.0
        print("─" * WIDTH, file=self.stream)
        print(f"events={counters.get('events', self.total)}  "
              f"warn={counters.get('warns', 0)}  "
              f"err={counters.get('errs', 0)}  "
              f"rate={rate:.1f}/s", file=self.stream)
        self.stream.flush()


class JsonBusSink(Sink):
    """Writes events as NDJSON to a bus file. Hot path never waits on I/O.

    Ring buffer (bounded deque) + daemon drain thread. If the buffer
    saturates, oldest events are dropped and a synthetic bus.drop event is
    flushed so the viewer can surface the gap.
    """

    def __init__(self, path: Path, capacity: int = 4096) -> None:
        self.path = path
        self.capacity = capacity
        self._buf: collections.deque[bytes] = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._dropped = 0
        self._fd = -1
        self._pid = os.getpid()
        self._thread = threading.Thread(
            target=self._run, name="click-bus-drain", daemon=True
        )
        self._thread.start()
        atexit.register(self._shutdown)

    def on_begin(self, focus: str) -> None:
        pass  # Header/footer are the viewer's responsibility.

    def on_event(self, ev: dict) -> None:
        try:
            line = (json.dumps(ev, separators=(",", ":")) + "\n").encode()
        except (TypeError, ValueError):
            line = (json.dumps({
                "ts": ev.get("ts", _mono()),
                "seq": ev.get("seq", 0),
                "pid": self._pid,
                "focus": ev.get("focus", ""),
                "severity": "error",
                "category": "bus",
                "action": "encode_fail",
                "kv": {},
            }, separators=(",", ":")) + "\n").encode()
        with self._lock:
            if len(self._buf) == self.capacity:
                # Drop-oldest. popleft + append keeps the ring full of newer.
                self._buf.popleft()
                self._dropped += 1
            self._buf.append(line)
        self._wake.set()

    def on_end(self, counters: dict) -> None:
        self._wake.set()

    def _run(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._fd = os.open(
                str(self.path),
                os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                0o600,
            )
        except OSError:
            return
        try:
            while not self._stop.is_set():
                self._wake.wait(timeout=0.05)
                self._wake.clear()
                self._flush_once()
            self._flush_once()
        finally:
            try:
                os.close(self._fd)
            except OSError:
                pass

    def _flush_once(self) -> None:
        with self._lock:
            if not self._buf and not self._dropped:
                return
            items = list(self._buf)
            self._buf.clear()
            dropped = self._dropped
            self._dropped = 0
        if dropped:
            drop_ev = {
                "ts": _mono(),
                "seq": -1,
                "pid": self._pid,
                "focus": "bus",
                "severity": "warn",
                "category": "bus",
                "action": "drop",
                "kv": {"count": dropped},
            }
            items.append(
                (json.dumps(drop_ev, separators=(",", ":")) + "\n").encode()
            )
        # One write per line keeps each line within PIPE_BUF so concurrent
        # O_APPEND writers on Linux cannot interleave bytes within a line.
        for line in items:
            try:
                os.write(self._fd, line)
            except OSError:
                break

    def _shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=0.3)


class Tracer:
    """Event publisher. Fans each event out to all configured sinks."""

    def __init__(self, focus: str, sinks: list[Sink]) -> None:
        self.focus = focus
        self.sinks = sinks
        self.pid = os.getpid()
        self.seq = 0
        self.counters = {"events": 0, "warns": 0, "errs": 0}
        for s in self.sinks:
            s.on_begin(focus)

    def emit(self, category: str, action: str, *, severity: str = "info",
             **kv) -> None:
        self.seq += 1
        self.counters["events"] += 1
        if severity == "warn":
            self.counters["warns"] += 1
        elif severity == "error":
            self.counters["errs"] += 1
        ev = {
            "ts": _mono(),
            "seq": self.seq,
            "pid": self.pid,
            "focus": self.focus,
            "severity": severity,
            "category": category,
            "action": action,
            "kv": kv,
        }
        for s in self.sinks:
            s.on_event(ev)

    def warn(self, category: str, action: str, **kv) -> None:
        self.emit(category, action, severity="warn", **kv)

    def error(self, category: str, action: str, **kv) -> None:
        self.emit(category, action, severity="error", **kv)

    def close(self) -> None:
        for s in self.sinks:
            s.on_end(self.counters)


# Module-level tracer; actions emit to it when set.
TRACER: Tracer | None = None


def _emit(category: str, action: str, **kv) -> None:
    if TRACER is not None:
        TRACER.emit(category, action, **kv)


# --- core X actions ---------------------------------------------------------

# When set, synthetic events route through the Claude master pair instead of
# the core pointer/keyboard, leaving the user's cursor untouched.
MPX: _mpx_mod.MpxController | None = None


def _send_motion(d, x: int, y: int) -> None:
    if MPX is not None:
        MPX.warp(x, y)
        return
    xtest.fake_input(d, X.MotionNotify, x=x, y=y)
    d.sync()


def _send_button(d, btn: int, press: bool) -> None:
    if MPX is not None:
        MPX.button(btn, press)
        return
    xtest.fake_input(d, X.ButtonPress if press else X.ButtonRelease, btn)
    d.sync()


def _send_key_kc(d, kc: int, press: bool) -> None:
    if MPX is not None:
        MPX.key(kc, press)
        return
    xtest.fake_input(d, X.KeyPress if press else X.KeyRelease, kc)
    d.sync()


def _motion(d, x: int, y: int, *, trace: bool = True) -> None:
    _send_motion(d, x, y)
    if trace:
        _emit("pointer", "move", x=x, y=y)


def _key(d, keysym: int, press: bool, *, label: str | None = None) -> None:
    kc = d.keysym_to_keycode(keysym)
    if kc == 0:
        raise ValueError(f"no keycode mapped for keysym {keysym:#x}")
    _send_key_kc(d, kc, press)
    if label is not None:
        _emit("key", f"{label}.{'down' if press else 'up'}")


def _find_spare_keycode(d) -> int | None:
    info = d.display.info
    min_kc, max_kc = info.min_keycode, info.max_keycode
    mapping = d.get_keyboard_mapping(min_kc, max_kc - min_kc + 1)
    for i, keysyms in enumerate(mapping):
        if all(ks == 0 for ks in keysyms):
            return min_kc + i
    return None


def _tap_keysym(d, keysym: int, *, label: str | None = None) -> None:
    """Press + release a keysym, remapping a spare keycode if necessary.

    Handles Unicode keysyms (em-dash, curly quotes, etc.) that have no
    keycode bound on the current layout — we temporarily bind one via
    change_keyboard_mapping and restore it afterwards.
    """
    kc = d.keysym_to_keycode(keysym)
    if kc != 0:
        _key(d, keysym, True, label=label)
        _key(d, keysym, False, label=label)
        return
    spare = _find_spare_keycode(d)
    if spare is None:
        raise ValueError(
            f"no keycode mapped for keysym {keysym:#x} "
            "and no spare keycode available to remap"
        )
    syms_per = len(d.get_keyboard_mapping(spare, 1)[0]) or 4
    # Clients cache the keyboard mapping and refresh it on MappingNotify
    # events asynchronously. We need short delays so the target client
    # sees the remap before the KeyPress and doesn't see the unmap until
    # after the KeyRelease. ~15ms matches xdotool's empirically safe wait.
    d.change_keyboard_mapping(spare, [[keysym] * syms_per])
    d.sync()
    time.sleep(0.015)
    try:
        _send_key_kc(d, spare, True)
        time.sleep(0.002)
        _send_key_kc(d, spare, False)
        time.sleep(0.015)
        if label is not None:
            _emit("key", f"{label}.down")
            _emit("key", f"{label}.up")
    finally:
        d.change_keyboard_mapping(spare, [[0] * syms_per])
        d.sync()


def _button(d, btn: int, press: bool) -> None:
    _send_button(d, btn, press)
    _emit("click", f"{BUTTON_NAME[btn]}.{'down' if press else 'up'}")


def do_move(d, x: int, y: int) -> None:
    _motion(d, x, y)


def do_click(d, x: int, y: int, button: str, count: int, delay: float) -> None:
    btn = BUTTONS[button]
    _motion(d, x, y)
    time.sleep(0.02)
    for i in range(count):
        _button(d, btn, True)
        time.sleep(0.02)
        _button(d, btn, False)
        if i + 1 < count:
            time.sleep(delay)
    if count >= 2:
        _emit("click", f"{button}.double", x=x, y=y)


def do_drag(d, x1: int, y1: int, x2: int, y2: int,
            button: str, duration: float, steps: int) -> None:
    btn = BUTTONS[button]
    _motion(d, x1, y1)
    time.sleep(0.05)
    _emit("drag", "begin", x1=x1, y1=y1, x2=x2, y2=y2,
          steps=steps, duration=f"{duration:.2f}s")
    _button(d, btn, True)
    steps = max(1, steps)
    dt = duration / steps
    for i in range(1, steps + 1):
        t = i / steps
        xi = int(round(x1 + (x2 - x1) * t))
        yi = int(round(y1 + (y2 - y1) * t))
        _motion(d, xi, yi, trace=False)
        if dt > 0:
            time.sleep(dt)
    time.sleep(0.03)
    _button(d, btn, False)
    _emit("drag", "end", x=x2, y=y2)


def do_type(d, text: str, cps: float) -> None:
    preview = text if len(text) <= 24 else text[:21] + "..."
    _emit("type", "begin", chars=len(text), preview=repr(preview))
    gap = 1.0 / cps if cps > 0 else 0.0
    shift_ks = XK.string_to_keysym("Shift_L")
    for ch in text:
        ks, need_shift = keysym_for_char(ch)
        if need_shift:
            _key(d, shift_ks, True)
        _tap_keysym(d, ks)
        if need_shift:
            _key(d, shift_ks, False)
        if gap:
            time.sleep(gap)
    _emit("type", "end", chars=len(text))


def do_key(d, spec: str) -> None:
    parts = [p.strip() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key spec")
    *mods, key = parts
    mod_syms = [(m, keysym_for_name(MODS.get(m.lower(), m))) for m in mods]
    key_ks = keysym_for_name(key)
    _emit("key", "combo", spec=spec)
    for name, ks in mod_syms:
        _key(d, ks, True, label=name.lower())
    _key(d, key_ks, True, label=key)
    _key(d, key_ks, False, label=key)
    for name, ks in reversed(mod_syms):
        _key(d, ks, False, label=name.lower())


# --- screenshot -------------------------------------------------------------

def do_shot(path: Path, monitor: int, region: tuple | None,
            quality: int, archive: bool, wid: int | None = None
            ) -> tuple[int, int]:
    if wid is not None:
        _emit("shot", "capture", target=f"wid={hex(wid)}")
        size = WindowCapture(wid).grab(path, quality)
        _emit("shot", "saved",
              path=str(path), size=f"{size[0]}x{size[1]}", quality=quality)
        if archive:
            _schedule_archive(path)
        return size
    _emit("shot", "capture",
          target=f"region={region}" if region else f"monitor={monitor}")
    with mss.mss() as sct:
        if region is not None:
            x, y, w, h = region
            grab = sct.grab({"left": x, "top": y, "width": w, "height": h})
        else:
            grab = sct.grab(sct.monitors[monitor])
        img = Image.frombytes("RGB", grab.size, grab.rgb)
        img.save(path, "JPEG", quality=quality, optimize=True)
    _emit("shot", "saved",
          path=str(path), size=f"{grab.size[0]}x{grab.size[1]}",
          quality=quality)
    if archive:
        _schedule_archive(path)
    return grab.size


# --- window resolution (xdotool wrapper) ------------------------------------

def _xdotool(*args: str, check: bool = True) -> str:
    r = subprocess.run(["xdotool", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(
            f"xdotool {' '.join(args)!r} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _parse_win_selector(sel: str) -> tuple[str, str]:
    """Selector kinds: name:, class:, pid:, wid:, app:.  Bare → name."""
    for pfx in ("name:", "class:", "pid:", "wid:", "app:"):
        if sel.startswith(pfx):
            return pfx[:-1], sel[len(pfx):]
    return "name", sel


def _xdotool_search(kind: str, value: str) -> list[int]:
    if kind == "name":
        out = _xdotool("search", "--onlyvisible", "--name", value, check=False)
    elif kind == "class":
        out = _xdotool("search", "--onlyvisible", "--class", value, check=False)
    elif kind == "pid":
        out = _xdotool("search", "--onlyvisible", "--pid", value, check=False)
    elif kind == "wid":
        return [int(value, 0)]
    elif kind == "app":
        # Find AT-SPI app whose name matches; resolve to its pid then to wids.
        try:
            from gi import require_version  # noqa: WPS433
            require_version("Atspi", "2.0")
            from gi.repository import Atspi  # noqa: WPS433
        except Exception:
            return []
        desk = Atspi.get_desktop(0)
        for i in range(desk.get_child_count()):
            a = desk.get_child_at_index(i)
            try:
                nm = a.get_name() or ""
            except Exception:
                continue
            if value.lower() in nm.lower():
                pid = a.get_process_id()
                out = _xdotool("search", "--onlyvisible", "--pid", str(pid),
                               check=False)
                wids = [int(s) for s in out.split() if s.strip()]
                if wids:
                    return wids
        return []
    else:
        raise ValueError(f"unknown selector kind: {kind!r}")
    return [int(s) for s in out.split() if s.strip()]


def _window_geom(wid: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) in screen coords."""
    out = _xdotool("getwindowgeometry", "--shell", str(wid))
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return (int(kv["X"]), int(kv["Y"]), int(kv["WIDTH"]), int(kv["HEIGHT"]))


def _window_info(wid: int) -> dict:
    name = _xdotool("getwindowname", str(wid), check=False)
    cls = _xdotool("getwindowclassname", str(wid), check=False)
    pid = _xdotool("getwindowpid", str(wid), check=False)
    try:
        gx, gy, gw, gh = _window_geom(wid)
    except Exception:
        gx = gy = gw = gh = 0
    return {"wid": wid, "name": name, "class": cls, "pid": pid,
            "geom": (gx, gy, gw, gh)}


def _resolve_window(sel: str) -> int:
    """Selector → x11 wid (int).

    Filters out tiny decoration windows (<50x50) and prefers the most
    recently active match (xdotool returns stacking order, newest last).
    Raises LookupError on no match.
    """
    kind, value = _parse_win_selector(sel)
    matches = _xdotool_search(kind, value)
    real = []
    for wid in matches:
        try:
            _, _, w, h = _window_geom(wid)
            if w >= 50 and h >= 50:
                real.append(wid)
        except Exception:
            real.append(wid)  # be permissive on transient geom errors
    if not real:
        raise LookupError(f"no window matched selector {sel!r}")
    chosen = real[-1]
    _emit("win", "resolve", selector=sel, wid=hex(chosen),
          candidates=len(real))
    return chosen


def _raise_window(wid: int) -> None:
    _xdotool("windowactivate", "--sync", str(wid), check=False)
    _emit("win", "raise", wid=hex(wid))


# --- window capture (XComposite) --------------------------------------------

class WindowCapture:
    """Capture a single window's pixmap.

    Strategy ladder:
      1. XComposite named-pixmap → pixmap.get_image() (works while occluded).
      2. mss region clipped to the window's screen geometry (raises window
         to the front *not* required, but the window must be unoccluded
         to actually show its own pixels).
    """

    def __init__(self, wid: int):
        self.wid = wid

    def grab(self, path: Path, quality: int) -> tuple[int, int]:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self._grab_composite(path, quality)
        except Exception as e:
            _emit("capture", "composite-fail",
                  wid=hex(self.wid), err=repr(str(e)))
            return self._grab_mss_region(path, quality)

    def _grab_composite(self, path: Path, quality: int) -> tuple[int, int]:
        from Xlib import display as _disp, X as _X
        from Xlib.ext import composite as _cext
        import numpy as np

        d = _disp.Display()
        try:
            if not d.has_extension("Composite"):
                raise RuntimeError("no Composite extension on this server")
            w = d.create_resource_object("window", self.wid)
            attr = w.get_attributes()
            if attr.map_state != _X.IsViewable:
                raise RuntimeError("window not viewable")
            redirected_here = False
            try:
                w.composite_redirect_window(_cext.RedirectAutomatic)
                d.sync()
                redirected_here = True
            except Exception:
                # Already redirected by the WM compositor — that's fine,
                # we can still name the window's pixmap.
                pass
            pix = w.composite_name_window_pixmap()
            try:
                pg = pix.get_geometry()
                img = pix.get_image(0, 0, pg.width, pg.height,
                                    _X.ZPixmap, 0xFFFFFFFF)
                arr = np.frombuffer(img.data, dtype=np.uint8)
                arr = arr.reshape((pg.height, pg.width, 4))
                # X servers on little-endian boxes deliver BGRA in ZPixmap.
                rgb = arr[..., [2, 1, 0]]
                Image.fromarray(rgb).save(
                    path, "JPEG", quality=quality, optimize=True)
                _emit("capture", "saved",
                      wid=hex(self.wid), path=str(path),
                      size=f"{pg.width}x{pg.height}", method="composite")
                return (pg.width, pg.height)
            finally:
                try:
                    pix.free()
                except Exception:
                    pass
                if redirected_here:
                    try:
                        w.composite_unredirect_window(
                            _cext.RedirectAutomatic)
                    except Exception:
                        pass
        finally:
            d.close()

    def _grab_mss_region(self, path: Path, quality: int) -> tuple[int, int]:
        x, y, w, h = _window_geom(self.wid)
        with mss.mss() as sct:
            grab = sct.grab(
                {"left": x, "top": y, "width": w, "height": h})
            Image.frombytes("RGB", grab.size, grab.rgb).save(
                path, "JPEG", quality=quality, optimize=True)
        _emit("capture", "saved",
              wid=hex(self.wid), path=str(path),
              size=f"{w}x{h}", method="mss-region")
        return (w, h)


# --- AT-SPI semantic layer --------------------------------------------------
#
# Selector grammar (passed as a single argv string):
#
#     role=push-button name="OK"
#     role=text-entry index=0
#     role~=button name~=Save
#
# Keys (any order, space-separated; values may be quoted via shlex):
#   role  : exact role-name match (case-insensitive; '_' == '-')
#   name  : exact name match
#   role~ : role-name substring match
#   name~ : name substring match
#   state : require state (e.g. state=showing); repeatable
#   index : among siblings sharing the same role+name, pick the Nth (0-based)
#
# Resolution walks the AT-SPI subtree below a window's frame in DFS order
# and returns the first match (or the index-Nth match).

def _atspi():
    from gi import require_version  # noqa: WPS433
    require_version("Atspi", "2.0")
    from gi.repository import Atspi  # noqa: WPS433
    return Atspi


def _norm_role(s: str) -> str:
    # AT-SPI role-name spelling is inconsistent ("push button" vs
    # "push_button" vs "push-button" depending on binding); normalize
    # whitespace and underscores to hyphens for selector comparison.
    return "-".join(s.lower().replace("_", " ").split())


def _ax_states(a) -> set[str]:
    Atspi = _atspi()
    out = set()
    try:
        ss = a.get_state_set()
    except Exception:
        return out
    # Iterate over the named StateType members rather than the (undocumented)
    # bitfield, so we end up with stable lowercase strings.
    for n in dir(Atspi.StateType):
        if n.startswith("_") or not n.isupper():
            continue
        try:
            if ss.contains(getattr(Atspi.StateType, n)):
                out.add(n.lower())
        except Exception:
            pass
    return out


def _ax_extents(a) -> tuple[int, int, int, int] | None:
    Atspi = _atspi()
    try:
        e = a.get_extents(Atspi.CoordType.SCREEN)
        if e.width <= 0 or e.height <= 0 or e.x < -1000000:
            return None
        return (e.x, e.y, e.width, e.height)
    except Exception:
        return None


def _atspi_app_for_pid(pid: int):
    Atspi = _atspi()
    desk = Atspi.get_desktop(0)
    for i in range(desk.get_child_count()):
        a = desk.get_child_at_index(i)
        try:
            if a.get_process_id() == pid:
                return a
        except Exception:
            continue
    return None


def _atspi_top_for_window(wid: int):
    """Find the AT-SPI frame/window/dialog corresponding to an X11 wid.

    Heuristic: pick the app by pid; among its children prefer one whose
    name matches the X11 WM_NAME, then any child marked SHOWING, then
    the first child.  Returns None if the app exposes no AT-SPI tree.
    """
    pid_str = _xdotool("getwindowpid", str(wid), check=False)
    if not pid_str:
        return None
    try:
        pid = int(pid_str)
    except ValueError:
        return None
    app = _atspi_app_for_pid(pid)
    if app is None:
        return None
    name = _xdotool("getwindowname", str(wid), check=False) or ""
    showing_match = None
    first = None
    for j in range(app.get_child_count()):
        f = app.get_child_at_index(j)
        if first is None:
            first = f
        try:
            fn = f.get_name() or ""
        except Exception:
            fn = ""
        if name and (fn == name or fn in name or name in fn):
            return f
        if showing_match is None and "showing" in _ax_states(f):
            showing_match = f
    return showing_match or first


def _parse_ax_selector(s: str) -> dict:
    """Parse 'k=v k~=v ...' selector. Returns dict of constraints."""
    sel: dict = {"role": None, "role_sub": None, "name": None,
                 "name_sub": None, "states": [], "index": None}
    for tok in shlex.split(s):
        if "=" not in tok:
            raise ValueError(f"bad selector token: {tok!r}")
        # split on first '=' only; supports 'role~=button'
        k, v = tok.split("=", 1)
        if k == "role":
            sel["role"] = _norm_role(v)
        elif k == "role~":
            sel["role_sub"] = _norm_role(v)
        elif k == "name":
            sel["name"] = v
        elif k == "name~":
            sel["name_sub"] = v
        elif k == "state":
            sel["states"].append(v.lower())
        elif k == "index":
            sel["index"] = int(v)
        else:
            raise ValueError(f"unknown selector key: {k!r}")
    return sel


def _ax_match(a, sel: dict) -> bool:
    try:
        role = _norm_role(a.get_role_name() or "")
        name = a.get_name() or ""
    except Exception:
        return False
    if sel["role"] and role != sel["role"]:
        return False
    if sel["role_sub"] and sel["role_sub"] not in role:
        return False
    if sel["name"] is not None and name != sel["name"]:
        return False
    if sel["name_sub"] and sel["name_sub"] not in name:
        return False
    if sel["states"]:
        st = _ax_states(a)
        for s in sel["states"]:
            if s not in st:
                return False
    return True


def _ax_walk(root, sel: dict, *, max_depth: int = 30):
    """DFS yield matches under root."""
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        try:
            if _ax_match(node, sel):
                yield node
            if depth >= max_depth:
                continue
            n = node.get_child_count()
        except Exception:
            continue
        for i in range(n - 1, -1, -1):
            try:
                stack.append((node.get_child_at_index(i), depth + 1))
            except Exception:
                pass


def _ax_resolve(top, selector_str: str):
    """Resolve a selector string against an AT-SPI top-level. Returns the
    matched accessible. Raises LookupError if nothing matches."""
    sel = _parse_ax_selector(selector_str)
    matches = list(_ax_walk(top, sel))
    if not matches:
        raise LookupError(f"no AT-SPI node matched {selector_str!r}")
    if sel["index"] is not None:
        if sel["index"] >= len(matches):
            raise LookupError(
                f"index={sel['index']} out of range "
                f"(only {len(matches)} matches)")
        return matches[sel["index"]]
    return matches[0]


def _ax_path(a) -> str:
    """Best-effort role/name path from app root down to a, for printing."""
    parts = []
    node = a
    while node is not None:
        try:
            r = _norm_role(node.get_role_name() or "?")
            n = node.get_name() or ""
        except Exception:
            r, n = "?", ""
        parts.append(f"{r}[{n}]" if n else r)
        try:
            node = node.get_parent()
        except Exception:
            node = None
    return "/" + "/".join(reversed(parts))


def _atspi_at_point(wid: int, x: int, y: int) -> dict | None:
    """AT-SPI accessibleAtPoint under a screen coordinate."""
    top = _atspi_top_for_window(wid)
    if top is None:
        return None
    Atspi = _atspi()
    # Walk down through accessible_at_point.
    node = top
    while True:
        try:
            child = node.get_accessible_at_point(
                x, y, Atspi.CoordType.SCREEN)
        except Exception:
            child = None
        if child is None or child == node:
            break
        node = child
    if node is top:
        # fall back to deepest match in DFS by extents
        candidate = None
        for n in _ax_walk(top, {"role": None, "role_sub": None,
                                "name": None, "name_sub": None,
                                "states": [], "index": None}):
            ext = _ax_extents(n)
            if not ext:
                continue
            ex, ey, ew, eh = ext
            if ex <= x < ex + ew and ey <= y < ey + eh:
                candidate = n
        node = candidate or top
    ext = _ax_extents(node) or (0, 0, 0, 0)
    return {
        "role": _norm_role(node.get_role_name() or ""),
        "name": node.get_name() or "",
        "bbox": ext,
        "path": _ax_path(node),
    }


# --- OCR --------------------------------------------------------------------

_OCR_ENGINE = None


def _ocr_engine():
    """Lazy-init RapidOCR. First call is ~1s; subsequent are ~0."""
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR  # noqa: WPS433
        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def do_ocr(path: Path, region_offset: tuple[int, int] = (0, 0)
           ) -> list[dict]:
    """Run OCR on a saved image. Returns boxes sorted by (y, x).

    region_offset shifts box coords back into screen space when the shot
    was captured from a region — so coordinates in the output match what
    you'd pass to `click X Y`.
    """
    _emit("ocr", "begin", path=str(path))
    engine = _ocr_engine()
    result, elapsed = engine(str(path))
    ox, oy = region_offset
    boxes: list[dict] = []
    for item in result or []:
        poly, text, conf = item
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        x, y = int(min(xs)), int(min(ys))
        w, h = int(max(xs) - min(xs)), int(max(ys) - min(ys))
        boxes.append({
            "x": x + ox, "y": y + oy, "w": w, "h": h,
            "text": text, "conf": round(float(conf), 3),
        })
    boxes.sort(key=lambda b: (b["y"], b["x"]))
    det_ms = int(round(sum(elapsed) * 1000)) if elapsed else 0
    _emit("ocr", "end", boxes=len(boxes), ms=det_ms)
    return boxes


def format_ocr(boxes: list[dict]) -> str:
    lines = [f"--- ocr ({len(boxes)} boxes) ---"]
    for b in boxes:
        lines.append(
            f"[{b['x']:4d},{b['y']:4d} {b['w']:4d}x{b['h']:3d}] "
            f"{b['text']}"
        )
    return "\n".join(lines)


def _default_shot_path() -> Path:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    return SHOT_DIR / f"shot-{ts}.jpg"


def _schedule_archive(path: Path) -> None:
    # Non-daemon so a short-lived CLI invocation waits for the archive to
    # finish on exit rather than killing the worker mid-append.
    t = threading.Thread(
        target=_archive_worker, args=(path,),
        name="wstool-shot-archive", daemon=False,
    )
    t.start()


def _archive_worker(path: Path) -> None:
    try:
        SHOT_TAR.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(SHOT_TAR_LOCK),
                          os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            with tarfile.open(str(SHOT_TAR), "a") as tf:
                tf.add(str(path), arcname=path.name)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        os.unlink(path)
        _emit("shot", "archive",
              tar=str(SHOT_TAR), name=path.name)
    except Exception as e:
        if TRACER is not None:
            TRACER.error("shot", "archive_fail", msg=repr(str(e)))


# --- script mode ------------------------------------------------------------

def run_script(d, lines) -> None:
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        toks = shlex.split(line)
        op = toks[0].lower()
        a = toks[1:]
        _emit("script", "step", op=op)
        if op == "move":
            do_move(d, int(a[0]), int(a[1]))
        elif op == "click":
            btn = a[2] if len(a) > 2 else "left"
            n = int(a[3]) if len(a) > 3 else 1
            do_click(d, int(a[0]), int(a[1]), btn, n, 0.08)
        elif op in ("dblclick", "doubleclick"):
            do_click(d, int(a[0]), int(a[1]), "left", 2, 0.08)
        elif op == "drag":
            dur = float(a[4]) if len(a) > 4 else 0.3
            do_drag(d, int(a[0]), int(a[1]), int(a[2]), int(a[3]),
                    "left", dur, 30)
        elif op == "type":
            do_type(d, " ".join(a), 0.0)
        elif op == "key":
            do_key(d, a[0])
        elif op == "sleep":
            time.sleep(float(a[0]))
            _emit("script", "sleep", duration=f"{float(a[0]):.2f}s")
        else:
            if TRACER:
                TRACER.error("script", "unknown_op", op=op)
            raise ValueError(f"unknown script op: {op!r}")


SCRIPT_HELP = """\
Script format (one command per line, '#' starts a comment):
  move X Y
  click X Y [button] [count]
  dblclick X Y
  drag X1 Y1 X2 Y2 [duration]
  type some literal text
  key Return            # or ctrl+c, shift+Tab, etc.
  sleep 0.5
"""


# --- bus path resolution ----------------------------------------------------

def _default_bus_path() -> Path:
    rt = os.environ.get("XDG_RUNTIME_DIR") or (
        f"{os.path.expanduser('~')}/.cache"
    )
    return Path(rt) / "click" / "bus.ndjson"


def resolve_bus_path(args) -> Path:
    if getattr(args, "bus", None):
        return Path(args.bus)
    env = os.environ.get("CLICK_BUS")
    if env:
        return Path(env)
    return _default_bus_path()


# --- viewer (trace subcommand) ---------------------------------------------

def cmd_trace(args) -> int:
    from inotify_simple import INotify, flags
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.text import Text

    path = resolve_bus_path(args)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        path.touch(mode=0o600)

    capacity = int(os.environ.get("CLICK_BUFFER", args.scrollback))
    scrollback: collections.deque = collections.deque(maxlen=capacity)
    counters = {"events": 0, "warns": 0, "errs": 0, "drops": 0}
    rate_window: collections.deque = collections.deque()

    state = {
        "fd": -1,
        "offset": 0,
        "partial": b"",
        "anchor": None,  # relative-time anchor
        "viewer_t0": _mono(),
    }
    stop = threading.Event()
    dirty = threading.Event()

    def open_bus(seek_to_end: bool) -> None:
        if state["fd"] != -1:
            try:
                os.close(state["fd"])
            except OSError:
                pass
        state["fd"] = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK)
        if seek_to_end:
            state["offset"] = os.lseek(state["fd"], 0, os.SEEK_END)
        else:
            state["offset"] = 0
        state["partial"] = b""

    def ingest_line(line: bytes) -> None:
        if not line.strip():
            return
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return
        scrollback.append(ev)
        counters["events"] += 1
        sev = ev.get("severity", "info")
        if sev == "warn":
            counters["warns"] += 1
        elif sev == "error":
            counters["errs"] += 1
        if ev.get("category") == "bus" and ev.get("action") == "drop":
            counters["drops"] += int(ev.get("kv", {}).get("count", 1))
        if state["anchor"] is None:
            state["anchor"] = ev["ts"]
        rate_window.append((_mono(), counters["events"]))

    def read_all() -> None:
        # Detect truncation first.
        try:
            st = os.fstat(state["fd"])
            if st.st_size < state["offset"]:
                os.lseek(state["fd"], 0, os.SEEK_SET)
                state["offset"] = 0
                state["partial"] = b""
        except OSError:
            return
        chunks = [state["partial"]]
        while True:
            try:
                chunk = os.read(state["fd"], 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            chunks.append(chunk)
            state["offset"] += len(chunk)
        data = b"".join(chunks)
        parts = data.split(b"\n")
        state["partial"] = parts[-1]
        for line in parts[:-1]:
            ingest_line(line)
        if parts[:-1]:
            dirty.set()

    def watcher() -> None:
        ino = INotify()
        try:
            ino.add_watch(str(path), flags.MODIFY | flags.CLOSE_WRITE)
        except OSError:
            pass
        try:
            ino.add_watch(
                str(path.parent),
                flags.CREATE | flags.MOVED_TO | flags.DELETE,
            )
        except OSError:
            pass
        read_all()
        basename = path.name
        while not stop.is_set():
            evs = ino.read(timeout=200)
            need_reopen = False
            for iev in evs:
                if iev.name == basename and (
                    iev.mask & (flags.CREATE | flags.MOVED_TO)
                ):
                    need_reopen = True
            if need_reopen:
                open_bus(seek_to_end=False)
            read_all()

    def render() -> Layout:
        width = max(40, Console().width)
        title = "agent-trace v0.1"
        status = f"live  focus={args.focus}"
        pad = width - len(title) - len(status)
        header = title + " " * max(1, pad) + status

        now = _mono()
        while rate_window and rate_window[0][0] < now - 5:
            rate_window.popleft()
        if len(rate_window) >= 2:
            t_first, s_first = rate_window[0]
            t_last, s_last = rate_window[-1]
            dt = t_last - t_first
            rate = (s_last - s_first) / dt if dt > 0 else 0.0
        else:
            rate = 0.0

        layout = Layout()
        layout.split_column(
            Layout(name="header", size=1),
            Layout(name="sep1", size=1),
            Layout(name="log", ratio=1),
            Layout(name="sep2", size=1),
            Layout(name="footer", size=1),
        )

        term_h = Console().size.height
        max_rows = max(1, term_h - 4)
        events = list(scrollback)[-max_rows:]
        anchor = state["anchor"] if state["anchor"] is not None else now
        rows = [format_row(ev["ts"] - anchor, ev) for ev in events]
        log_text = "\n".join(rows)

        footer = (
            f"events={counters['events']}  "
            f"warn={counters['warns']}  "
            f"err={counters['errs']}  "
            f"rate={rate:.1f}/s  "
            f"drops={counters['drops']}"
        )

        layout["header"].update(Text(header))
        layout["sep1"].update(Text("─" * width))
        layout["log"].update(Text(log_text))
        layout["sep2"].update(Text("─" * width))
        layout["footer"].update(Text(footer))
        return layout

    open_bus(seek_to_end=not args.replay)
    wth = threading.Thread(target=watcher, daemon=True, name="click-trace-watch")
    wth.start()

    console = Console()
    try:
        with Live(
            render(), console=console, refresh_per_second=args.refresh,
            screen=True,
        ) as live:
            period = 1.0 / max(1, args.refresh)
            while not stop.is_set():
                # Redraw when dirty or when rate window may have aged.
                dirty.wait(timeout=period)
                dirty.clear()
                live.update(render())
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0


# --- win subcommands --------------------------------------------------------

def cmd_win(args) -> int:
    try:
        return _cmd_win_inner(args)
    except (LookupError, RuntimeError, ValueError) as e:
        print(str(e), file=sys.stderr)
        _emit("win", "error", msg=repr(str(e)))
        return 1


def _cmd_win_inner(args) -> int:
    sub = args.win_action
    if sub == "list":
        out = _xdotool("search", "--onlyvisible", "--name", ".+",
                       check=False)
        wids = [int(s) for s in out.split() if s.strip()]
        rows = []
        for wid in wids:
            info = _window_info(wid)
            gx, gy, gw, gh = info["geom"]
            if gw < 50 or gh < 50:
                continue
            rows.append((wid, info, gx, gy, gw, gh))
        for wid, info, gx, gy, gw, gh in rows:
            ax = "y" if _atspi_top_for_window(wid) else "n"
            print(f"{hex(wid):>12}  {gw}x{gh:<5}+{gx:<4}+{gy:<5}  "
                  f"pid={info['pid']:<6}  ax={ax}  "
                  f"class={info['class']:<28}  {info['name']}")
        return 0
    if sub == "find":
        try:
            wid = _resolve_window(args.selector)
        except LookupError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(hex(wid))
        return 0
    if sub == "raise":
        wid = _resolve_window(args.selector)
        _raise_window(wid)
        if not args.trace:
            print(f"raised {hex(wid)}")
        return 0
    if sub == "identify":
        x, y = args.x, args.y
        out = _xdotool("search", "--onlyvisible", "--name", ".+",
                       check=False)
        match = None
        for w in [int(s) for s in out.split() if s.strip()]:
            try:
                gx, gy, gw, gh = _window_geom(w)
            except Exception:
                continue
            if gw < 50 or gh < 50:
                continue
            if gx <= x < gx + gw and gy <= y < gy + gh:
                match = (w, gx, gy, gw, gh)
        if not match:
            print(f"no window at ({x},{y})", file=sys.stderr)
            return 1
        wid, gx, gy, gw, gh = match
        info = _window_info(wid)
        print(f"window: {hex(wid)} class={info['class']} "
              f"name={info['name']!r}")
        print(f"  geom: {gw}x{gh} at ({gx},{gy}); local=({x-gx},{y-gy})")
        try:
            ax = _atspi_at_point(wid, x, y)
        except Exception as e:
            print(f"  atspi: (unavailable: {e})", file=sys.stderr)
            return 0
        if ax:
            bx, by, bw, bh = ax["bbox"]
            print(f"  atspi: role={ax['role']} name={ax['name']!r} "
                  f"bbox={bw}x{bh}@({bx},{by})")
            print(f"  path:  {ax['path']}")
        return 0
    raise ValueError(f"unknown win action: {sub!r}")


# --- ax subcommands (AT-SPI) ------------------------------------------------

def _ax_describe(a, *, indent: int = 0) -> str:
    try:
        role = _norm_role(a.get_role_name() or "?")
        name = a.get_name() or ""
    except Exception:
        return f"{'  '*indent}<unreadable>"
    states = sorted(_ax_states(a))
    short_states = [s for s in states
                    if s in ("showing", "visible", "focusable",
                             "focused", "sensitive", "selected",
                             "checked")]
    ext = _ax_extents(a)
    bbox = (f" @{ext[0]},{ext[1]} {ext[2]}x{ext[3]}"
            if ext else "")
    return (f"{'  '*indent}{role} {name!r} "
            f"[{','.join(short_states)}]{bbox}")


def _ax_top_or_die(args, d=None):
    if not getattr(args, "win", None):
        raise ValueError("--win is required for ax subcommands")
    wid = _resolve_window(args.win)
    top = _atspi_top_for_window(wid)
    if top is None:
        raise RuntimeError(
            f"no AT-SPI top-level for window {hex(wid)} — the app does "
            "not expose accessibility (try the coordinate flow)")
    _emit("ax", "top", wid=hex(wid), name=repr(top.get_name() or ""))
    return wid, top


def cmd_ax(args) -> int:
    try:
        return _cmd_ax_inner(args)
    except (LookupError, RuntimeError, ValueError) as e:
        # Domain errors should print cleanly; only let unexpected ones
        # bubble up for a stack trace.
        print(str(e), file=sys.stderr)
        _emit("ax", "error", msg=repr(str(e)))
        return 1


def _cmd_ax_inner(args) -> int:
    Atspi = _atspi()
    sub = args.ax_action

    if sub == "tree":
        wid, top = _ax_top_or_die(args)
        max_depth = args.depth
        def walk(node, depth):
            if depth > max_depth:
                return
            print(_ax_describe(node, indent=depth))
            try:
                n = node.get_child_count()
            except Exception:
                return
            for i in range(n):
                try:
                    walk(node.get_child_at_index(i), depth + 1)
                except Exception:
                    pass
        walk(top, 0)
        return 0

    if sub == "find":
        wid, top = _ax_top_or_die(args)
        try:
            node = _ax_resolve(top, args.selector)
        except LookupError as e:
            _emit("ax", "missing", selector=args.selector)
            print(str(e), file=sys.stderr)
            return 1
        ext = _ax_extents(node)
        print(_ax_describe(node))
        print(f"  path: {_ax_path(node)}")
        if ext:
            cx, cy = ext[0] + ext[2] // 2, ext[1] + ext[3] // 2
            print(f"  center: ({cx},{cy})")
        return 0

    if sub == "click":
        wid, top = _ax_top_or_die(args)
        node = _ax_resolve(top, args.selector)
        _emit("ax", "click.begin", selector=args.selector,
              path=_ax_path(node))
        # Prefer Action.do_action("click") if available.
        action_done = False
        try:
            ai = node.get_action_iface()
        except Exception:
            ai = None
        if ai is not None:
            try:
                n = ai.get_n_actions()
            except Exception:
                n = 0
            for i in range(n):
                try:
                    aname = ai.get_action_name(i) or ""
                except Exception:
                    aname = ""
                if aname.lower() in ("click", "press", "activate",
                                     "do default action"):
                    try:
                        ai.do_action(i)
                        action_done = True
                        _emit("ax", "click.end", method="action",
                              action=aname)
                        break
                    except Exception:
                        pass
        if not action_done:
            ext = _ax_extents(node)
            if not ext:
                _emit("ax", "click.fail", reason="no extents")
                print("cannot click: no Action interface and no extents",
                      file=sys.stderr)
                return 1
            cx, cy = ext[0] + ext[2] // 2, ext[1] + ext[3] // 2
            _raise_window(wid)
            time.sleep(0.05)
            d = display.Display()
            try:
                do_click(d, cx, cy, "left", 1, 0.08)
            finally:
                d.close()
            _emit("ax", "click.end", method="coord", x=cx, y=cy)
        if not args.trace:
            print(f"clicked: {_ax_path(node)}")
        return 0

    if sub == "type":
        wid, top = _ax_top_or_die(args)
        node = _ax_resolve(top, args.selector)
        text = " ".join(args.text)
        _emit("ax", "type.begin", selector=args.selector,
              chars=len(text))
        # Prefer EditableText.set_text_contents (atomic, race-free).
        et = None
        try:
            et = node.get_editable_text_iface()
        except Exception:
            pass
        if et is not None:
            try:
                et.set_text_contents(text)
                _emit("ax", "type.end", method="editable_text")
                if not args.trace:
                    print(f"typed (atspi): {len(text)} chars")
                return 0
            except Exception as e:
                _emit("ax", "type.fallback", err=repr(str(e)))
        # Fallback: focus + raise + XTEST type.
        try:
            ci = node.get_component_iface()
            if ci is not None:
                ci.grab_focus()
        except Exception:
            pass
        _raise_window(wid)
        time.sleep(0.05)
        d = display.Display()
        try:
            do_type(d, text, args.cps)
            if args.enter:
                do_key(d, "Return")
        finally:
            d.close()
        _emit("ax", "type.end", method="coord")
        if not args.trace:
            print(f"typed: {len(text)} chars")
        return 0

    if sub == "text":
        wid, top = _ax_top_or_die(args)
        if args.selector:
            target = _ax_resolve(top, args.selector)
            nodes = [target]
        else:
            # Walk all text-bearing children.
            nodes = []
            for n in _ax_walk(top, _parse_ax_selector("role~=text")):
                nodes.append(n)
        out_chunks = []
        for n in nodes:
            try:
                ti = n.get_text_iface()
            except Exception:
                ti = None
            if ti is None:
                # fall back to the accessible's name
                name = n.get_name() or ""
                if name:
                    out_chunks.append(name)
                continue
            try:
                length = ti.get_character_count()
                out_chunks.append(ti.get_text(0, length))
            except Exception:
                pass
        print("\n".join(c for c in out_chunks if c))
        return 0

    if sub == "watch":
        wid, top = _ax_top_or_die(args)
        deadline = time.time() + args.timeout
        sel = _parse_ax_selector(args.selector)
        while time.time() < deadline:
            try:
                for m in _ax_walk(top, sel):
                    print(_ax_describe(m))
                    return 0
            except Exception:
                pass
            time.sleep(args.interval)
        print(f"timed out waiting for {args.selector!r}", file=sys.stderr)
        return 1

    raise ValueError(f"unknown ax action: {sub!r}")


# --- CLI --------------------------------------------------------------------

def parse_region(s: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("region must be X,Y,W,H")
    return tuple(parts)  # type: ignore[return-value]


def _build_tracer(args) -> Tracer:
    sinks: list[Sink] = []
    if getattr(args, "trace", False):
        sinks.append(StdoutSink())
    if not getattr(args, "no_bus", False):
        path = resolve_bus_path(args)
        cap = int(os.environ.get("CLICK_BUFFER", 4096))
        sinks.append(JsonBusSink(path, capacity=cap))
    focus = args.focus if args.focus else (args.cmd or "all")
    return Tracer(focus=focus, sinks=sinks)


def main(argv: list[str] | None = None) -> int:
    global TRACER
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 2 and argv[0].lstrip("-").isdigit() and argv[1].lstrip("-").isdigit():
        argv = ["click", *argv]

    p = argparse.ArgumentParser(
        prog="click",
        description="Screenshot, click, drag, and type on the X11 desktop.",
        epilog="Shortcut: `click X Y` is equivalent to `click click X Y`.",
    )
    p.add_argument("-t", "--trace", action="store_true",
                   help="render the agent-trace UI inline (header/events/footer)")
    p.add_argument("--focus", default=None,
                   help="focus label shown in trace (default: cmd name)")
    p.add_argument("--bus", default=None,
                   help="path to the NDJSON bus file (default: $XDG_RUNTIME_DIR/click/bus.ndjson)")
    p.add_argument("--no-bus", action="store_true",
                   help="disable publishing to the bus")
    p.add_argument("--mpx", action="store_true",
                   help="route synthetic events through the Claude master "
                        "pair (auto-creates if missing). Also enabled by "
                        "CLICK_MPX=1.")
    p.add_argument("--win", default=None,
                   help="scope this command to a window — selector "
                        "syntax: name:Vivado | class:Gimp | pid:1234 | "
                        "wid:0xNN | app:vivado. Coords (click/drag/move) "
                        "become window-relative; shot captures via "
                        "XComposite.")
    p.add_argument("--no-raise", action="store_true",
                   help="with --win, do not raise the window before "
                        "interacting (capture still works occluded; "
                        "synthetic input may go to whatever has focus)")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("shot", help="capture a screenshot (writes to /tmp)")
    ps.add_argument("--name", default=None,
                    help="basename under /tmp/wstool-shots/ "
                         "(default: shot-<epoch_ms>.jpg)")
    ps.add_argument("-m", "--monitor", type=int, default=0)
    ps.add_argument("-r", "--region", type=parse_region, default=None)
    ps.add_argument("--quality", type=int, default=SHOT_DEFAULT_QUALITY,
                    help=f"JPEG quality 1-95 (default {SHOT_DEFAULT_QUALITY})")
    ps.add_argument("--no-archive", action="store_true",
                    help="keep the loose JPEG; skip the background tar append")
    ps.add_argument("--ocr", action="store_true",
                    help="also OCR the shot and print boxes (screen coords)")
    ps.add_argument("--ocr-only", action="store_true",
                    help="print only OCR text, suppress image path line")

    pc = sub.add_parser("click", help="click at X Y (optionally then type)")
    pc.add_argument("x", type=int)
    pc.add_argument("y", type=int)
    pc.add_argument("-b", "--button", choices=BUTTONS.keys(), default="left")
    pc.add_argument("-n", "--count", type=int, default=1)
    pc.add_argument("-d", "--delay", type=float, default=0.08)
    pc.add_argument("--type", dest="type_text", default=None)
    pc.add_argument("--enter", action="store_true")
    pc.add_argument("--cps", type=float, default=0.0)

    pd = sub.add_parser("dblclick", help="double-click at X Y")
    pd.add_argument("x", type=int)
    pd.add_argument("y", type=int)

    pg = sub.add_parser("drag", help="press, drag, and release")
    pg.add_argument("x1", type=int); pg.add_argument("y1", type=int)
    pg.add_argument("x2", type=int); pg.add_argument("y2", type=int)
    pg.add_argument("-b", "--button", choices=BUTTONS.keys(), default="left")
    pg.add_argument("--duration", type=float, default=0.3)
    pg.add_argument("--steps", type=int, default=30)

    pt = sub.add_parser("type", help="type literal text at current focus")
    pt.add_argument("text", nargs="+")
    pt.add_argument("--enter", action="store_true")
    pt.add_argument("--cps", type=float, default=0.0)

    pk = sub.add_parser("key", help="press a named key or combo")
    pk.add_argument("spec")

    pm = sub.add_parser("move", help="move cursor to X Y")
    pm.add_argument("x", type=int); pm.add_argument("y", type=int)

    prun = sub.add_parser("script", help="run a multi-step script")
    prun.add_argument("file", nargs="?", type=Path, default=None)
    prun.add_argument("--help-script", action="store_true")

    ptrace = sub.add_parser("trace", help="live-stream the event bus")
    ptrace.add_argument("--replay", action="store_true",
                        help="start from the beginning of the bus file")
    ptrace.add_argument("--scrollback", type=int, default=500)
    ptrace.add_argument("--refresh", type=int, default=20)

    pup = sub.add_parser(
        "mpx-up",
        help="create the Claude master pointer/keyboard pair "
             "(idempotent; one-time per X session)",
    )
    pup.add_argument("--warp-to", default=None,
                     help="initial cursor position 'X,Y' "
                          "(default: park at bottom-center)")
    pup.add_argument("--cursor", default="pirate",
                     help="cursor-font shape for the Claude master "
                          "(default 'pirate'; e.g. crosshair, hand1, "
                          "target, watch — see mpx.CURSOR_SHAPES)")

    sub.add_parser(
        "mpx-down",
        help="remove the Claude master pair",
    )

    sub.add_parser(
        "mpx-status",
        help="report whether the Claude master pair exists and its IDs",
    )

    sub.add_parser(
        "mpx-park",
        help="warp the Claude cursor to the parking spot (bottom-center)",
    )

    pwin = sub.add_parser("win", help="window identification & control")
    wsub = pwin.add_subparsers(dest="win_action", required=True)
    wsub.add_parser("list", help="list visible windows (wid, geom, pid, "
                                  "class, name; ax=y/n marks AT-SPI bound)")
    pwf = wsub.add_parser("find", help="resolve a selector to an x11 wid")
    pwf.add_argument("selector",
                     help="name:Vivado | class:Gimp | pid:N | wid:0xN | "
                          "app:vivado | bare-string (defaults to name:)")
    pwr = wsub.add_parser("raise",
                          help="bring a window to the front and focus it")
    pwr.add_argument("selector")
    pwi = wsub.add_parser("identify",
                          help="report which window (and AT-SPI control) "
                               "is at a screen X,Y")
    pwi.add_argument("x", type=int)
    pwi.add_argument("y", type=int)

    pax = sub.add_parser(
        "ax",
        help="AT-SPI semantic interaction (requires --win SEL on every "
             "subcommand). Selector grammar: role=...  name=...  "
             "name~=...  state=...  index=N",
    )
    asub = pax.add_subparsers(dest="ax_action", required=True)
    pat = asub.add_parser("tree", help="dump the accessibility tree")
    pat.add_argument("--depth", type=int, default=8,
                     help="max tree depth to print (default 8)")
    paf = asub.add_parser("find",
                          help="resolve a selector and print path + bbox")
    paf.add_argument("selector")
    pac = asub.add_parser("click",
                          help="invoke Action.click; coord-fallback at bbox "
                               "center if widget has no Action interface")
    pac.add_argument("selector")
    pay = asub.add_parser("type",
                          help="set EditableText contents; coord-fallback "
                               "via focus+type if interface is missing")
    pay.add_argument("selector")
    pay.add_argument("text", nargs="+")
    pay.add_argument("--enter", action="store_true",
                     help="press Return after typing (fallback path only)")
    pay.add_argument("--cps", type=float, default=0.0)
    pax_text = asub.add_parser("text",
                               help="extract text via AT-SPI Text iface")
    pax_text.add_argument("selector", nargs="?", default=None,
                          help="omit to dump all text-bearing children")
    paw = asub.add_parser("watch",
                          help="poll until selector resolves (race-free)")
    paw.add_argument("selector")
    paw.add_argument("--timeout", type=float, default=10.0)
    paw.add_argument("--interval", type=float, default=0.2)

    args = p.parse_args(argv)

    # Viewer doesn't publish events; handle separately.
    if args.cmd == "trace":
        if not args.focus:
            args.focus = "all"
        return cmd_trace(args)

    # MPX lifecycle commands run standalone (no python-xlib display, no bus).
    if args.cmd in ("mpx-up", "mpx-down", "mpx-status", "mpx-park"):
        return _cmd_mpx(args)

    TRACER = _build_tracer(args)
    TRACER.emit("session", "attach", target=f"desktop-{_display_num()}")

    mpx_created_here = False
    if args.mpx or os.environ.get("CLICK_MPX") == "1":
        global MPX
        MPX = _mpx_mod.MpxController()
        if not MPX.attach():
            MPX.ensure_master()
            MPX.set_cursor(os.environ.get("CLICK_MPX_CURSOR", "pirate"))
            mpx_created_here = True
            TRACER.emit("mpx", "create",
                        pointer_id=MPX.pointer_id,
                        keyboard_id=MPX.keyboard_id)
        else:
            TRACER.emit("mpx", "attach",
                        pointer_id=MPX.pointer_id,
                        keyboard_id=MPX.keyboard_id)

    rc = 0
    try:
        if args.cmd == "win":
            return cmd_win(args)
        if args.cmd == "ax":
            return cmd_ax(args)

        if args.cmd == "shot":
            SHOT_DIR.mkdir(parents=True, exist_ok=True)
            if args.name:
                path = SHOT_DIR / Path(args.name).name
                if path.suffix.lower() not in (".jpg", ".jpeg"):
                    path = path.with_suffix(".jpg")
            else:
                path = _default_shot_path()
            wid = (_resolve_window(args.win)
                   if getattr(args, "win", None) else None)
            size = do_shot(path, args.monitor, args.region,
                           args.quality, not args.no_archive, wid=wid)
            if not args.trace and not args.ocr_only:
                print(f"{path} {size[0]}x{size[1]}")
            if args.ocr or args.ocr_only:
                # When --win is set, the JPEG is the window pixmap (origin
                # at the window's top-left); shift OCR boxes to screen
                # coords using the live geometry, since the window may
                # have moved between capture and OCR.
                if wid is not None:
                    gx, gy, _, _ = _window_geom(wid)
                    offset = (gx, gy)
                elif args.region:
                    offset = (args.region[0], args.region[1])
                else:
                    offset = (0, 0)
                boxes = do_ocr(path, region_offset=offset)
                print(format_ocr(boxes))
            return 0

        d = display.Display()
        try:
            rc = _dispatch(d, args)
        finally:
            d.close()
    except Exception as e:
        TRACER.error("session", "error", msg=repr(str(e)))
        rc = 1
        if not args.trace:
            raise
    finally:
        if MPX is not None:
            # Tear down the master pair we created so it doesn't linger and
            # steal keyboard focus from the user. Only tear down if *we*
            # created it this invocation; if we attached to an existing
            # pair (e.g. the user explicitly ran `click mpx-up`), park
            # instead. CLICK_MPX_PERSIST=1 forces the old park-only
            # behavior for debugging.
            persist = os.environ.get("CLICK_MPX_PERSIST") == "1"
            try:
                if mpx_created_here and not persist:
                    MPX.remove_master()
                    TRACER.emit("mpx", "remove")
                else:
                    px, py = MPX.park()
                    TRACER.emit("mpx", "park", x=px, y=py)
            except Exception:
                pass
            MPX.close()
        TRACER.emit("session", "detach")
        TRACER.close()
    return rc


def _cmd_mpx(args) -> int:
    ctl = _mpx_mod.MpxController()
    try:
        if args.cmd == "mpx-up":
            existed = ctl.attach()
            ptr_id, kbd_id = ctl.ensure_master()
            ctl.set_cursor(args.cursor)
            if args.warp_to:
                wx, wy = (int(s) for s in args.warp_to.split(","))
                ctl.warp(wx, wy)
            else:
                wx, wy = ctl.park()
            verb = "attached" if existed else "created"
            print(f"Claude master {verb}: pointer={ptr_id} keyboard={kbd_id} "
                  f"cursor={args.cursor} parked=({wx},{wy})")
            return 0
        if args.cmd == "mpx-park":
            if not ctl.attach():
                print("Claude master not present", flush=True)
                return 1
            x, y = ctl.park()
            print(f"parked Claude cursor at ({x},{y})")
            return 0
        if args.cmd == "mpx-down":
            removed = ctl.remove_master()
            print("Claude master removed" if removed
                  else "Claude master not present")
            return 0
        if args.cmd == "mpx-status":
            ids = ctl._find_master(_mpx_mod.CLAUDE_NAME)
            if ids is None:
                print("Claude master not present")
                return 1
            print(f"Claude master: pointer={ids[0]} keyboard={ids[1]}")
            return 0
    finally:
        ctl.close()
    return 0


def _dispatch(d, args) -> int:
    # --win SEL: resolve the window once, raise it (unless --no-raise),
    # and translate per-command coords from window-relative to absolute
    # immediately before each action (so a moved window doesn't void the
    # whole flow).
    win_wid = None
    if getattr(args, "win", None):
        win_wid = _resolve_window(args.win)
        if not getattr(args, "no_raise", False):
            _raise_window(win_wid)
            time.sleep(0.05)

    def _abs(x: int, y: int) -> tuple[int, int]:
        if win_wid is None:
            return x, y
        gx, gy, _, _ = _window_geom(win_wid)
        return gx + x, gy + y

    if args.cmd == "click":
        ax, ay = _abs(args.x, args.y)
        do_click(d, ax, ay, args.button, args.count, args.delay)
        if args.type_text is not None:
            time.sleep(0.08)
            do_type(d, args.type_text, args.cps)
        if args.enter:
            do_key(d, "Return")
        if not args.trace:
            scope = f" [in {hex(win_wid)}]" if win_wid is not None else ""
            msg = (f"{args.button} click x{args.count} @ "
                   f"({args.x},{args.y}){scope}")
            if args.type_text is not None:
                msg += f" + typed {len(args.type_text)} chars"
            if args.enter:
                msg += " + Return"
            print(msg)
    elif args.cmd == "dblclick":
        ax, ay = _abs(args.x, args.y)
        do_click(d, ax, ay, "left", 2, 0.08)
        if not args.trace:
            print(f"double-click @ ({args.x},{args.y})")
    elif args.cmd == "drag":
        ax1, ay1 = _abs(args.x1, args.y1)
        ax2, ay2 = _abs(args.x2, args.y2)
        do_drag(d, ax1, ay1, ax2, ay2,
                args.button, args.duration, args.steps)
        if not args.trace:
            print(f"{args.button} drag ({args.x1},{args.y1}) -> "
                  f"({args.x2},{args.y2})")
    elif args.cmd == "type":
        text = " ".join(args.text)
        do_type(d, text, args.cps)
        if args.enter:
            do_key(d, "Return")
        if not args.trace:
            print(f"typed {len(text)} chars"
                  + (" + Return" if args.enter else ""))
    elif args.cmd == "key":
        do_key(d, args.spec)
        if not args.trace:
            print(f"key {args.spec}")
    elif args.cmd == "move":
        ax, ay = _abs(args.x, args.y)
        do_move(d, ax, ay)
        if not args.trace:
            print(f"moved @ ({args.x},{args.y})")
    elif args.cmd == "script":
        if args.help_script:
            print(SCRIPT_HELP)
            return 0
        src = args.file.read_text() if args.file else sys.stdin.read()
        run_script(d, src.splitlines())
        if not args.trace:
            print("script ok")
    return 0


def _display_num() -> str:
    return (os.environ.get("DISPLAY", ":0").lstrip(":") or "0").split(".", 1)[0]


if __name__ == "__main__":
    sys.exit(main())
