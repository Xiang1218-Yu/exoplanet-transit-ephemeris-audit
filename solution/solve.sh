#!/bin/sh
set -eu
cp "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/transit_audit.py" /app/transit_audit.py
chmod 755 /app/transit_audit.py
