#!/bin/bash
set -euo pipefail

# TCMalloc reduces memory fragmentation under large model workloads
export LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libtcmalloc_minimal.so.4

# Read COMMANDLINE_ARGS then unset it
RAW_ARGS="${COMMANDLINE_ARGS:-}"
unset COMMANDLINE_ARGS

EXTRA_ARGS=()
if [[ -n "$RAW_ARGS" ]]; then
    read -ra EXTRA_ARGS <<< "$RAW_ARGS"
fi

# Symlink settings files into the config bind-mount so they persist across container recreations
for f in config.json ui-config.json styles.csv user.css; do
    ln -sf /home/forge/sd-webui/config/$f /home/forge/sd-webui/$f
done

# Headless API backend for the ControlPlugin Photoshop plugin:
#   --listen  bind 0.0.0.0 (container networking)
#   --api     enable /sdapi/v1/* and the fork's /cplugapi/v1/* surface
#
# Pass --api-auth "user:pass" via COMMANDLINE_ARGS (or trailing args) so
# /cplugapi/v1/* inherits the same Basic auth as /sdapi/v1/* — keep the
# credential out of the image. Basic auth is only meaningfully protected
# behind a TLS reverse proxy; see docker/README.md.
exec python /home/forge/sd-webui/launch.py \
    --listen \
    --api \
    "${EXTRA_ARGS[@]}" \
    "$@"
