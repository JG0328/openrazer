#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# End-to-end smoke test of the hidraw devices (Razer Barracuda X Chroma, Razer Seiren V3 Mini).
#
# Starts, in a throw-away directory and on a PRIVATE D-Bus session bus, the repo's daemon in
# --test-dir mode against fake sysfs entries and a fake 2.4 GHz dongle, then checks it with the
# installed client library (the one Polychromatic uses) and with polychromatic-cli.
#
# It never touches the session bus of the user, so a daemon already running there (and the real
# devices it drives) is not affected: every command below runs with DBUS_SESSION_BUS_ADDRESS set
# to the private bus, the daemon gets its own --run-dir/--log-dir/--persistence/--config, and the
# clean-up kills only the processes this script started (by PID, never by name).
#
# Usage: scripts/local/smoke-hidraw.sh      (no arguments; exit 0 and "SMOKE OK" only if all passes)

set -Eeuo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$REPO"

export PYTHONDONTWRITEBYTECODE=1

WIRELESS='Razer Barracuda X Chroma (Wireless)'
WIRED='Razer Barracuda X Chroma (Wired)'
SEIREN='Razer Seiren V3 Mini'
EXPECTED_NAMES=("$WIRED" "$WIRELESS" "$SEIREN")

W=$(mktemp -d "${TMPDIR:-/tmp}/openrazer-smoke.XXXXXX")
NAME="openrazer-smoke-$$-$RANDOM"
BUS_PID=''
DONGLE_PID=''
DAEMON_PID=''

# --- helpers ------------------------------------------------------------------------------------

# Print PID and the PIDs of everything below it
tree_pids() {
    local pid=$1 child
    echo "$pid"
    for child in $(ps -o pid= --ppid "$pid" 2>/dev/null || true); do
        tree_pids "$child"
    done
}

# Stop a process this script started, and its children: TERM, then KILL after 5 s
stop_tree() {
    local pid=$1 pids p i alive
    [ -n "$pid" ] || return 0
    kill -0 "$pid" 2>/dev/null || { wait "$pid" 2>/dev/null || true; return 0; }

    pids=$(tree_pids "$pid")
    # shellcheck disable=SC2086
    kill -TERM $pids 2>/dev/null || true
    for i in $(seq 1 25); do
        alive=''
        for p in $pids; do
            if kill -0 "$p" 2>/dev/null; then alive=1; fi
        done
        [ -n "$alive" ] || break
        sleep 0.2
    done
    for p in $pids; do
        kill -0 "$p" 2>/dev/null && kill -KILL "$p" 2>/dev/null
    done
    wait "$pid" 2>/dev/null || true
    return 0
}

cleanup() {
    local rc=$?
    trap - EXIT ERR
    set +e
    stop_tree "$DAEMON_PID"
    stop_tree "$DONGLE_PID"
    stop_tree "$BUS_PID"
    rm -rf "$W"
    exit "$rc"
}
trap cleanup EXIT

fail() {
    trap - ERR
    echo "SMOKE FAIL: $*" >&2
    local file
    for file in daemon.out dongle.log bus.log cli-static.out; do
        if [ -s "$W/$file" ]; then
            echo "--- last lines of $file" >&2
            tail -n 25 "$W/$file" >&2
        fi
    done
    exit 1
}
trap 'fail "command failed (line $LINENO): $BASH_COMMAND"' ERR

# Run a client with the installed openrazer library (no PYTHONPATH) and a state dir inside W
isolated() {
    XDG_CONFIG_HOME="$W/xdg/config" XDG_CACHE_HOME="$W/xdg/cache" XDG_RUNTIME_DIR="$W/xdg/runtime" \
        env -u PYTHONPATH "$@"
}

mkdir -p "$W/run" "$W/logs" "$W/td" "$W/xdg/config" "$W/xdg/cache" "$W/xdg/runtime"
chmod 700 "$W/xdg/runtime"
: > "$W/persistence.conf"

# --- 1. private bus -----------------------------------------------------------------------------

ADDR="unix:abstract=$NAME"
cat > "$W/bus.conf" <<'XML'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*"/>
    <allow receive_sender="*"/>
    <allow own="*"/>
  </policy>
</busconfig>
XML

systemd-socket-activate -l "@$NAME" -E DBUS_SESSION_BUS_ADDRESS="$ADDR" -E XDG_RUNTIME_DIR \
    dbus-broker-launch --scope user --config-file "$W/bus.conf" > "$W/bus.log" 2>&1 &
BUS_PID=$!
export DBUS_SESSION_BUS_ADDRESS="$ADDR"

# The private bus must answer, and must not already know org.razer (that would be the real
# daemon: then this is not the private bus and nothing may be started)
NAME_HAS_OWNER=(gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus
                --method org.freedesktop.DBus.NameHasOwner org.razer)
owner=''
for _ in $(seq 1 50); do
    if owner=$("${NAME_HAS_OWNER[@]}" 2>/dev/null); then break; fi
    kill -0 "$BUS_PID" 2>/dev/null || fail "the private bus process exited"
    sleep 0.2
done
[ "$owner" = '(false,)' ] || fail "private bus is not up or already has an owner for org.razer (got '$owner'), refusing to start the daemon"

# --- 2. fake sysfs ------------------------------------------------------------------------------

PYTHONPATH="$REPO/daemon/tests:$REPO/daemon:$REPO/pylib" python3 -c '
import os
import sys
from hidraw_fixtures import make_sysfs_hid

root, dongle = sys.argv[1], sys.argv[2]
wireless = make_sysfs_hid(root, "0003:1532:0574.0001")
with open(os.path.join(wireless, "fake_hidraw_socket"), "w") as name_file:
    name_file.write(dongle)
make_sysfs_hid(root, "0003:1532:0573.0002")
make_sysfs_hid(root, "0003:1532:056A.0003")
' "$W/td" "$NAME-dongle"

# --- 3. fake dongle -----------------------------------------------------------------------------

python3 daemon/tests/fake_hidraw.py --abstract "$NAME-dongle" --log "$W/dongle.log" > "$W/dongle.out" 2>&1 &
DONGLE_PID=$!
ready=''
for _ in $(seq 1 50); do
    if grep -qF "@$NAME-dongle" /proc/net/unix; then ready=1; break; fi
    kill -0 "$DONGLE_PID" 2>/dev/null || fail "the fake dongle exited: $(cat "$W/dongle.out")"
    sleep 0.2
done
[ -n "$ready" ] || fail "the fake dongle is not listening on @$NAME-dongle"

# --- 4. daemon ----------------------------------------------------------------------------------

PYTHONPATH="$REPO/pylib:$REPO/daemon" python3 "$REPO/daemon/run_openrazer_daemon.py" -F -v \
    --run-dir "$W/run" --log-dir "$W/logs" --config "$REPO/daemon/resources/razer.conf" \
    --persistence "$W/persistence.conf" --test-dir "$W/td" > "$W/daemon.out" 2>&1 &
DAEMON_PID=$!

owner=''
for _ in $(seq 1 50); do
    if owner=$("${NAME_HAS_OWNER[@]}" 2>/dev/null) && [ "$owner" = '(true,)' ]; then break; fi
    kill -0 "$DAEMON_PID" 2>/dev/null || fail "the daemon exited before owning org.razer"
    sleep 0.2
done
[ "$owner" = '(true,)' ] || fail "the daemon did not own org.razer on the private bus within 10 s (got '$owner')"

# --- 5. installed client library (what Polychromatic uses) --------------------------------------

isolated python3 - "$REPO" <<'PY' || fail "client check failed (see above)"
import sys
import time

import openrazer.client as client

repo = sys.argv[1]
wired = 'Razer Barracuda X Chroma (Wired)'
wireless = 'Razer Barracuda X Chroma (Wireless)'
seiren = 'Razer Seiren V3 Mini'
expected = sorted([wired, wireless, seiren])

# The devices are discovered, and the first answer of the dongle arrives, a moment after the
# daemon owns its bus name
deadline = time.monotonic() + 10
while True:
    devices = client.DeviceManager().devices
    names = sorted(device.name for device in devices)
    by_name = {device.name: device for device in devices}
    battery = None
    if wireless in by_name and by_name[wireless].has('battery'):
        battery = by_name[wireless].battery_level
    if (names == expected and battery == 79) or time.monotonic() > deadline:
        break
    time.sleep(0.3)

print('client library:', client.__file__)
print('devices:', names)

problems = []
if client.__file__.startswith(repo):
    problems.append('the client library comes from the repo, not from the installed package')
if names != expected:
    problems.append('device names are %r, expected %r' % (names, expected))
if wireless in by_name:
    device = by_name[wireless]
    for capability in ('battery', 'lighting_static', 'brightness'):
        if not device.has(capability):
            problems.append('%s lacks the %r capability' % (wireless, capability))
    if battery != 79:
        problems.append('%s battery_level is %r, expected 79' % (wireless, battery))
else:
    problems.append('%s is not listed' % wireless)

for problem in problems:
    print('SMOKE FAIL (client):', problem, file=sys.stderr)
sys.exit(1 if problems else 0)
PY

# --- 6. Polychromatic CLI -----------------------------------------------------------------------

listing=$(isolated polychromatic-cli -l --no-pretty-column 2>&1) || fail "polychromatic-cli -l failed: $listing"
for device_name in "${EXPECTED_NAMES[@]}"; do
    grep -qF -- "$device_name" <<< "$listing" || fail "polychromatic-cli -l does not list '$device_name'. Output: $listing"
done

isolated polychromatic-cli -n "$WIRELESS" -o static -c "#0000ff" > "$W/cli-static.out" 2>&1 \
    || fail "polychromatic-cli -o static -c #0000ff failed"

blue=$(PYTHONPATH="$REPO/daemon" python3 -c '
from openrazer_daemon.misc import barracuda_protocol
print(barracuda_protocol.build_color(0, 0, 255).hex())')
found=''
for _ in $(seq 1 25); do
    if grep -qxF "$blue" "$W/dongle.log"; then found=1; break; fi
    sleep 0.2
done
[ -n "$found" ] || fail "the dongle never received the blue colour frame (build_color(0, 0, 255))"

# Everything the daemon wrote to the dongle must pass the safety gate
PYTHONPATH="$REPO/daemon" python3 - "$W/dongle.log" <<'PY' || fail "the dongle received a frame that check_frame refuses"
import sys

from openrazer_daemon.misc import barracuda_protocol

with open(sys.argv[1]) as log:
    frames = [bytes.fromhex(line) for line in log.read().split()]
refused = [frame.hex() for frame in frames if barracuda_protocol.check_frame(frame) is not None]
print('frames received by the dongle: %d, refused by check_frame: %d' % (len(frames), len(refused)))
for frame in refused:
    print('SMOKE FAIL (dongle): refused frame', frame, file=sys.stderr)
sys.exit(1 if refused or not frames else 0)
PY

echo "SMOKE OK"
