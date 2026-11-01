"""Turn the PrometheusRule manifest into a plain rules file that promtool can test."""
import sys
from pathlib import Path

import yaml

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
groups = yaml.safe_load(src.read_text())["spec"]["groups"]
dst.write_text(yaml.safe_dump({"groups": groups}, sort_keys=False))
print(f"{dst}: {sum(len(g['rules']) for g in groups)} rules")
