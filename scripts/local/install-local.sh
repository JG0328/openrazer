#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Install, for the current user only, the repo's OpenRazer daemon in place of the packaged one, and
# the udev rule that gives the user access to the Barracuda X Chroma 2.4 GHz dongle (hidraw).
#
# Nothing packaged is modified: the repo daemon is selected by a systemd USER drop-in for
# openrazer-daemon.service (the D-Bus activation file delegates to that unit, so one drop-in
# redirects both ways of starting the daemon, and it survives package upgrades).
#
# Steps, in order, each one announced:
#   1. scripts/local/smoke-hidraw.sh must print "SMOKE OK" (private bus, never touches the real one)
#   2. read-only check of the devices that already work (repo daemon vs. the installed package),
#      then ask for confirmation
#   3. udev rule (sudo), replacing the temporary 70-barracuda-probe.rules
#   4. systemd user drop-in
#   5. daemon reload + restart, then status and the end of razer.log
#
# Every step is idempotent: running the script again after a partial run is safe.
# Undo it with scripts/local/uninstall-local.sh.
#
# Usage: scripts/local/install-local.sh      (no arguments; asks for sudo and for confirmation)

set -euo pipefail

REPO=$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)
cd "$REPO"

RULE_NAME=70-openrazer-hidraw-local.rules
PROBE_RULE=/etc/udev/rules.d/70-barracuda-probe.rules
PKG=/usr/lib/python3/dist-packages/openrazer_daemon
DROPIN_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/openrazer-daemon.service.d
DROPIN=$DROPIN_DIR/10-repo-daemon.conf
PYCACHE=$HOME/.cache/openrazer-pycache
LOG=${XDG_DATA_HOME:-$HOME/.local/share}/openrazer/logs/razer.log

die() {
    echo "ERROR: $*" >&2
    exit 1
}

step() {
    echo
    echo "=== $* ==="
}

[ "$(id -u)" -ne 0 ] || die "no ejecutar como root: el script pide sudo cuando lo necesita y usa el systemd del usuario"

# The paths end up in a systemd unit (ExecStart=, Environment=): refuse what systemd would re-interpret
for path in "$REPO" "$PYCACHE"; do
    case $path in
        *[[:space:]%\$\"\'\\]*) die "la ruta $path tiene espacios o caracteres especiales de systemd; usa otra ruta" ;;
    esac
done

# --- 1. smoke test ------------------------------------------------------------------------------

step "1/5 Prueba de humo en un bus D-Bus privado (scripts/local/smoke-hidraw.sh)"
smoke_log=$(mktemp "${TMPDIR:-/tmp}/openrazer-install-smoke.XXXXXX")
trap 'rm -f "$smoke_log"' EXIT
if ! bash scripts/local/smoke-hidraw.sh 2>&1 | tee "$smoke_log"; then
    die "la prueba de humo falló; no se instala nada"
fi
if [ "$(tail -n 1 "$smoke_log")" != "SMOKE OK" ]; then
    die "la prueba de humo no terminó con 'SMOKE OK'; no se instala nada"
fi

# --- 2. read-only check of the devices that already work -----------------------------------------

step "2/5 Comprobación previa de los dispositivos que ya funcionan (solo lectura: paquete instalado frente al repo)"
dpkg-query -W -f='Paquete instalado: ${Package} ${Version}\n' openrazer-daemon 2> /dev/null || true

if [ -d "$PKG" ]; then
    for pair in \
        mouse.py:RazerBasiliskV3Pro35KWired \
        mouse.py:RazerBasiliskV3Pro35KWireless \
        keyboards.py:RazerBlackWidowV4Pro \
        accessory.py:RazerNommoChroma; do
        F=${pair%%:*}
        C=${pair##*:}
        echo
        echo "--- diff -u de la clase $C ($F): paquete instalado (-) frente al repo (+) ---"
        diff -u \
            --label "$PKG/hardware/$F ($C)" \
            --label "daemon/openrazer_daemon/hardware/$F ($C)" \
            <(sed -n "/^class $C(/,/^class /p" "$PKG/hardware/$F") \
            <(sed -n "/^class $C(/,/^class /p" "daemon/openrazer_daemon/hardware/$F") \
            && echo "(sin diferencias)" || true
    done
    echo
    echo "--- diff -rq --exclude=__pycache__ $PKG/dbus_services daemon/openrazer_daemon/dbus_services ---"
    diff -rq --exclude=__pycache__ "$PKG/dbus_services" daemon/openrazer_daemon/dbus_services || true
else
    echo "AVISO: no existe $PKG: NO se pueden mostrar las diferencias de los dispositivos" >&2
    echo "       que ya funcionan. Revisa a mano antes de continuar." >&2
fi

echo
answer=''
read -r -p "¿Las diferencias son aceptables? Se instalará la regla udev y el daemon del repo [s/N] " answer || answer=''
case $answer in
    s | S | y | Y) ;;
    *)
        echo "Cancelado: no se ha cambiado nada." >&2
        exit 1
        ;;
esac

# --- 3. udev rule -------------------------------------------------------------------------------

step "3/5 Regla udev (sudo)"
echo "+ sudo install -m 644 install_files/udev/$RULE_NAME /etc/udev/rules.d/"
sudo install -m 644 "install_files/udev/$RULE_NAME" /etc/udev/rules.d/
echo "+ sudo rm -f $PROBE_RULE"
sudo rm -f "$PROBE_RULE"
echo "+ sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw"
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=hidraw

# --- 4. systemd user drop-in --------------------------------------------------------------------

step "4/5 Drop-in de systemd (usuario): $DROPIN"
mkdir -p "$DROPIN_DIR"
cat > "$DROPIN" <<DROPIN_EOF
[Service]
ExecStart=
ExecStart=/usr/bin/python3 $REPO/daemon/run_openrazer_daemon.py -F
Environment=PYTHONPATH=$REPO/daemon
Environment=PYTHONPYCACHEPREFIX=$PYCACHE
DROPIN_EOF
echo "Escrito:"
cat "$DROPIN"

# --- 5. restart the daemon ----------------------------------------------------------------------

step "5/5 Recarga y reinicio del daemon"
echo "+ systemctl --user daemon-reload && systemctl --user restart openrazer-daemon"
systemctl --user daemon-reload
restart_rc=0
systemctl --user restart openrazer-daemon || restart_rc=$?

echo "Esperando 5 s..."
sleep 5

echo
echo "+ systemctl --user status openrazer-daemon --no-pager"
systemctl --user status openrazer-daemon --no-pager || true

echo
echo "+ tail -n 30 $LOG"
if [ -f "$LOG" ]; then
    tail -n 30 "$LOG"
else
    echo "(no existe $LOG)" >&2
fi

echo
if [ "$restart_rc" -ne 0 ]; then
    echo "ERROR: 'systemctl --user restart openrazer-daemon' falló (código $restart_rc)." >&2
    echo "Para deshacerlo: bash scripts/local/uninstall-local.sh" >&2
    exit 1
fi
echo "Instalado. Para deshacerlo: bash scripts/local/uninstall-local.sh"
