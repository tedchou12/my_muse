#!/bin/sh
set -eu

# The image runs as pwuser. Tighten the data volume at runtime; /workspace is
# the intended writable area for optional shell commands.
chmod 700 /data 2>/dev/null || true

# The shell workspace is separate from /data and writable by the Muse user.
mkdir -p /workspace 2>/dev/null || true
chmod 700 /workspace 2>/dev/null || true

exec "$@"
