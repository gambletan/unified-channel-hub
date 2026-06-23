#!/usr/bin/env bash
# Copy the optional ERP client from the sibling X-Auto repo into the build context.
# ERP is optional: if the source is missing, the image still builds and ERP stays disabled.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"   # support/scripts -> repo root
SRC="${ERP_SRC:-$REPO_ROOT/../X-Auto/AC-Customer-Support/erp_client.py}"
DEST="$REPO_ROOT/support/vendor/erp_client.py"
if [ -f "$SRC" ]; then
  cp "$SRC" "$DEST"
  echo "bundled erp_client.py from $SRC"
else
  echo "WARN: $SRC not found — building without ERP (it will be disabled at runtime)"
fi
