#!/bin/bash
# Set up the HYPERVISOR side of the ENCS 5412 switch.
#
# Run this ON PROXMOX, not in the bootstrap VM. The VM only boots the ASIC over
# PCIe; the switch's management VLAN lives on the X710 backplane NIC, which
# belongs to the host.
#
#   cp -r /opt/encs-host  root@proxmox:/root/     (from the VM)
#   bash /root/encs-host/install.sh
set -euo pipefail

SW_IP="${SW_IP:-169.254.1.0}"
HOST_IP="${HOST_IP:-169.254.1.1}"
VLAN="${VLAN:-2363}"
VLAN_IF="${VLAN_IF:-sw2363}"
MTU="${MTU:-9216}"

say(){ printf '\n\e[36m==> %s\e[0m\n' "$*"; }

# --- find the backplane NIC ------------------------------------------------
# Do NOT hardcode a name. It is enp8s0f1np1 on one box and something else on
# the next, and Debian has changed its predictable-naming output between
# releases before now.
#
# BOTH X710 ports go to the Marvell switch (verified by MAC learning: function
# .0 is te1, function .1 is te2). Only te2 carries the management VLAN, so we
# want function .1 specifically.
#
# Order by PCI address, NOT by interface name: 0000:08:00.0 < 0000:08:00.1 is
# a property of the hardware and survives any renaming scheme, whereas sorting
# names would silently pick the wrong port if a future release reordered them
# - and configuring the VLAN on te1 leaves the switch unreachable.
say "Locating the X710 backplane interface"
mapfile -t I40E < <(
  for i in /sys/class/net/*; do
    n=$(basename "$i")
    d=$(basename "$(readlink -f "$i/device/driver" 2>/dev/null)" 2>/dev/null || true)
    [ "$d" = "i40e" ] || continue
    pci=$(basename "$(readlink -f "/sys/class/net/$n/device" 2>/dev/null)" 2>/dev/null || true)
    [ -n "$pci" ] && echo "$pci $n"
  done | sort | awk '{print $2}'
)
if [ "${#I40E[@]}" -lt 2 ]; then
    echo "ERROR: expected 2 i40e ports, found ${#I40E[@]}: ${I40E[*]:-none}" >&2
    echo "       Is this the ENCS host? Are the X710 drivers loaded?" >&2
    exit 1
fi
BACKPLANE="${BACKPLANE:-${I40E[1]}}"
echo "  i40e ports : ${I40E[*]}  (ordered by PCI address)"
echo "  backplane  : $BACKPLANE  (mac $(cat /sys/class/net/$BACKPLANE/address))"

# --- bring up the management VLAN -----------------------------------------
# NB: the VLAN interface CANNOT be named "$BACKPLANE.$VLAN" - that is 16 chars
# for enp8s0f1np1 and exceeds the 15-char IFNAMSIZ limit, which fails with
# 'name not a valid ifname'. Hence the short explicit name.
say "Configuring $VLAN_IF (VLAN $VLAN) @ $HOST_IP"
ip link set "$BACKPLANE" up mtu "$MTU"
ip link show "$VLAN_IF" >/dev/null 2>&1 || \
    ip link add link "$BACKPLANE" name "$VLAN_IF" type vlan id "$VLAN"
# grep -c not -q: under pipefail a -q early exit SIGPIPEs the producer and
# the pipeline reports failure even on a match.
HAVE_IP=$(ip addr show dev "$VLAN_IF" 2>/dev/null | grep -c "$HOST_IP" || true)
[ "${HAVE_IP:-0}" -gt 0 ] || ip addr add "$HOST_IP/16" dev "$VLAN_IF"
ip link set "$VLAN_IF" up mtu "$MTU"

# --- persist ---------------------------------------------------------------
IFACES=/etc/network/interfaces
if ! grep -q "^auto $VLAN_IF" "$IFACES" 2>/dev/null; then
    say "Persisting to $IFACES"
    cat >> "$IFACES" <<EOF

auto $BACKPLANE
iface $BACKPLANE inet manual
    mtu $MTU

auto $VLAN_IF
iface $VLAN_IF inet static
    address $HOST_IP/16
    vlan-raw-device $BACKPLANE
    vlan-id $VLAN
    mtu $MTU
EOF
else
    echo "  already present in $IFACES"
fi

# --- tools -----------------------------------------------------------------
say "Installing switch tools"
HERE="$(cd "$(dirname "$0")" && pwd)"
for t in encs-switch-api encs-switch-tui; do
    if [ -f "$HERE/$t" ]; then
        install -m 0755 "$HERE/$t" /usr/local/sbin/
        echo "  /usr/local/sbin/$t"
    fi
done
if [ -f "$HERE/encs-switch-replay.service" ]; then
    install -m 0644 "$HERE/encs-switch-replay.service" /etc/systemd/system/
    mkdir -p /etc/encs-switch
    systemctl daemon-reload
    systemctl enable encs-switch-replay.service >/dev/null 2>&1 || true
    echo "  encs-switch-replay.service enabled"
fi

# --- verify ----------------------------------------------------------------
say "Checking the switch"
if ping -c2 -W2 "$SW_IP" >/dev/null 2>&1; then
    echo "  switch is UP at $SW_IP"
    echo
    echo "  next:  encs-switch-tui"
else
    echo "  switch not answering at $SW_IP yet."
    echo "  - is the bootstrap VM running?"
    echo "  - in the VM: encs-switch-status ; journalctl -u marvell-switch-boot"
    echo "  - a cold bootstrap takes ~60s to reach 'ROS ready!'"
    # Both X710 ports reach the ASIC but only one carries VLAN 2363, so a
    # wrong pick looks exactly like a switch that has not booted.
    OTHER="${I40E[0]}"
    if [ "$BACKPLANE" != "$OTHER" ]; then
        echo "  - if it never answers, this may be the wrong X710 port."
        echo "    Both reach the switch; only one carries the management VLAN."
        echo "    Retry with:  BACKPLANE=$OTHER bash $0"
    fi
fi
