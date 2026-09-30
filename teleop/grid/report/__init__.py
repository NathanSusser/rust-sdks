"""Reports. Host A only (numpy, matplotlib).

    teleop.grid.report.cell.render(cell_dir) -> Path    # report.pdf + report.html in the cell (repeat) dir
    teleop.grid.report.combo.render(combo_dir) -> Path  # summary.pdf + summary.html over a combination's repeats
    teleop.grid.report.grid.render(grid_dir) -> Path    # comparison/{metrics.csv, comparison.html, comparison.pdf,
                                                        #   analysis.html}; refreshes stale combo summaries first
"""
