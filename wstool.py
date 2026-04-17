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
import sys
import tarfile
import threading
import time
from pathlib import Path

import mss
from PIL import Image
from Xlib import X, XK, display
from Xlib.ext import xtest

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
    if ks == 0:
        raise ValueError(f"no X keysym for character {ch!r}")
    return ks, False


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

def _motion(d, x: int, y: int, *, trace: bool = True) -> None:
    xtest.fake_input(d, X.MotionNotify, x=x, y=y)
    d.sync()
    if trace:
        _emit("pointer", "move", x=x, y=y)


def _key(d, keysym: int, press: bool, *, label: str | None = None) -> None:
    kc = d.keysym_to_keycode(keysym)
    if kc == 0:
        raise ValueError(f"no keycode mapped for keysym {keysym:#x}")
    xtest.fake_input(d, X.KeyPress if press else X.KeyRelease, kc)
    d.sync()
    if label is not None:
        _emit("key", f"{label}.{'down' if press else 'up'}")


def _button(d, btn: int, press: bool) -> None:
    xtest.fake_input(d, X.ButtonPress if press else X.ButtonRelease, btn)
    d.sync()
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
        _key(d, ks, True)
        _key(d, ks, False)
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
            quality: int, archive: bool) -> tuple[int, int]:
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

    args = p.parse_args(argv)

    # Viewer doesn't publish events; handle separately.
    if args.cmd == "trace":
        if not args.focus:
            args.focus = "all"
        return cmd_trace(args)

    TRACER = _build_tracer(args)
    TRACER.emit("session", "attach", target=f"desktop-{_display_num()}")

    rc = 0
    try:
        if args.cmd == "shot":
            SHOT_DIR.mkdir(parents=True, exist_ok=True)
            if args.name:
                path = SHOT_DIR / Path(args.name).name
                if path.suffix.lower() not in (".jpg", ".jpeg"):
                    path = path.with_suffix(".jpg")
            else:
                path = _default_shot_path()
            size = do_shot(path, args.monitor, args.region,
                           args.quality, not args.no_archive)
            if not args.trace:
                print(f"{path} {size[0]}x{size[1]}")
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
        TRACER.emit("session", "detach")
        TRACER.close()
    return rc


def _dispatch(d, args) -> int:
    if args.cmd == "click":
        do_click(d, args.x, args.y, args.button, args.count, args.delay)
        if args.type_text is not None:
            time.sleep(0.08)
            do_type(d, args.type_text, args.cps)
        if args.enter:
            do_key(d, "Return")
        if not args.trace:
            msg = f"{args.button} click x{args.count} @ ({args.x},{args.y})"
            if args.type_text is not None:
                msg += f" + typed {len(args.type_text)} chars"
            if args.enter:
                msg += " + Return"
            print(msg)
    elif args.cmd == "dblclick":
        do_click(d, args.x, args.y, "left", 2, 0.08)
        if not args.trace:
            print(f"double-click @ ({args.x},{args.y})")
    elif args.cmd == "drag":
        do_drag(d, args.x1, args.y1, args.x2, args.y2,
                args.button, args.duration, args.steps)
        if not args.trace:
            print(f"{args.button} drag ({args.x1},{args.y1}) -> ({args.x2},{args.y2})")
    elif args.cmd == "type":
        text = " ".join(args.text)
        do_type(d, text, args.cps)
        if args.enter:
            do_key(d, "Return")
        if not args.trace:
            print(f"typed {len(text)} chars" + (" + Return" if args.enter else ""))
    elif args.cmd == "key":
        do_key(d, args.spec)
        if not args.trace:
            print(f"key {args.spec}")
    elif args.cmd == "move":
        do_move(d, args.x, args.y)
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
