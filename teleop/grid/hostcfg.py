"""Per-host configuration: ~/.config/teleop/host.yaml and ~/.config/teleop/sfu.env.

Runs on both hosts: standard library plus PyYAML only.

The file is validated against exactly the keys in CONTRACT.md (plus the control-plane
additions recorded there). A missing key fails loudly by name; an unknown key fails too,
because a misspelt key that is silently ignored is a default nobody chose.

The SFU hostname is never in the repo. It lives in sfu.env as TELEOP_SFU_HOST and is read
by `sfu_host()`, which refuses rather than falling back: a run that silently went to a
retired SFU with the same room name received nothing and looked like total loss.
"""
from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("TELEOP_CONFIG_DIR", "~/.config/teleop")).expanduser()

# key -> (type, required-on-role or None for both)
REQUIRED = {
    "role": str,
    "peer": str,
    "wwan_iface": str,
    "ptp_iface": str,
    "ptp_role": str,
    "modem_index": int,
    "diag_tty": str,
    "diag_venv_python": str,
    "credentials_env": str,
    "tcpdump": str,
    "results_root": str,
    "display": str,
    "repo": str,
}
OPTIONAL = {
    # added by control-plane (CONTRACT.md): where the checkout lives on the PEER, when it
    # differs from this host's `repo`. Defaults to `repo`.
    "peer_repo": str,
    # added by control plane v2: read on A (the orchestrator). true = do not purge B's copy of
    # a cell after A has pulled and verified it. Default false: Host B keeps nothing.
    "b_keep_after_pull": bool,
}
OPTIONAL_DEFAULTS = {"b_keep_after_pull": False}
PATH_KEYS = ("diag_venv_python", "credentials_env", "results_root", "repo", "peer_repo")


class HostConfigError(RuntimeError):
    pass


def _yaml():
    try:
        import yaml  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover
        raise HostConfigError("PyYAML is not installed (apt install python3-yaml)") from e
    return yaml


def validate(raw: dict, source: str = "host.yaml") -> dict:
    if not isinstance(raw, dict):
        raise HostConfigError(f"{source}: top level must be a mapping")
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise HostConfigError(f"{source}: missing key(s): {', '.join(missing)}")
    unknown = [k for k in raw if k not in REQUIRED and k not in OPTIONAL]
    if unknown:
        raise HostConfigError(f"{source}: unknown key(s): {', '.join(unknown)}")
    cfg: dict = {}
    for k, typ in {**REQUIRED, **OPTIONAL}.items():
        if k not in raw:
            continue
        v = raw[k]
        if v is None:
            v = OPTIONAL_DEFAULTS.get(k, "")
        if typ is bool:
            if isinstance(v, str) and v.strip().lower() in ("true", "yes", "on", "1", "false", "no", "off", "0"):
                v = v.strip().lower() in ("true", "yes", "on", "1")
            if isinstance(v, int) and not isinstance(v, bool) and v in (0, 1):
                v = bool(v)
            if not isinstance(v, bool):
                raise HostConfigError(f"{source}: {k} must be true or false, got {v!r}")
        elif typ is int:
            if isinstance(v, bool) or not isinstance(v, int):
                try:
                    v = int(str(v))
                except ValueError:
                    raise HostConfigError(f"{source}: {k} must be an integer, got {v!r}") from None
        else:
            if not isinstance(v, (str, int, float)):
                raise HostConfigError(f"{source}: {k} must be a string, got {type(v).__name__}")
            v = str(v)
        cfg[k] = v
    if cfg["role"] not in ("a", "b"):
        raise HostConfigError(f"{source}: role must be 'a' or 'b', got {cfg['role']!r}")
    if cfg["ptp_role"] not in ("master", "slave"):
        raise HostConfigError(f"{source}: ptp_role must be 'master' or 'slave', got {cfg['ptp_role']!r}")
    for k in ("peer", "wwan_iface", "ptp_iface", "diag_tty", "diag_venv_python",
              "credentials_env", "tcpdump", "results_root", "repo"):
        if not cfg[k].strip():
            raise HostConfigError(f"{source}: {k} is empty")
    if cfg["role"] == "b" and not cfg["display"].strip():
        raise HostConfigError(f"{source}: display is empty; Host B's subscriber renders to it")
    for k in PATH_KEYS:
        if k in cfg and cfg[k]:
            cfg[k] = str(Path(cfg[k]).expanduser())
    cfg.setdefault("peer_repo", cfg["repo"])
    for k, v in OPTIONAL_DEFAULTS.items():
        cfg.setdefault(k, v)
    return cfg


def load(path: str | os.PathLike | None = None) -> dict:
    """Read and validate host.yaml. TELEOP_HOST_YAML overrides the location (tests)."""
    p = Path(path or os.environ.get("TELEOP_HOST_YAML") or CONFIG_DIR / "host.yaml").expanduser()
    if not p.is_file():
        raise HostConfigError(f"{p} does not exist; copy teleop/config/host.example.yaml there and fill it in")
    with open(p) as f:
        raw = _yaml().safe_load(f)
    return validate(raw, str(p))


def parse_env_file(text: str) -> dict:
    """KEY=VALUE lines, optional `export`, optional single/double quotes. No expansion."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out


def sfu_host(path: str | os.PathLike | None = None) -> str:
    """TELEOP_SFU_HOST from sfu.env. Never defaulted: an unset SFU fails here, by name."""
    p = Path(path or os.environ.get("TELEOP_SFU_ENV") or CONFIG_DIR / "sfu.env").expanduser()
    if not p.is_file():
        raise HostConfigError(f"{p} does not exist; it must set TELEOP_SFU_HOST")
    host = parse_env_file(p.read_text()).get("TELEOP_SFU_HOST", "").strip()
    if not host:
        raise HostConfigError(f"{p}: TELEOP_SFU_HOST is not set")
    if "/" in host or " " in host:
        raise HostConfigError(f"{p}: TELEOP_SFU_HOST must be a bare hostname, not a URL")
    return host


def host_dir_name(role: str) -> str:
    return "hosta" if role == "a" else "hostb"
