#!/usr/bin/env python3
"""Time Tracker - a small GTK window with persistent named timers shown as pills,
plus a built-in MCP server (Streamable HTTP) while the window is open.

Type a value (max 10 characters) in the text box, optionally an estimate in the
second box (e.g. "2h", "90m", "1h30m" - a plain number is minutes), and press
Enter to start a timer. The estimate is a manually-entered value only (meant to
be copied in from Jira); this app never talks to Jira itself.
  * Green pill  = running, no estimate, or running and well under estimate.
  * Grey pill   = paused, regardless of estimate.
  * From 95% of the estimate, a running pill fades from green to red, reaching
    full red exactly at 100%. Past 100% it flashes between red and dark red.
  * Hover a pill to see the timespan recorded so far, and the estimate/overtime.
  * Click the pill body to pause / resume it.
  * Click the clock icon to set or change its estimate.
  * Click the X on the pill to stop the timer and remove it.
  * "Always on top" keeps the window above other windows.

Every start / pause / resume / stop is appended to timers.log, and the full set of
timers is written to timers.json on every change. On startup the file is read back
and each timer is restored in the state it was in (running timers keep counting the
time that passed while the app was closed).

MCP: while running, an MCP server listens on http://127.0.0.1:8765/mcp (override the
port with --port N or TIME_TRACKER_MCP_PORT). Tools: add_timer, set_estimate,
pause_timer, resume_timer, stop_timer, list_timers, read_log.
"""

import itertools
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# "Always on top" is not supported by GTK3 on Wayland; prefer X11/XWayland unless
# the user explicitly picked a backend.
os.environ.setdefault("GDK_BACKEND", "x11")

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(APP_DIR, "timers.json")
LOG_FILE = os.path.join(APP_DIR, "timers.log")
ICON_FILE = os.path.join(APP_DIR, "icon.svg")
MAX_LEN = 10
DEFAULT_PORT = 8765
MCP_PATH = "/mcp"
SERVER_NAME = "time-tracker"
SERVER_VERSION = "1.1.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

CSS = b"""
.pill {
    border-radius: 999px;
    padding: 2px 2px 2px 6px;
    color: white;
}
.pill.running { background-color: #2e9e4f; }
.pill.paused  { background-color: #8e8e8e; }
.pill button {
    background: transparent;
    background-image: none;
    border: none;
    box-shadow: none;
    padding: 4px 10px;
    min-height: 0;
    min-width: 0;
    color: white;
    font-weight: bold;
}
.pill button:hover { background-color: rgba(0, 0, 0, 0.15); border-radius: 999px; }
.pill .close { font-weight: bold; padding: 4px 8px; }
.pill .close:hover { background-color: rgba(0, 0, 0, 0.3); }
.status { color: #777; font-size: 90%; }
"""

# Colors for the estimate fade/flash. Plain green/grey come from the static
# CSS above; these are blended in Python and pushed per-pill via a small
# per-widget CssProvider, since CSS alone can't do a continuous fade.
COLOR_GREEN = (0x2e, 0x9e, 0x4f)
COLOR_RED = (0xc0, 0x39, 0x2b)
COLOR_DARK_RED = (0x6b, 0x10, 0x10)
FADE_START_RATIO = 0.95  # start fading to red at 95% of the estimate


def _lerp_color(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(round(c1[i] + (c2[i] - c1[i]) * t)) for i in range(3))


def _color_to_hex(c):
    return f"#{c[0]:02x}{c[1]:02x}{c[2]:02x}"


def fmt(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def stamp(epoch=None) -> str:
    return datetime.fromtimestamp(epoch if epoch is not None else time.time()).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- model
class Timer:
    def __init__(self, name, accumulated=0.0, running=True, started_at=None, created_at=None,
                 estimate_seconds=None):
        self.name = name
        self.accumulated = float(accumulated)  # seconds collected while paused
        self.running = bool(running)
        if self.running and started_at is None:
            started_at = time.time()
        self.started_at = started_at if self.running else None
        self.created_at = created_at if created_at is not None else time.time()
        # Manually entered (e.g. copied from Jira) - never fetched automatically.
        self.estimate_seconds = float(estimate_seconds) if estimate_seconds else None

    def elapsed(self) -> float:
        if self.running:
            return self.accumulated + (time.time() - self.started_at)
        return self.accumulated

    def estimate_ratio(self):
        """Elapsed / estimate, or None if there is no estimate."""
        if not self.estimate_seconds:
            return None
        return self.elapsed() / self.estimate_seconds

    def pause(self):
        if self.running:
            self.accumulated = self.elapsed()
            self.running = False
            self.started_at = None

    def resume(self):
        if not self.running:
            self.started_at = time.time()
            self.running = True

    def to_dict(self):
        return {
            "name": self.name,
            "accumulated": self.accumulated,
            "running": self.running,
            "started_at": self.started_at,
            "created_at": self.created_at,
            "estimate_seconds": self.estimate_seconds,
        }

    def info(self):
        d = {
            "name": self.name,
            "state": "running" if self.running else "paused",
            "elapsed": fmt(self.elapsed()),
            "elapsed_seconds": int(self.elapsed()),
            "created_at": stamp(self.created_at),
            "last_started_at": stamp(self.started_at) if self.running else None,
            "estimate_seconds": self.estimate_seconds,
            "estimate": fmt(self.estimate_seconds) if self.estimate_seconds else None,
        }
        if self.estimate_seconds:
            remaining = self.estimate_seconds - self.elapsed()
            d["remaining"] = fmt(remaining) if remaining >= 0 else None
            d["overtime"] = fmt(-remaining) if remaining < 0 else None
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(
            name=str(d["name"])[:MAX_LEN],
            accumulated=d.get("accumulated", 0.0),
            running=d.get("running", False),
            started_at=d.get("started_at"),
            created_at=d.get("created_at"),
            estimate_seconds=d.get("estimate_seconds"),
        )


def load_state():
    """Returns (timers, settings)."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        timers = [Timer.from_dict(d) for d in data.get("timers", [])]
        settings = data.get("settings", {}) if isinstance(data.get("settings"), dict) else {}
        return timers, settings
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return [], {}


def save_state(timers, settings):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {"saved_at": datetime.now().isoformat(timespec="seconds"),
             "settings": settings,
             "timers": [t.to_dict() for t in timers]},
            f, indent=2,
        )
    os.replace(tmp, STATE_FILE)


def log_action(action, timer, source="ui"):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{stamp()}\t{action:<6}\t{timer.name}\t{fmt(timer.elapsed())}\t{source}\n")


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.I)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION_CHUNK_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([smhdw])", re.I)


def parse_duration_seconds(value):
    """Accepts a duration for an estimate: '2h', '90m', '1h30m', or a bare
    number (treated as minutes). Returns seconds, or None for a blank value
    (which clears the estimate)."""
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    total = 0.0
    matched = False
    for m in _DURATION_CHUNK_RE.finditer(text):
        total += float(m.group(1)) * _DURATION_UNITS[m.group(2).lower()]
        matched = True
    if matched:
        return total
    try:
        return float(text) * 60  # bare number -> minutes
    except ValueError:
        raise ValueError(
            f"Unrecognised estimate {text!r}. Use e.g. '2h', '90m', '1h30m', "
            "or a bare number of minutes."
        )


def format_duration_input(seconds):
    """The inverse of parse_duration_seconds, for pre-filling the edit dialog."""
    if not seconds:
        return ""
    total_min = round(seconds / 60)
    h, m = divmod(total_min, 60)
    if h and m:
        return f"{h}h{m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def parse_since(value):
    """Accepts an ISO date/time ('2026-09-28', '2026-09-28T10:00', '2026-09-28 10:00:00')
    or a relative duration ('30m', '2h', '1d'). Returns a datetime or None for 'everything'."""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    m = _DURATION_RE.match(text)
    if m:
        return datetime.now() - timedelta(seconds=float(m.group(1)) * _DURATION_UNITS[m.group(2).lower()])
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"Unrecognised 'since' value {text!r}. Use an ISO date/time such as "
            "'2026-09-28T10:00' or a duration such as '30m', '2h', '1d'."
        )


def read_log(since=None, limit=None):
    cutoff = parse_since(since)
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    except FileNotFoundError:
        lines = []
    if cutoff is not None:
        kept = []
        for ln in lines:
            try:
                ts = datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if ts >= cutoff:
                kept.append(ln)
        lines = kept
    if limit:
        lines = lines[-int(limit):]
    return lines, cutoff


# ------------------------------------------------------------------------ MCP tools
TOOLS = [
    {
        "name": "add_timer",
        "description": f"Start a new timer with the given name (max {MAX_LEN} characters). "
                       "If a paused timer with that name exists it is resumed instead. "
                       "An optional estimate (manually entered, e.g. copied from Jira - this "
                       "tool never talks to Jira) makes the pill fade from green to red as the "
                       "estimate runs out and flash red/dark-red once it is exceeded.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": MAX_LEN, "description": "Timer name"},
                "estimate": {
                    "type": "string",
                    "description": "Optional estimate, e.g. '2h', '90m', '1h30m', or a bare "
                                   "number of minutes. Omit for no estimate.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "set_estimate",
        "description": "Set, change, or clear a timer's estimate. The pill fades from green to "
                       "red as the estimate runs out and flashes red/dark-red once exceeded.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Timer name"},
                "estimate": {
                    "type": "string",
                    "description": "e.g. '2h', '90m', '1h30m', or a bare number of minutes. "
                                   "Omit or send an empty string to clear the estimate.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "pause_timer",
        "description": "Pause a running timer. Its recorded time is kept and it stays visible as a grey pill.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Timer name"}},
            "required": ["name"],
        },
    },
    {
        "name": "resume_timer",
        "description": "Resume a paused timer.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Timer name"}},
            "required": ["name"],
        },
    },
    {
        "name": "stop_timer",
        "description": "Stop a timer and remove it from the window. Its final elapsed time is written to the log.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Timer name"}},
            "required": ["name"],
        },
    },
    {
        "name": "list_timers",
        "description": "List all current timers with their state (running/paused) and recorded time.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_log",
        "description": "Read the action log (START/PAUSE/RESUME/STOP lines). Optionally only entries "
                       "since a point in time.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": "ISO date/time (e.g. '2026-09-28T10:00') or a relative duration "
                                   "(e.g. '30m', '2h', '1d'). Omit for the whole log.",
                },
                "limit": {"type": "integer", "minimum": 1, "description": "Return at most the last N lines."},
            },
        },
    },
]


class ToolError(Exception):
    pass


def _clean_name(args):
    name = str(args.get("name", "")).strip()
    if not name:
        raise ToolError("'name' is required.")
    if len(name) > MAX_LEN:
        raise ToolError(f"'name' must be at most {MAX_LEN} characters (got {len(name)}).")
    return name


def _clean_estimate(args):
    try:
        return parse_duration_seconds(args.get("estimate"))
    except ValueError as e:
        raise ToolError(str(e))


def dispatch_tool(app, name, args):
    """Runs a tool. GUI-touching tools are marshalled onto the GTK main thread."""
    args = args or {}
    if name == "add_timer":
        n = _clean_name(args)
        est = _clean_estimate(args)
        return app.call_on_main(lambda: app.api_add(n, est))
    if name == "set_estimate":
        n = _clean_name(args)
        est = _clean_estimate(args)
        return app.call_on_main(lambda: app.api_set_estimate(n, est))
    if name == "pause_timer":
        n = _clean_name(args)
        return app.call_on_main(lambda: app.api_pause(n))
    if name == "resume_timer":
        n = _clean_name(args)
        return app.call_on_main(lambda: app.api_resume(n))
    if name == "stop_timer":
        n = _clean_name(args)
        return app.call_on_main(lambda: app.api_stop(n))
    if name == "list_timers":
        return app.call_on_main(app.api_list)
    if name == "read_log":
        try:
            lines, cutoff = read_log(args.get("since"), args.get("limit"))
        except ValueError as e:
            raise ToolError(str(e))
        return {
            "since": cutoff.strftime("%Y-%m-%d %H:%M:%S") if cutoff else None,
            "count": len(lines),
            "columns": ["timestamp", "action", "timer", "elapsed", "source"],
            "lines": lines,
        }
    raise ToolError(f"Unknown tool: {name}")


# ------------------------------------------------------------------- MCP transport
class MCPHandler(BaseHTTPRequestHandler):
    """Minimal MCP 'Streamable HTTP' transport: JSON-RPC 2.0 over POST, JSON responses.
    No server-initiated SSE stream is offered (GET returns 405), which is allowed by the spec."""

    app = None  # set by start_mcp_server
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt_, *args):  # keep the terminal quiet
        pass

    # ---- helpers
    def _send(self, status, body=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = origin.split("://", 1)[-1].split(":", 1)[0].lower()
        return host in ("localhost", "127.0.0.1", "[::1]", "::1")

    # ---- HTTP verbs
    def do_GET(self):
        if self.path.rstrip("/") != MCP_PATH:
            return self._send(404, {"error": "not found"})
        self._send(405, {"error": "SSE stream not supported; POST JSON-RPC to this endpoint"},
                   {"Allow": "POST, DELETE"})

    def do_DELETE(self):
        self._send(200)

    def do_POST(self):
        if self.path.rstrip("/") != MCP_PATH:
            return self._send(404, {"error": "not found"})
        if not self._origin_ok():
            return self._send(403, {"error": "forbidden origin"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"null")
        except (ValueError, TypeError):
            return self._send(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": "Parse error"}})

        messages = payload if isinstance(payload, list) else [payload]
        responses = [r for r in (self._handle(m) for m in messages) if r is not None]
        if not responses:
            return self._send(202)  # notifications / responses only
        self._send(200, responses if isinstance(payload, list) else responses[0])

    # ---- JSON-RPC
    def _handle(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "Invalid Request"}}
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}
        if method is None:
            return None  # a response from the client; nothing to do
        is_notification = "id" not in msg

        try:
            result = self._call(method, params)
        except ToolError as e:
            # Tool execution errors are reported inside the result, per MCP.
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except KeyError:
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"}}
        except Exception as e:  # noqa: BLE001
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32603, "message": f"Internal error: {e}"}}
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _call(self, method, params):
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": "Controls the Time Tracker desktop window. Timer names are at most "
                                f"{MAX_LEN} characters. Use list_timers to see what is running. "
                                "An optional, manually-entered estimate (e.g. '2h', '90m') makes "
                                "a pill fade to red near the deadline and flash once overtime; "
                                "set it via add_timer or change it later with set_estimate.",
            }
        if method == "ping":
            return {}
        if method in ("notifications/initialized", "notifications/cancelled",
                      "notifications/roots/list_changed"):
            return None
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            name = params.get("name")
            if not any(t["name"] == name for t in TOOLS):
                raise ToolError(f"Unknown tool: {name}")
            data = dispatch_tool(self.app, name, params.get("arguments") or {})
            return {
                "content": [{"type": "text", "text": json.dumps(data, indent=2)}],
                "structuredContent": data,
            }
        raise KeyError(method)


def start_mcp_server(app, port):
    """Starts the HTTP server on a daemon thread. Returns (server, url) or raises OSError."""
    MCPHandler.app = app
    server = ThreadingHTTPServer(("127.0.0.1", port), MCPHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="mcp-http", daemon=True).start()
    return server, f"http://127.0.0.1:{port}{MCP_PATH}"


# ---------------------------------------------------------------------------- view
_pill_counter = itertools.count()


class Pill(Gtk.Box):
    def __init__(self, timer, on_toggle, on_close, on_hover, on_edit_estimate):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.timer = timer
        ctx = self.get_style_context()
        ctx.add_class("pill")
        # A unique class lets the per-pill CssProvider below target only this
        # pill, at higher specificity than .pill.running/.paused, so it can
        # override the base color for the estimate fade/flash - or be cleared
        # back to an empty stylesheet to let the base color show through.
        self.dyn_class = f"pill-dyn-{next(_pill_counter)}"
        ctx.add_class(self.dyn_class)
        self._dyn_provider = Gtk.CssProvider()
        ctx.add_provider(self._dyn_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.muted = False  # right-click freezes the overtime flash to solid red

        self.label = Gtk.Label(label=timer.name)
        self.body = Gtk.Button(relief=Gtk.ReliefStyle.NONE)
        self.body.add(self.label)
        self.body.connect("clicked", lambda *_: on_toggle(timer))
        self.body.connect("button-press-event", self.on_body_button_press)
        self.body.set_has_tooltip(True)
        self.body.connect("query-tooltip", self.on_query_tooltip)
        self.body.connect("enter-notify-event", lambda *_: on_hover(self, True))
        self.body.connect("leave-notify-event", lambda *_: on_hover(self, False))

        self.estimate_btn = Gtk.Button(label="⏱", relief=Gtk.ReliefStyle.NONE)
        self.estimate_btn.get_style_context().add_class("close")
        self.estimate_btn.set_tooltip_text("Set / change the estimate")
        self.estimate_btn.connect("clicked", lambda *_: on_edit_estimate(timer))

        self.close = Gtk.Button(label="✕", relief=Gtk.ReliefStyle.NONE)
        self.close.get_style_context().add_class("close")
        self.close.set_tooltip_text("Stop and close this timer")
        self.close.connect("clicked", lambda *_: on_close(timer))

        self.pack_start(self.body, False, False, 0)
        self.pack_start(self.estimate_btn, False, False, 0)
        self.pack_start(self.close, False, False, 0)
        self.refresh()
        self.update_color(True)

    def tooltip_text(self):
        t = self.timer
        state = "Running" if t.running else "Paused"
        lines = [t.name, f"{state} · recorded {fmt(t.elapsed())}", f"Created {stamp(t.created_at)}"]
        if t.running:
            lines.append(f"Running since {stamp(t.started_at)}")
        if t.estimate_seconds:
            lines.append(f"Estimate {fmt(t.estimate_seconds)}")
            remaining = t.estimate_seconds - t.elapsed()
            if remaining >= 0:
                lines.append(f"Remaining {fmt(remaining)}")
            else:
                lines.append(f"Over by {fmt(-remaining)}")
                lines.append("Right-click to silence the flash" if not self.muted else "Right-click to resume flashing")
        lines.append("Click to pause" if t.running else "Click to resume")
        return "\n".join(lines)

    def on_query_tooltip(self, widget, x, y, keyboard_mode, tooltip):
        tooltip.set_text(self.tooltip_text())
        return True

    def on_body_button_press(self, widget, event):
        if event.button == 3:  # right-click: toggle the overtime flash on/off
            self.muted = not self.muted
            self.update_color(True)
            return True
        return False

    def refresh(self):
        ctx = self.get_style_context()
        if self.timer.running:
            ctx.add_class("running")
            ctx.remove_class("paused")
        else:
            ctx.add_class("paused")
            ctx.remove_class("running")

    def update_color(self, flash_on):
        """Recomputes the estimate fade/flash override. flash_on alternates on
        a fast timer so overtime pills visibly blink; it is ignored otherwise."""
        t = self.timer
        if not t.running or not t.estimate_seconds:
            self._dyn_provider.load_from_data(b"")
            return
        ratio = t.estimate_ratio()
        if ratio < FADE_START_RATIO:
            self._dyn_provider.load_from_data(b"")
            return
        if ratio < 1.0:
            frac = (ratio - FADE_START_RATIO) / (1.0 - FADE_START_RATIO)
            color = _lerp_color(COLOR_GREEN, COLOR_RED, frac)
        else:
            color = COLOR_RED if (self.muted or flash_on) else COLOR_DARK_RED
        css = f".pill.{self.dyn_class} {{ background-color: {_color_to_hex(color)}; }}"
        self._dyn_provider.load_from_data(css.encode())


class TimeTrackerWindow(Gtk.Window):
    def __init__(self, port=DEFAULT_PORT):
        super().__init__(title="Time Tracker")
        self.set_default_size(560, 320)
        if os.path.exists(ICON_FILE):
            try:
                self.set_icon_from_file(ICON_FILE)
            except GLib.Error:
                pass
        self.set_border_width(12)
        self.connect("destroy", self.on_destroy)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        timers, self.settings = load_state()

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(vbox)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        vbox.pack_start(top, False, False, 0)

        self.entry = Gtk.Entry()
        self.entry.set_max_length(MAX_LEN)
        self.entry.set_placeholder_text(f"Type a value (max {MAX_LEN} chars) and press Enter")
        self.entry.connect("activate", self.on_entry_activate)
        top.pack_start(self.entry, True, True, 0)

        self.estimate_entry = Gtk.Entry()
        self.estimate_entry.set_placeholder_text("Estimate (e.g. 2h, 90m) optional")
        self.estimate_entry.set_width_chars(20)
        self.estimate_entry.connect("activate", self.on_entry_activate)
        top.pack_start(self.estimate_entry, False, False, 0)

        self.on_top = Gtk.CheckButton(label="Always on top")
        self.on_top.set_active(bool(self.settings.get("always_on_top", False)))
        self.on_top.connect("toggled", self.on_top_toggled)
        top.pack_start(self.on_top, False, False, 0)

        # A manual wrap layout: GtkFlowBox snaps children into shared column
        # widths across rows even with homogeneous=False, so short pills end
        # up with visible gutters instead of packing tight. A plain vertical
        # box of row boxes, rebuilt on resize, gives true tight-pack wrapping.
        self.pills_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.pills_container.set_halign(Gtk.Align.FILL)
        self.pills_container.set_hexpand(True)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.pills_container)
        vbox.pack_start(scroller, True, True, 0)

        self.status = Gtk.Label(xalign=0)
        self.status.get_style_context().add_class("status")
        self.status.set_selectable(True)
        vbox.pack_start(self.status, False, False, 0)

        self.pills = {}  # name -> Pill, insertion order preserved
        self.hovered = None
        self._last_layout_width = -1
        for timer in timers:
            self.add_pill(timer)

        def _on_scroller_size_allocate(_widget, allocation):
            if allocation.width != self._last_layout_width:
                self._last_layout_width = allocation.width
                self.relayout_pills()

        scroller.connect("size-allocate", _on_scroller_size_allocate)

        self.mcp_server = None
        try:
            self.mcp_server, url = start_mcp_server(self, port)
            self.status.set_text(f"MCP server: {url}")
        except OSError as e:
            self.status.set_text(f"MCP server not started on port {port}: {e.strerror or e}")

        GLib.timeout_add(1000, self.tick)
        GLib.timeout_add(500, self.color_tick)
        self.show_all()
        self.set_keep_above(self.on_top.get_active())
        self.entry.grab_focus()

    # ---- helpers
    @property
    def timers(self):
        return [p.timer for p in self.pills.values()]

    def persist(self):
        save_state(self.timers, self.settings)

    def add_pill(self, timer):
        pill = Pill(timer, self.on_toggle, self.on_close, self.on_hover, self.on_edit_estimate)
        pill.set_hexpand(False)
        pill.set_halign(Gtk.Align.START)
        pill.show_all()
        self.pills[timer.name] = pill
        self.relayout_pills()

    def relayout_pills(self):
        """Rebuilds the row boxes so pills pack tightly, wrapping to a new
        row only when the next pill would not fit in the available width."""
        for pill in self.pills.values():
            parent = pill.get_parent()
            if parent is not None:
                parent.remove(pill)
        for row in list(self.pills_container.get_children()):
            self.pills_container.remove(row)
            row.destroy()

        spacing = 8
        avail_width = self.pills_container.get_allocated_width()
        if avail_width <= 1:
            avail_width = max(self.get_size()[0] - 24, 1)  # fallback before first allocation

        row = None
        row_width = 0
        for pill in self.pills.values():
            natural_w = pill.get_preferred_width()[1]
            needed = natural_w if row is None else spacing + natural_w
            if row is not None and row_width + needed > avail_width:
                row = None
            if row is None:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing)
                row.set_halign(Gtk.Align.START)
                self.pills_container.pack_start(row, False, False, 0)
                row_width = 0
                needed = natural_w
            row.pack_start(pill, False, False, 0)
            row_width += needed
        self.pills_container.show_all()

    def call_on_main(self, fn, timeout=5.0):
        """Run fn() on the GTK main thread and return its result (used by the MCP thread)."""
        done = threading.Event()
        box = {}

        def runner():
            try:
                box["result"] = fn()
            except Exception as e:  # noqa: BLE001
                box["error"] = e
            done.set()
            return False

        GLib.idle_add(runner)
        if not done.wait(timeout):
            raise ToolError("The window did not respond in time.")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    # ---- actions shared by UI and MCP (must run on the main thread)
    def start_timer(self, name, source, estimate_seconds=None):
        existing = self.pills.get(name)
        if existing:
            if estimate_seconds is not None:
                existing.timer.estimate_seconds = estimate_seconds
            if not existing.timer.running:
                existing.timer.resume()
                log_action("RESUME", existing.timer, source)
                existing.refresh()
                existing.update_color(True)
                self.persist()
                return existing.timer, "resumed"
            return existing.timer, "already running"
        timer = Timer(name, estimate_seconds=estimate_seconds)
        self.add_pill(timer)
        log_action("START", timer, source)
        self.persist()
        return timer, "started"

    def pause_timer(self, name, source):
        pill = self.pills.get(name)
        if not pill:
            raise ToolError(f"No timer named {name!r}.")
        if not pill.timer.running:
            return pill.timer, "already paused"
        pill.timer.pause()
        log_action("PAUSE", pill.timer, source)
        pill.refresh()
        self.persist()
        return pill.timer, "paused"

    def resume_timer(self, name, source):
        pill = self.pills.get(name)
        if not pill:
            raise ToolError(f"No timer named {name!r}.")
        if pill.timer.running:
            return pill.timer, "already running"
        pill.timer.resume()
        log_action("RESUME", pill.timer, source)
        pill.refresh()
        self.persist()
        return pill.timer, "resumed"

    def stop_timer(self, name, source):
        pill = self.pills.pop(name, None)
        if not pill:
            raise ToolError(f"No timer named {name!r}.")
        log_action("STOP", pill.timer, source)
        if self.hovered is pill:
            self.hovered = None
        pill.destroy()
        self.relayout_pills()
        self.persist()
        return pill.timer, "stopped"

    # ---- MCP entry points
    def api_add(self, name, estimate_seconds=None):
        timer, outcome = self.start_timer(name, "mcp", estimate_seconds)
        return {"outcome": outcome, "timer": timer.info()}

    def api_set_estimate(self, name, estimate_seconds):
        pill = self.pills.get(name)
        if not pill:
            raise ToolError(f"No timer named {name!r}.")
        pill.timer.estimate_seconds = estimate_seconds
        pill.muted = False
        pill.update_color(True)
        self.persist()
        return {"outcome": "updated", "timer": pill.timer.info()}

    def api_pause(self, name):
        timer, outcome = self.pause_timer(name, "mcp")
        return {"outcome": outcome, "timer": timer.info()}

    def api_resume(self, name):
        timer, outcome = self.resume_timer(name, "mcp")
        return {"outcome": outcome, "timer": timer.info()}

    def api_stop(self, name):
        timer, outcome = self.stop_timer(name, "mcp")
        return {"outcome": outcome, "timer": timer.info()}

    def api_list(self):
        return {"count": len(self.pills), "timers": [t.info() for t in self.timers]}

    # ---- UI events
    def on_entry_activate(self, entry):
        name = self.entry.get_text().strip()[:MAX_LEN]
        if not name:
            return
        try:
            estimate = parse_duration_seconds(self.estimate_entry.get_text())
        except ValueError as e:
            self.status.set_text(str(e))
            return
        self.entry.set_text("")
        self.estimate_entry.set_text("")
        self.start_timer(name, "ui", estimate)

    def on_toggle(self, timer):
        if timer.running:
            self.pause_timer(timer.name, "ui")
        else:
            self.resume_timer(timer.name, "ui")

    def on_close(self, timer):
        self.stop_timer(timer.name, "ui")

    def on_hover(self, pill, entered):
        self.hovered = pill if entered else None

    def on_edit_estimate(self, timer):
        dialog = Gtk.Dialog(
            title=f"Estimate for {timer.name}",
            transient_for=self,
            modal=True,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK,
        )
        box = dialog.get_content_area()
        box.set_border_width(10)
        box.set_spacing(6)
        box.add(Gtk.Label(label="Estimate (e.g. 2h, 90m, 1h30m). Leave blank to clear."))
        entry = Gtk.Entry()
        entry.set_text(format_duration_input(timer.estimate_seconds))
        entry.set_activates_default(True)
        box.add(entry)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.show_all()
        while True:
            response = dialog.run()
            if response != Gtk.ResponseType.OK:
                dialog.destroy()
                return
            try:
                estimate = parse_duration_seconds(entry.get_text())
            except ValueError as e:
                error = Gtk.MessageDialog(
                    transient_for=dialog, modal=True,
                    message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK,
                    text=str(e),
                )
                error.run()
                error.destroy()
                continue
            break
        dialog.destroy()
        pill = self.pills.get(timer.name)
        if pill is None:
            return
        pill.timer.estimate_seconds = estimate
        pill.muted = False
        pill.update_color(True)
        self.persist()

    def on_top_toggled(self, button):
        active = button.get_active()
        self.set_keep_above(active)
        self.settings["always_on_top"] = active
        self.persist()

    def tick(self):
        # Timespans are only shown on hover; keep the visible tooltip current.
        if self.hovered is not None and self.hovered.timer.running:
            self.hovered.body.trigger_tooltip_query()
        return True  # keep the timeout alive

    def color_tick(self):
        flash_on = int(time.time() * 2) % 2 == 0
        for pill in self.pills.values():
            pill.update_color(flash_on)
        return True  # keep the timeout alive

    def on_destroy(self, *_):
        self.persist()
        if self.mcp_server is not None:
            self.mcp_server.shutdown()
        Gtk.main_quit()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    port = int(os.environ.get("TIME_TRACKER_MCP_PORT", DEFAULT_PORT))
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    Gdk.set_program_class("time-tracker")  # lets GNOME pair the window with its launcher
    TimeTrackerWindow(port=port)
    Gtk.main()


if __name__ == "__main__":
    main()
