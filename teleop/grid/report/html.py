"""A tiny HTML builder: escaping, tables, and the shared page shell. No framework.

Pages are self-contained: CSS, data and scripts are inline, images are data: URIs.
"""
from __future__ import annotations

import base64
import html as _html
import io
import json
import os
import tempfile
from pathlib import Path

from . import style as S


def atomic_write(path: Path, write) -> None:
    """write(tmp_path) then rename over path, so a reader never sees a half-written report.
    The result gets the usual umask-derived mode (mkstemp alone would leave it 0600)."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    try:
        write(Path(tmp))
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def esc(v) -> str:
    return _html.escape("" if v is None else str(v), quote=True)


def tag(name: str, body: str = "", **attrs) -> str:
    """tag('td', 'x', cls='num') -> <td class="num">x</td>. body is trusted HTML."""
    parts = []
    for k, v in attrs.items():
        if v is None or v is False:
            continue
        k = {"cls": "class", "for_": "for"}.get(k, k).replace("_", "-")
        parts.append(k if v is True else f'{k}="{esc(v)}"')
    open_ = f"<{name}{' ' + ' '.join(parts) if parts else ''}>"
    return f"{open_}{body}</{name}>"


def table(headers: list[str], rows: list[list], *, num_cols: set[int] | None = None,
          cls: str = "t", row_cls: list[str | None] | None = None) -> str:
    """Rows are plain values (escaped) unless wrapped in Raw. Numeric columns right-aligned."""
    num_cols = num_cols if num_cols is not None else set()
    th = "".join(tag("th", esc(h), cls="num" if i in num_cols else None) for i, h in enumerate(headers))
    body = []
    for r_i, row in enumerate(rows):
        tds = []
        for i, v in enumerate(row):
            content = v.html if isinstance(v, Raw) else esc(S.fmt(v) if S.is_num(v) or v is None else v)
            tds.append(tag("td", content, cls="num" if i in num_cols else None))
        rc = row_cls[r_i] if row_cls and r_i < len(row_cls) else None
        body.append(tag("tr", "".join(tds), cls=rc))
    return tag("table", tag("thead", tag("tr", th)) + tag("tbody", "".join(body)), cls=cls)


class Raw:
    """Marks a cell value as already-escaped HTML."""

    def __init__(self, html: str):
        self.html = html


def fig_img(fig, alt: str, dpi: int = 110) -> str:
    """Embed a matplotlib figure as an inline PNG."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor="#ffffff")
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    return f'<img class="fig" alt="{esc(alt)}" src="data:image/png;base64,{data}">'


def json_script(obj, id_: str) -> str:
    """Embed JSON safely inside <script type=application/json>."""
    text = json.dumps(obj, separators=(",", ":"), default=str)
    text = text.replace("</", "<\\/").replace("<!--", "<\\!--")
    return f'<script type="application/json" id="{esc(id_)}">{text}</script>'


BASE_CSS = f"""
:root {{
  color-scheme: light;
  --surface: {S.SURFACE}; --page: {S.PAGE}; --ink: {S.INK}; --ink2: {S.INK_2};
  --muted: {S.MUTED}; --grid: {S.GRIDLINE}; --base: {S.BASELINE}; --panel: {S.PANEL};
  --band: {S.BAND}; --crit: {S.CRITICAL}; --warn: {S.WARNING}; --good: {S.GOOD_TEXT};
  --s1: {S.SLOTS[0]}; --s2: {S.SLOTS[1]}; --s3: {S.SLOTS[2]}; --s4: {S.SLOTS[3]};
  --s5: {S.SLOTS[4]}; --s6: {S.SLOTS[5]}; --s7: {S.SLOTS[6]}; --s8: {S.SLOTS[7]};
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --surface: #1a1a19; --page: #0d0d0d; --ink: #ffffff; --ink2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --base: #383835; --panel: #232322; --good: #0ca30c;
    --s1: {S.SLOTS_DARK[0]}; --s2: {S.SLOTS_DARK[1]}; --s3: {S.SLOTS_DARK[2]}; --s4: {S.SLOTS_DARK[3]};
    --s5: {S.SLOTS_DARK[4]}; --s6: {S.SLOTS_DARK[5]}; --s7: {S.SLOTS_DARK[6]}; --s8: {S.SLOTS_DARK[7]};
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --surface: #1a1a19; --page: #0d0d0d; --ink: #ffffff; --ink2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --base: #383835; --panel: #232322; --good: #0ca30c;
  --s1: {S.SLOTS_DARK[0]}; --s2: {S.SLOTS_DARK[1]}; --s3: {S.SLOTS_DARK[2]}; --s4: {S.SLOTS_DARK[3]};
  --s5: {S.SLOTS_DARK[4]}; --s6: {S.SLOTS_DARK[5]}; --s7: {S.SLOTS_DARK[6]}; --s8: {S.SLOTS_DARK[7]};
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--page); color: var(--ink); font: 14px/1.45 {S.CSS_FONT}; }}
header.band {{ background: var(--band); color: #fff; padding: 16px; }}
header.band h1 {{ margin: 0 0 4px; font-size: 20px; overflow-wrap: anywhere; }}
header.band .sub {{ color: #c9d3dd; font-size: 12px; }}
main {{ max-width: 1200px; margin: 0 auto; padding: 16px; }}
section {{ background: var(--surface); border: 1px solid var(--grid); border-radius: 6px;
          padding: 16px; margin: 0 0 16px; overflow-x: auto; }}
h2 {{ font-size: 16px; margin: 0 0 8px; }}
h3 {{ font-size: 13px; margin: 12px 0 6px; color: var(--ink2); }}
p.note {{ color: var(--ink2); font-size: 12px; margin: 4px 0 8px; }}
table.t {{ border-collapse: collapse; font-size: 12px; font-variant-numeric: tabular-nums; width: 100%; }}
table.t th {{ text-align: left; color: var(--ink2); font-weight: 600; border-bottom: 1px solid var(--base);
             padding: 4px 8px; white-space: nowrap; }}
table.t td {{ padding: 3px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }}
table.t .num {{ text-align: right; }}
table.t tr.summary td {{ font-weight: 600; background: var(--panel); }}
table.t tr.excluded td {{ color: var(--muted); }}
table.kv td:first-child {{ color: var(--ink2); width: 1%; }}
.banner {{ border-left: 4px solid var(--crit); background: rgba(208,59,59,0.10); color: var(--ink);
           padding: 8px 12px; margin: 8px 0; font-weight: 600; }}
.banner.warn {{ border-left-color: var(--warn); background: rgba(250,178,25,0.12); }}
.ok {{ color: var(--good); font-weight: 600; }}
.bad {{ color: var(--crit); font-weight: 600; }}
img.fig {{ width: 100%; height: auto; display: block; background: #fff; border-radius: 4px; }}
a {{ color: var(--s1); }}
"""


def page(title: str, subtitle: str, body: str, *, extra_css: str = "", script: str = "") -> str:
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{esc(title)}</title><style>{BASE_CSS}{extra_css}</style></head><body>"
        f"<header class=\"band\"><h1>{esc(title)}</h1><div class=\"sub\">{esc(subtitle)}</div></header>"
        f"<main>{body}</main>{script}</body></html>\n"
    )
