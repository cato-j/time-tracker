# Time Tracker

A small desktop window (Python 3 + GTK 3) with named, persistent timers shown as pills,
and a built-in MCP server while the window is open.

## Run

```bash
python3 time_tracker.py            # MCP on http://127.0.0.1:8765/mcp
python3 time_tracker.py --port 9000
TIME_TRACKER_MCP_PORT=9000 python3 time_tracker.py
```

Requires `python3-gi` and GTK 3 (already present on most Linux desktops). No other packages.

## Desktop icon and app menu entry

```bash
./install.sh              # adds "Time Tracker" to the app menu and to ~/Desktop
./install.sh --uninstall  # removes it again
```

The script fills in the project path, installs `icon.svg` as the `time-tracker` icon, and marks the
desktop launcher as trusted so GNOME shows it as a clickable icon. Re-run it if you move the folder.

## Use

- Type a value (max 10 characters) in the text box and press **Enter** to start a timer for it.
- Each timer is a pill showing its name: **green** = running, **grey** = paused.
- Hover a pill to see the timespan recorded so far, when it was created and when it was last started.
- Click the pill body to pause it. Click it again to resume.
- Click the **✕** on the pill to stop the timer and close it.
- Entering a value that already has a paused timer resumes that timer.
- Tick **Always on top** to keep the window above other windows. The choice is remembered.

## MCP server

While the window is open it serves MCP over Streamable HTTP on localhost only
(the URL is shown at the bottom of the window). Register it with Claude Code:

```bash
claude mcp add --transport http time-tracker http://127.0.0.1:8765/mcp
```

Tools:

| Tool           | Arguments                | Effect                                                    |
|----------------|--------------------------|-----------------------------------------------------------|
| `add_timer`    | `name`                   | Start a timer, or resume it if a paused one has that name |
| `pause_timer`  | `name`                   | Pause a running timer                                     |
| `resume_timer` | `name`                   | Resume a paused timer                                     |
| `stop_timer`   | `name`                   | Stop the timer and remove it from the window              |
| `list_timers`  |                          | All timers with state and recorded time                   |
| `read_log`     | `since?`, `limit?`       | Log lines. `since` is ISO (`2026-09-28T10:00`) or relative (`30m`, `2h`, `1d`) |

Changes made through MCP appear in the window immediately and are logged with source `mcp`.

## Files (created next to the script)

| File          | Purpose                                                                 |
|---------------|-------------------------------------------------------------------------|
| `timers.log`  | Append-only log: timestamp, action (START/PAUSE/RESUME/STOP), name, elapsed, source (ui/mcp). |
| `timers.json` | Snapshot of all running and paused timers plus settings, rewritten on every change. |

On startup `timers.json` is read and every timer is restored in the state it was in.
A timer that was running when the app closed keeps counting the time that passed while it was closed,
because the file stores the moment it was last started rather than a frozen elapsed value.

## Notes

- The app defaults to the X11 backend (`GDK_BACKEND=x11`) because GTK 3 cannot keep a window on top
  under native Wayland. Set `GDK_BACKEND=wayland` yourself if you prefer it and don't need always-on-top.
- The MCP endpoint binds to 127.0.0.1 and rejects non-localhost `Origin` headers.
