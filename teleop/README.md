# teleop — run a test grid

1. Once per host: copy `config/host.example.yaml` to `~/.config/teleop/host.yaml`; `~/.config/teleop/sfu.env` sets `TELEOP_SFU_HOST`; both hosts are on the same commit, and `cargo build --release` has been run on each.
2. Write a grid: `python3 -m teleop.grid.cli grid new teleop/config/grids/mine.yaml --id g1001a --axis codec=h264,av1 --axis kbps=512,2500 --set clip=/path/clip.mp4` (or copy `config/grids/smoke.yaml`).
3. Check it without running anything: `python3 -m teleop.grid.cli grid check teleop/config/grids/mine.yaml` (cell table + pre-flight on A and B).
4. Run it from Host A (the operator starts every grid): `python3 -m teleop.grid.cli grid run teleop/config/grids/mine.yaml` (add `--resume` to continue a paused or stopped grid).
5. Watch and stop: `grid status g1001a`, `grid stop g1001a`. Results are written to `~/teleop-runs/g1001a/` (`cells/<label>/manifest.json`, `metrics.json`, `comparison/`).
