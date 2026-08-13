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
warn(){ printf '\e[33m  %s\e[0m\n' "$*" >&2; }

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
# encs-switch-vnet moves this VLAN onto a bridge so VMs can share the
# backplane, and a VLAN subinterface of an enslaved NIC receives nothing. So
# once its block is in the file, the management VLAN already has an owner:
# re-running this installer must not append a second, wrongly-parented
# definition - that would silently take the switch away on the next boot.
if grep -q "^# >>> encs-switch-vnet begin" /etc/network/interfaces 2>/dev/null; then
    say "Management VLAN is managed by encs-switch-vnet"
    echo "  its block in /etc/network/interfaces owns $VLAN_IF -"
    echo "  leaving the network config alone"
    echo "  (encs-switch-vnet status  shows it; ifreload -a applies changes)"
    SKIP_NET=1
fi

# NB: the VLAN interface CANNOT be named "$BACKPLANE.$VLAN" - that is 16 chars
# for enp8s0f1np1 and exceeds the 15-char IFNAMSIZ limit, which fails with
# 'name not a valid ifname'. Hence the short explicit name.
if [ -z "${SKIP_NET:-}" ]; then
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
    # The '#' lines are not decoration: Proxmox reads them back as each
    # interface's Comment and shows them in the network panel, the Edit
    # dialog and the bridge picker. Six months from now "sw2363" on its own
    # means nothing, and the wrong guess about it is "unused, delete it".
    # encs-switch-vnet writes the identical lines when it takes these over.
    cat >> "$IFACES" <<EOF

auto $BACKPLANE
iface $BACKPLANE inet manual
#X710 backplane link to the Marvell switch ASIC (te2). Not a front port - the GE1/x jacks are behind the switch, not on this NIC.
    mtu $MTU

auto $VLAN_IF
iface $VLAN_IF inet static
#Marvell switch management VLAN $VLAN, link-local. encs-switch-tui reaches the ASIC at 169.254.1.0 over this. Not for VMs; do not delete.
    address $HOST_IP/16
    vlan-raw-device $BACKPLANE
    vlan-id $VLAN
    mtu $MTU
EOF
else
    echo "  already present in $IFACES"
fi
fi   # SKIP_NET

# --- tools -----------------------------------------------------------------
say "Installing switch tools"
HERE="$(cd "$(dirname "$0")" && pwd)"
for t in encs-switch-api encs-switch-tui encs-switch-vnet; do
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

# --- optional: a bridge for VM traffic -------------------------------------
# Offered rather than done. Creating swbr0 re-parents the management VLAN,
# which is a change worth understanding before it happens - and skipping it
# costs nothing, because 'encs-switch-vnet init' does exactly the same thing
# whenever the answer becomes yes.
#
# Skipped without a prompt when stdin is not a terminal (piped installs), or
# with ENCS_NO_VNET=1. Never assume yes: an unattended installer that quietly
# rearranged the network would be the worst possible default here.
if [ -x /usr/local/sbin/encs-switch-vnet ] && [ -z "${ENCS_NO_VNET:-}" ] \
   && [ -z "${SKIP_NET:-}" ] && [ -t 0 ]; then
    say "Optional: create swbr0 - the NFVIS virtual network for VMs"
    cat <<EOF
  swbr0 MIMICS THE VIRTUAL NETWORK NFVIS GIVES A VM. In NFVIS you pick a
  network - lan-net - when creating a VM, and its traffic comes out the
  GE1/x jacks. That network is a bridge over the 10G backplane behind the
  switch (NFVIS calls it lan-br, its only port is int-LAN = te2), plus a
  VLAN on the ASIC deciding which jacks it reaches. swbr0 is the same
  bridge, built the Proxmox way: VLAN-aware, $BACKPLANE as its only port.

  Without it, a new Proxmox VM lands on vmbr0 - the MGMT CPU jack - so
  guest traffic shares the one interface you manage the hypervisor over.
  NFVIS never did that.

    vmbr0   Proxmox management, and the bootstrap VM. Leave it as it is.
    swbr0   VM traffic, i.e. what lan-net was:
              bridge=swbr0            -> switch VLAN 1 -> all 8 GE1/x jacks
                                         (this is stock lan-net exactly)
              bridge=swbr0,tag=100    -> whichever jacks are in VLAN 100
                                         (encs-switch-vnet add 100 --ports gi0)

  Creating it MOVES $VLAN_IF onto the bridge. That is required, not optional:
  once $BACKPLANE is a bridge port, a VLAN subinterface of the NIC itself
  receives nothing. Your Proxmox management interface keeps every setting
  it has - the only change there is one comment line on its stanza saying
  guests belong on swbr0. Removed again if you ever tear swbr0 down.

  Nothing is applied until you run 'ifreload -a' - this only writes the
  config and shows you the diff first.

  Say no and nothing changes; run 'encs-switch-vnet init' any time later.
EOF
    printf '\n  Create swbr0 now? [y/N] '
    read -r ANS || ANS=""
    case "$ANS" in
        [Yy]*)
            # NB: no `[ -n "$x" ] && echo` as the last statement of a block -
            # under `set -e` a false test is a failing command and would end
            # the install right here, after the tools are in place but before
            # the switch check.
            if ! /usr/local/sbin/encs-switch-vnet init --yes; then
                warn "encs-switch-vnet init failed - the network config was"
                warn "not changed. VMs stay on vmbr0; nothing else is affected."
            fi
            ;;
        *)  echo "  skipped - VMs stay on vmbr0 until you run"
            echo "            encs-switch-vnet init"
            ;;
    esac
fi

# --- verify ----------------------------------------------------------------
say "Checking the switch"
if ping -c2 -W2 "$SW_IP" >/dev/null 2>&1; then
    echo "  switch is UP at $SW_IP"
    echo
    echo "  next:  encs-switch-tui        configure the switch"
    echo "         encs-switch-vnet init  put VMs on the front LAN ports"
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
