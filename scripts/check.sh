#!/bin/sh
set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
python3 -B -m unittest discover -s tests
python3 -B -m bencher_linux build-probe "$@"
python3 -B -m bencher_linux doctor
if command -v node >/dev/null; then
    python3 - <<'PY' | node --check
from pathlib import Path
print(Path('bencher_linux/benchmark_report.html').read_text().split('<script>', 1)[1].split('</script>', 1)[0])
PY
fi
