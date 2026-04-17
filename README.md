# wstool.py

Screenshot, click, drag, and type on the X11 desktop. Every action is
published to an NDJSON event bus that can be streamed live with the
built-in `trace` viewer.

## Requirements

- Linux with an X11 display (`$DISPLAY` set). Wayland sessions need XWayland.
- Python 3.10+
- Packages: `mss`, `python-xlib`, `Pillow`. The `trace` viewer also needs
  `inotify_simple` and `rich`.

### Setup

```sh
python3 -m venv .venv
.venv/bin/pip install mss python-xlib Pillow inotify_simple rich
```

Run everything through `.venv/bin/python wstool.py ...` (or activate the
venv first).

## Quick start

```sh
# Screenshot the primary monitor (writes to /tmp/wstool-shots/ and archives)
python wstool.py shot

# Left-click at (640, 480)
python wstool.py click 640 480

# Click a field, then type into it, then press Enter
python wstool.py click 300 200 --type "hello world" --enter
```

`click X Y` is accepted as a shortcut for `click click X Y`.

## Subcommands

### shot — screenshot (ephemeral, archived)

Screenshots are saved as reduced-quality JPEGs under `/tmp/wstool-shots/`,
then moved into `/tmp/wstool-shots.tar` by a background thread so loose
files don't accumulate in the workspace. Shots are treated as ephemeral
logging artifacts — `/tmp` is cleared on reboot, which is the rotation
policy.

```sh
python wstool.py shot [--name NAME] [-m MONITOR] [-r X,Y,W,H]
                      [--quality Q] [--no-archive]
```

- `--name` — basename under `/tmp/wstool-shots/` (default
  `shot-<epoch_ms>.jpg`). Directory components are stripped; extension
  is forced to `.jpg`.
- `-m, --monitor` — monitor index per `mss` (0 = virtual all-monitors,
  1 = first physical, …). Default `0`.
- `-r, --region` — crop rectangle `X,Y,W,H` in screen pixels.
- `--quality` — JPEG quality 1–95 (default 40; trades legibility for
  size).
- `--no-archive` — keep the loose JPEG in `/tmp/wstool-shots/`; skip the
  background tar append.

Retrieve a past shot:

```sh
tar -tf /tmp/wstool-shots.tar                 # list
tar -xf /tmp/wstool-shots.tar shot-<ts>.jpg   # extract one to cwd
```

### click — click (and optionally type)

```sh
python wstool.py click X Y [-b BUTTON] [-n COUNT] [-d DELAY]
                           [--type TEXT] [--enter] [--cps N]
```

- `-b` — `left` (default), `middle`, `right`, `scrollup`, `scrolldown`
- `-n` — click count (e.g. `-n 2` for double-click)
- `-d` — inter-click delay in seconds
- `--type TEXT` — type literal text after the click
- `--enter` — press Return after clicking/typing
- `--cps` — typing rate (chars/sec, `0` = as fast as possible)

### dblclick — double-click

```sh
python wstool.py dblclick X Y
```

### drag — press, move, release

```sh
python wstool.py drag X1 Y1 X2 Y2 [-b BUTTON] [--duration S] [--steps N]
```

### type — type text at the current focus

```sh
python wstool.py type hello world [--enter] [--cps N]
```

All positional args are joined with spaces. Quote the string to preserve
multiple spaces.

### key — press a named key or combo

```sh
python wstool.py key Return
python wstool.py key ctrl+c
python wstool.py key shift+Tab
python wstool.py key ctrl+shift+t
```

Combos are `mod+mod+…+key` joined with `+`. Modifiers: `ctrl`, `shift`,
`alt`, `super`. Common named keys: `Return`, `Escape`, `Tab`, `BackSpace`,
`Delete`, `Up`/`Down`/`Left`/`Right`, `Home`/`End`, `Page_Up`/`Page_Down`,
plus any X keysym name.

### move — move the cursor

```sh
python wstool.py move X Y
```

### script — run a sequence

```sh
python wstool.py script path/to/file.txt
python wstool.py script --help-script   # show script syntax
cat steps.txt | python wstool.py script
```

Script format (one command per line, `#` starts a comment):

```
move X Y
click X Y [button] [count]
dblclick X Y
drag X1 Y1 X2 Y2 [duration]
type some literal text
key Return            # or ctrl+c, shift+Tab, etc.
sleep 0.5
```

### trace — live-view the event bus

```sh
python wstool.py trace [--replay] [--scrollback N] [--refresh HZ]
```

Renders a full-screen viewer that tails the NDJSON bus and shows events
from every concurrent `wstool.py` invocation sharing the same bus path.

- `--replay` — start from the beginning of the bus file instead of its tail
- `--scrollback` — rows kept in memory (default `500`,
  overridable via `CLICK_BUFFER`)
- `--refresh` — redraw rate in Hz (default `20`)

## Global options

These go **before** the subcommand:

- `-t, --trace` — print the trace UI inline to stdout for this run
- `--focus LABEL` — label shown in trace output (default: subcommand name)
- `--bus PATH` — NDJSON bus path
- `--no-bus` — don't publish events to the bus

## The event bus

Every action emits structured events (`session`, `pointer`, `click`, `key`,
`type`, `drag`, `shot`, `script`, `bus`) as one JSON object per line.

Bus path resolution order:

1. `--bus PATH`
2. `$CLICK_BUS`
3. `$XDG_RUNTIME_DIR/click/bus.ndjson` (fallback: `~/.cache/click/bus.ndjson`)

Writes use a bounded ring buffer drained on a background thread; if the
buffer saturates, oldest events are dropped and a synthetic `bus.drop`
event is emitted so the viewer can surface the gap.

## Environment variables

- `DISPLAY` — X11 display to target (required)
- `CLICK_BUS` — default bus path
- `CLICK_BUFFER` — ring-buffer / scrollback capacity (default `4096` for
  the writer, `500` for the viewer)
- `XDG_RUNTIME_DIR` — used to derive the default bus path

## Examples

Screenshot a 300×200 region at (100, 150):

```sh
python wstool.py shot -r 100,150,300,200 --name region.jpg
```

Right-click, wait, then Escape:

```sh
python wstool.py click 800 400 -b right
sleep 0.2
python wstool.py key Escape
```

Fill a login form from a script:

```
# login.wst
click 520 310
type alice@example.com
key Tab
type hunter2
key Return
```

```sh
python wstool.py script login.wst
```

Watch events live in another terminal while you run commands:

```sh
# terminal A
python wstool.py trace

# terminal B
python wstool.py click 640 480 --type "hello" --enter
```

## Exit codes

- `0` — success
- `1` — runtime error (message emitted as a `session.error` bus event; also
  raised to stderr unless `--trace` is set)
