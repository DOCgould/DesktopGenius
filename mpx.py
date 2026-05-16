"""MPX (Multi-Pointer X) controller for click.

Routes synthetic pointer and key events through a dedicated master device
pair so the user's primary cursor is not hijacked by automation.

python-xlib's xinput ext doesn't wrap XIChangeHierarchy, XIWarpPointer, or
the device-targeted XTest functions, so this module talks directly to
libXi.so.6 / libXtst.so.6 / libX11.so.6 via ctypes.

The libX11 connection here is independent of python-xlib's pure-Python one;
they coexist fine — two parallel client connections to the same server.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path

# --- shared library handles ------------------------------------------------

_X11 = ctypes.CDLL("libX11.so.6", use_errno=True)
_Xi = ctypes.CDLL("libXi.so.6", use_errno=True)
_Xtst = ctypes.CDLL("libXtst.so.6", use_errno=True)

# Opaque pointer types
Display = ctypes.c_void_p
XDevice_p = ctypes.c_void_p
XID = ctypes.c_ulong
Window = XID
Cursor = XID
Bool = ctypes.c_int

# Cursor-font shapes from /usr/include/X11/cursorfont.h.
# Even values only — odd values are reserved for the "mask" half of the
# old two-glyph encoding and are never passed to XCreateFontCursor.
CURSOR_SHAPES = {
    "X_cursor": 0, "arrow": 2, "crosshair": 34, "diamond_cross": 36,
    "dot": 38, "exchange": 50, "fleur": 52, "gobbler": 54, "gumby": 56,
    "hand1": 58, "hand2": 60, "heart": 62, "iron_cross": 66,
    "left_ptr": 68, "pencil": 86, "pirate": 88, "plus": 90,
    "question_arrow": 92, "right_ptr": 94, "sailboat": 104, "spider": 122,
    "spraycan": 124, "star": 126, "target": 128, "tcross": 130,
    "top_left_arrow": 132, "trek": 142, "umbrella": 146, "watch": 150,
    "xterm": 152,
}

# --- libX11 ----------------------------------------------------------------

_X11.XOpenDisplay.argtypes = [ctypes.c_char_p]
_X11.XOpenDisplay.restype = Display
_X11.XCloseDisplay.argtypes = [Display]
_X11.XCloseDisplay.restype = ctypes.c_int
_X11.XDefaultRootWindow.argtypes = [Display]
_X11.XDefaultRootWindow.restype = Window
_X11.XFlush.argtypes = [Display]
_X11.XSync.argtypes = [Display, Bool]
_X11.XCreateFontCursor.argtypes = [Display, ctypes.c_uint]
_X11.XCreateFontCursor.restype = Cursor
_X11.XFreeCursor.argtypes = [Display, Cursor]
_X11.XDisplayWidth.argtypes = [Display, ctypes.c_int]
_X11.XDisplayWidth.restype = ctypes.c_int
_X11.XDisplayHeight.argtypes = [Display, ctypes.c_int]
_X11.XDisplayHeight.restype = ctypes.c_int
_X11.XDefaultScreen.argtypes = [Display]
_X11.XDefaultScreen.restype = ctypes.c_int

# --- libXi: hierarchy + query + warp + open/close device ------------------

XI_ADD_MASTER = 1
XI_REMOVE_MASTER = 2
XI_FLOATING = 2
XI_ALL_DEVICES = 0
XI_ALL_MASTER_DEVICES = 1
XI_MASTER_POINTER = 1
XI_MASTER_KEYBOARD = 2
XI_SLAVE_POINTER = 3
XI_SLAVE_KEYBOARD = 4


class _XIAddMasterInfo(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("name", ctypes.c_char_p),
        ("send_core", ctypes.c_int),
        ("enable", ctypes.c_int),
    ]


class _XIRemoveMasterInfo(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("deviceid", ctypes.c_int),
        ("return_mode", ctypes.c_int),
        ("return_pointer", ctypes.c_int),
        ("return_keyboard", ctypes.c_int),
    ]


class _XIDeviceInfo(ctypes.Structure):
    _fields_ = [
        ("deviceid", ctypes.c_int),
        ("name", ctypes.c_char_p),
        ("use", ctypes.c_int),
        ("attachment", ctypes.c_int),
        ("enabled", ctypes.c_int),
        ("num_classes", ctypes.c_int),
        ("classes", ctypes.c_void_p),
    ]


_Xi.XIChangeHierarchy.argtypes = [Display, ctypes.c_void_p, ctypes.c_int]
_Xi.XIChangeHierarchy.restype = ctypes.c_int

_Xi.XIQueryDevice.argtypes = [
    Display, ctypes.c_int, ctypes.POINTER(ctypes.c_int),
]
_Xi.XIQueryDevice.restype = ctypes.POINTER(_XIDeviceInfo)
_Xi.XIFreeDeviceInfo.argtypes = [ctypes.POINTER(_XIDeviceInfo)]

_Xi.XIWarpPointer.argtypes = [
    Display, ctypes.c_int,
    Window, Window,
    ctypes.c_double, ctypes.c_double,
    ctypes.c_uint, ctypes.c_uint,
    ctypes.c_double, ctypes.c_double,
]
_Xi.XIWarpPointer.restype = ctypes.c_int

# XOpenDevice/XCloseDevice are XInput1 calls, exported by libXi.
_Xi.XOpenDevice.argtypes = [Display, XID]
_Xi.XOpenDevice.restype = XDevice_p
_Xi.XCloseDevice.argtypes = [Display, XDevice_p]
_Xi.XCloseDevice.restype = ctypes.c_int

# Per-master cursor binding (XInput2).
_Xi.XIDefineCursor.argtypes = [Display, ctypes.c_int, Window, Cursor]
_Xi.XIDefineCursor.restype = ctypes.c_int
_Xi.XIUndefineCursor.argtypes = [Display, ctypes.c_int, Window]
_Xi.XIUndefineCursor.restype = ctypes.c_int

# --- libXtst: device-targeted fake events ---------------------------------

_Xtst.XTestFakeDeviceKeyEvent.argtypes = [
    Display, XDevice_p, ctypes.c_uint, Bool,
    ctypes.POINTER(ctypes.c_int), ctypes.c_int, ctypes.c_ulong,
]
_Xtst.XTestFakeDeviceKeyEvent.restype = Bool

_Xtst.XTestFakeDeviceButtonEvent.argtypes = [
    Display, XDevice_p, ctypes.c_uint, Bool,
    ctypes.POINTER(ctypes.c_int), ctypes.c_int, ctypes.c_ulong,
]
_Xtst.XTestFakeDeviceButtonEvent.restype = Bool


# --- module surface --------------------------------------------------------

CLAUDE_NAME = "Claude"


def _state_path() -> Path:
    rt = os.environ.get("XDG_RUNTIME_DIR") or (
        f"{os.path.expanduser('~')}/.cache"
    )
    return Path(rt) / "click" / "mpx.json"


class MpxError(RuntimeError):
    pass


class MpxController:
    """Owns a Claude master pair and dispatches synthetic events to it.

    Each instance opens its own libX11 connection. Call close() (or use as a
    context manager) to release device handles and the connection.
    """

    def __init__(self) -> None:
        self._dpy = _X11.XOpenDisplay(None)
        if not self._dpy:
            raise MpxError("XOpenDisplay failed")
        self.pointer_id: int | None = None
        self.keyboard_id: int | None = None
        # XTest can't address master devices directly (XInput1 limitation),
        # so we route through the master's auto-created XTEST slave pair.
        self.xtest_pointer_id: int | None = None
        self.xtest_keyboard_id: int | None = None
        self._pointer_dev: int | None = None
        self._keyboard_dev: int | None = None
        self._cursor: int | None = None

    def close(self) -> None:
        for dev in (self._pointer_dev, self._keyboard_dev):
            if dev:
                _Xi.XCloseDevice(self._dpy, dev)
        self._pointer_dev = None
        self._keyboard_dev = None
        if self._cursor:
            _X11.XFreeCursor(self._dpy, self._cursor)
            self._cursor = None
        if self._dpy:
            _X11.XCloseDisplay(self._dpy)
            self._dpy = None

    def __enter__(self) -> "MpxController":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # --- lifecycle ---

    def ensure_master(self, name: str = CLAUDE_NAME) -> tuple[int, int]:
        """Return (pointer_id, keyboard_id), creating the pair if absent."""
        ids = self._find_master(name)
        if ids is None:
            self._add_master(name)
            ids = self._find_master(name)
            if ids is None:
                raise MpxError(
                    f"AddMaster {name!r} succeeded but device not found"
                )
        self.pointer_id, self.keyboard_id = ids
        self._resolve_xtest_slaves()
        self._save_state()
        return ids

    def attach(self, name: str = CLAUDE_NAME) -> bool:
        """Bind to an existing master pair without creating. False if absent."""
        ids = self._find_master(name)
        if ids is None:
            return False
        self.pointer_id, self.keyboard_id = ids
        self._resolve_xtest_slaves()
        return True

    def _resolve_xtest_slaves(self) -> None:
        assert self.pointer_id is not None and self.keyboard_id is not None
        slaves = self._find_xtest_slaves(self.pointer_id, self.keyboard_id)
        if slaves is None:
            raise MpxError(
                f"XTEST slaves for master pair "
                f"({self.pointer_id},{self.keyboard_id}) not found"
            )
        self.xtest_pointer_id, self.xtest_keyboard_id = slaves

    def remove_master(self, name: str = CLAUDE_NAME) -> bool:
        ids = self._find_master(name)
        if ids is None:
            self._clear_state()
            return False
        ptr_id, _ = ids
        # Removing the master pointer also removes its paired keyboard.
        info = _XIRemoveMasterInfo(
            type=XI_REMOVE_MASTER,
            deviceid=ptr_id,
            return_mode=XI_FLOATING,
            return_pointer=0,
            return_keyboard=0,
        )
        _Xi.XIChangeHierarchy(self._dpy, ctypes.byref(info), 1)
        _X11.XSync(self._dpy, 0)
        self._clear_state()
        self.pointer_id = None
        self.keyboard_id = None
        return True

    def _add_master(self, name: str) -> None:
        info = _XIAddMasterInfo(
            type=XI_ADD_MASTER,
            name=name.encode(),
            send_core=1,
            enable=1,
        )
        _Xi.XIChangeHierarchy(self._dpy, ctypes.byref(info), 1)
        _X11.XSync(self._dpy, 0)

    def _find_master(self, name: str) -> tuple[int, int] | None:
        wanted_ptr = f"{name} pointer"
        wanted_kbd = f"{name} keyboard"
        n = ctypes.c_int(0)
        arr = _Xi.XIQueryDevice(
            self._dpy, XI_ALL_MASTER_DEVICES, ctypes.byref(n),
        )
        try:
            ptr_id: int | None = None
            kbd_id: int | None = None
            for i in range(n.value):
                info = arr[i]
                dev_name = info.name.decode() if info.name else ""
                if info.use == XI_MASTER_POINTER and dev_name == wanted_ptr:
                    ptr_id = info.deviceid
                elif info.use == XI_MASTER_KEYBOARD and dev_name == wanted_kbd:
                    kbd_id = info.deviceid
            if ptr_id is None or kbd_id is None:
                return None
            return ptr_id, kbd_id
        finally:
            _Xi.XIFreeDeviceInfo(arr)

    def _find_xtest_slaves(
        self, master_ptr_id: int, master_kbd_id: int,
    ) -> tuple[int, int] | None:
        """Locate the XTEST slave pair attached to the given masters.

        Returns (xtest_pointer_id, xtest_keyboard_id) or None if missing.
        XTEST slaves are auto-created by the server for every master pair.
        """
        n = ctypes.c_int(0)
        arr = _Xi.XIQueryDevice(self._dpy, XI_ALL_DEVICES, ctypes.byref(n))
        try:
            tp: int | None = None
            tk: int | None = None
            for i in range(n.value):
                info = arr[i]
                dev_name = info.name.decode() if info.name else ""
                if "XTEST" not in dev_name:
                    continue
                if (info.use == XI_SLAVE_POINTER
                        and info.attachment == master_ptr_id):
                    tp = info.deviceid
                elif (info.use == XI_SLAVE_KEYBOARD
                        and info.attachment == master_kbd_id):
                    tk = info.deviceid
            if tp is None or tk is None:
                return None
            return tp, tk
        finally:
            _Xi.XIFreeDeviceInfo(arr)

    # --- state cache ---

    def _save_state(self) -> None:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps({
            "display": os.environ.get("DISPLAY", ":0"),
            "pointer_id": self.pointer_id,
            "keyboard_id": self.keyboard_id,
        }))

    def _clear_state(self) -> None:
        try:
            _state_path().unlink()
        except FileNotFoundError:
            pass

    # --- per-session XDevice handles ---

    def _pdev(self) -> int:
        # XTest works against XInput1 slave devices; the master's auto-created
        # XTEST slave is what we open here. Events flow up to the master.
        if self._pointer_dev is None:
            if self.xtest_pointer_id is None:
                raise MpxError(
                    "xtest_pointer_id unset; call ensure_master/attach first"
                )
            dev = _Xi.XOpenDevice(self._dpy, XID(self.xtest_pointer_id))
            if not dev:
                raise MpxError(
                    f"XOpenDevice(XTEST pointer {self.xtest_pointer_id}) failed"
                )
            self._pointer_dev = dev
        return self._pointer_dev

    def _kdev(self) -> int:
        if self._keyboard_dev is None:
            if self.xtest_keyboard_id is None:
                raise MpxError(
                    "xtest_keyboard_id unset; call ensure_master/attach first"
                )
            dev = _Xi.XOpenDevice(self._dpy, XID(self.xtest_keyboard_id))
            if not dev:
                raise MpxError(
                    f"XOpenDevice(XTEST keyboard {self.xtest_keyboard_id}) failed"
                )
            self._keyboard_dev = dev
        return self._keyboard_dev

    # --- event synthesis ---

    # --- cursor binding ---

    def set_cursor(self, name: str) -> None:
        """Bind a named cursor-font shape to the Claude master pointer.

        Cursor is set on the root window, so it shows whenever the Claude
        pointer is over a window that doesn't define its own cursor.
        """
        if self.pointer_id is None:
            raise MpxError("pointer_id unset; call ensure_master/attach first")
        if name not in CURSOR_SHAPES:
            raise MpxError(
                f"unknown cursor name {name!r}; "
                f"known: {', '.join(sorted(CURSOR_SHAPES))}"
            )
        new_cursor = _X11.XCreateFontCursor(self._dpy, CURSOR_SHAPES[name])
        if not new_cursor:
            raise MpxError(f"XCreateFontCursor({name}) failed")
        root = _X11.XDefaultRootWindow(self._dpy)
        _Xi.XIDefineCursor(self._dpy, self.pointer_id, root, new_cursor)
        _X11.XSync(self._dpy, 0)
        if self._cursor:
            _X11.XFreeCursor(self._dpy, self._cursor)
        self._cursor = new_cursor

    def screen_size(self) -> tuple[int, int]:
        scr = _X11.XDefaultScreen(self._dpy)
        return (
            _X11.XDisplayWidth(self._dpy, scr),
            _X11.XDisplayHeight(self._dpy, scr),
        )

    def park(self) -> tuple[int, int]:
        """Warp to bottom-center of the screen, away from typical UI.

        Returns the (x, y) parked position.
        """
        w, h = self.screen_size()
        x, y = w // 2, h - 1
        self.warp(x, y)
        return x, y

    def warp(self, x: int, y: int) -> None:
        if self.pointer_id is None:
            raise MpxError("pointer_id unset; call ensure_master/attach first")
        root = _X11.XDefaultRootWindow(self._dpy)
        _Xi.XIWarpPointer(
            self._dpy, self.pointer_id,
            0, root,
            0.0, 0.0, 0, 0,
            float(x), float(y),
        )
        _X11.XFlush(self._dpy)

    def button(self, btn: int, press: bool) -> None:
        _Xtst.XTestFakeDeviceButtonEvent(
            self._dpy, self._pdev(), ctypes.c_uint(btn),
            1 if press else 0, None, 0, 0,
        )
        _X11.XFlush(self._dpy)

    def key(self, keycode: int, press: bool) -> None:
        _Xtst.XTestFakeDeviceKeyEvent(
            self._dpy, self._kdev(), ctypes.c_uint(keycode),
            1 if press else 0, None, 0, 0,
        )
        _X11.XFlush(self._dpy)
