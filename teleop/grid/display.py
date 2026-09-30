"""Which X authorization file lets a process started over ssh draw on Host B's screen.

Standard library only: runs on Host B.

The subscriber renders to B's desktop (DISPLAY=:0, WAYLAND_DISPLAY unset, so it goes through
Xwayland). Started from a desktop terminal it inherits XAUTHORITY; started by the grid agent
over ssh it does not, and Xwayland refuses the connection ("Authorization required, but no
authorization protocol specified"). The subscriber then panics before it negotiates any video.
That is how the first smoke cell of 2026-09-30 died, four seconds after it started.

GNOME on Wayland puts the file at /run/user/<uid>/.mutter-Xwaylandauth.<random>, and the
random part changes at every login, so it cannot live in a config file. It is read from the
running desktop session instead, in this order:
  1. host.yaml `xauthority`, if set and the file exists (an explicit override);
  2. XAUTHORITY from the environment of this user's gnome-shell or Xwayland process;
  3. the newest /run/user/<uid>/.mutter-Xwaylandauth.*;
  4. /run/user/<uid>/gdm/Xauthority, then ~/.Xauthority.
"""
from __future__ import annotations

import glob
import os
from pathlib import Path

SESSION_PROCESSES = ("gnome-shell", "Xwayland", "mutter", "gnome-session-binary")


def _environ_of(pid: str) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    out = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.partition(b"=")
        if sep:
            out[k.decode(errors="replace")] = v.decode(errors="replace")
    return out


def _session_xauthority(uid: int) -> str | None:
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            continue
        if comm not in SESSION_PROCESSES:
            continue
        xa = _environ_of(pid).get("XAUTHORITY")
        if xa and os.path.isfile(xa):
            return xa
    return None


def find_xauthority(cfg: dict | None = None) -> tuple[str | None, str]:
    """(path or None, how it was found)."""
    uid = os.getuid()
    explicit = (cfg or {}).get("xauthority") or ""
    if explicit and os.path.isfile(os.path.expanduser(explicit)):
        return os.path.expanduser(explicit), "host.yaml xauthority"
    found = _session_xauthority(uid)
    if found:
        return found, "desktop session environment"
    mutter = sorted(glob.glob(f"/run/user/{uid}/.mutter-Xwaylandauth.*"), key=os.path.getmtime)
    if mutter:
        return mutter[-1], "newest mutter Xwayland auth file"
    for p in (f"/run/user/{uid}/gdm/Xauthority", os.path.expanduser("~/.Xauthority")):
        if os.path.isfile(p):
            return p, p
    return None, "no X authorization file found"
