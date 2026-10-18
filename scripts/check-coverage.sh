#!/usr/bin/env bash
# Fails if line coverage of contracts/src is below THRESHOLD (default 95).
set -euo pipefail
THRESHOLD="${THRESHOLD:-95}"
cd "$(dirname "$0")/../contracts"
FORGE="${FORGE:-forge}"
$FORGE coverage --no-match-coverage "(test|script)" --report lcov --report-file lcov.info >/dev/null
awk -v t="$THRESHOLD" '
  /^SF:/ { insrc = (index($0, "SF:src/") == 1 || index($0, "SF:src\\") == 1) }
  insrc && /^LF:/ { lf += substr($0, 4) }
  insrc && /^LH:/ { lh += substr($0, 4) }
  END {
    if (lf == 0) { print "no coverage data for src/"; exit 1 }
    pct = 100 * lh / lf
    printf "line coverage of src/: %.2f%% (%d/%d), threshold %s%%\n", pct, lh, lf, t
    exit (pct + 0 >= t + 0) ? 0 : 1
  }' lcov.info
