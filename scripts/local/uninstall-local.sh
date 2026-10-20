#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Undo scripts/local/install-local.sh: remove the systemd user drop-in (the packaged daemon runs
# again), restart the daemon, remove the local udev rule and re-trigger the hidraw devices.
#
# Idempotent and tolerant: whatever is already gone is skipped, and a failing step does not stop
# the next ones (the exit status is non-zero if any step failed).
#
# Usage: scripts/local/uninstall-local.sh      (no arguments; asks for sudo)

set -euo pipefail

RULE=/etc/udev/rules.d/70-openrazer-hidraw-local.rules
DROPIN_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/openrazer-daemon.service.d
DROPIN=$DROPIN_DIR/10-repo-daemon.conf

failed=0

step() {
    echo
    echo "=== $* ==="
}

[ "$(id -u)" -ne 0 ] || {
    echo "ERROR: no ejecutar como root: el script pide sudo cuando lo necesita y usa el systemd del usuario" >&2
    exit 1
}

step "1/2 Drop-in de systemd (usuario) y reinicio del daemon empaquetado"
echo "+ rm -f $DROPIN"
rm -f "$DROPIN"
# Only removes the directory if it is now empty
rmdir "$DROPIN_DIR" 2> /dev/null || true

echo "+ systemctl --user daemon-reload && systemctl --user restart openrazer-daemon"
systemctl --user daemon-reload || {
    echo "AVISO: 'systemctl --user daemon-reload' falló" >&2
    failed=1
}
systemctl --user restart openrazer-daemon || {
    echo "AVISO: 'systemctl --user restart openrazer-daemon' falló" >&2
    failed=1
}

echo "+ systemctl --user status openrazer-daemon --no-pager"
systemctl --user status openrazer-daemon --no-pager || true

step "2/2 Regla udev (sudo)"
echo "+ sudo rm -f $RULE"
sudo rm -f "$RULE" || {
    echo "AVISO: no se pudo borrar $RULE" >&2
    failed=1
}
echo "+ sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw"
{ sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw; } || {
    echo "AVISO: falló la recarga de udev" >&2
    failed=1
}

echo
if [ "$failed" -ne 0 ]; then
    echo "Desinstalado con avisos (ver arriba)." >&2
    exit 1
fi
echo "Desinstalado: el daemon empaquetado vuelve a ser el que se ejecuta."
