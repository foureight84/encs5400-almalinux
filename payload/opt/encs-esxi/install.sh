#!/bin/sh
# Set up the ESXi side of the Cisco ENCS 5412 Marvell switch.  EXPERIMENTAL.
#
# RUN THIS ON THE ESXi HOST, over SSH.  It is the ESXi counterpart of
# opt/encs-host/install.sh, and it does strictly less: ESXi cannot run the
# switch tools (no bash, no systemd, no curses), so those move into the
# bootstrap VM.  What is left for the host is passthrough and networking.
#
#   scp -r payload/opt/encs-esxi root@<esxi>:/vmfs/volumes/datastore1/
#   ssh root@<esxi> 'sh /vmfs/volumes/datastore1/encs-esxi/install.sh'
#
# Nothing is applied until you pass --yes: the default prints the plan.
# Everything it creates is recorded in $STATE so uninstall.sh can take
# exactly that back out and nothing else.  See docs/ESXI.md.
#
# POSIX sh only - the ESXi shell is busybox ash.  No bashisms.
set -eu

VSWITCH="${VSWITCH:-vSwitchENCS}"
PG_MGMT="${PG_MGMT:-encs-mgmt-2363}"
PG_LAN="${PG_LAN:-encs-lan}"
VLAN="${VLAN:-2363}"
MTU="${MTU:-9000}"
# NOT under /etc. /etc on ESXi is a 28 MB RAM disk restored from the bootbank
# at boot, and auto-backup.sh only archives files that have a companion
# ".#<name>" marker (see the find in /sbin/auto-backup.sh). A file this script
# creates has no marker, so it was silently dropped on every reboot - which
# left uninstall.sh with no record and refusing to run without --force.
# The bundle already lives on a VMFS datastore, which is real storage, so the
# record goes next to it.
STATE="${STATE:-$(dirname "$0")/created}"
RESET_METHOD="${RESET_METHOD:-}"     # flr | d3d0 | link | bridge | default
APPLY=0

usage() {
    cat <<EOF
usage: sh $0 [options]

  --yes                 apply the plan (default: print it and stop)
  --reset-method M      also add "11ab be00 M false" to /etc/vmware/passthru.map
                        (flr | d3d0 | link | bridge | default) - only needed if
                        the VM refuses to power on with a passthrough error
  --backplane vmnicN    override the te2 uplink (default: the higher-numbered
                        PCI function of the two i40en ports)
  -h, --help            this

environment: VSWITCH PG_MGMT PG_LAN VLAN MTU STATE
EOF
    exit 1
}

BACKPLANE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --yes)          APPLY=1 ;;
        --reset-method) [ $# -ge 2 ] || usage; RESET_METHOD="$2"; shift ;;
        --backplane)    [ $# -ge 2 ] || usage; BACKPLANE="$2"; shift ;;
        -h|--help)      usage ;;
        *)              echo "unknown option: $1" >&2; usage ;;
    esac
    shift
done

say()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Plan lines are collected and printed before anything runs, so --yes and the
# dry run show the same list and there is no "it did half of it" state.
PLAN=""
plan() { PLAN="$PLAN$1
"; }

# ------------------------------------------------------------------ sanity
uname -a | grep -q VMkernel || die "this is not an ESXi host (uname says $(uname -s)).
       The Proxmox/Linux installer is opt/encs-host/install.sh."
# `command` is not a builtin in the ESXi busybox ash - `command -v` exits 127
# with "sh: command: not found", so the check below used to fail on every real
# host.  `type` is a builtin there and is what busybox actually provides.
type esxcli >/dev/null 2>&1 || die "esxcli not found"

# ------------------------------------------------------- the Marvell device
# Never hardcode the BDF: it has been observed at both 0d:00.0 and 0e:00.0 on
# the same machine across reinstalls, where 0e:00.0 was once the I210 instead.
say "Locating the Marvell switch ASIC (11ab:be00)"
# The field lines must be anchored: the same block also carries "SubVendor ID"
# and "SubDevice ID", and an unanchored /Vendor ID:/ matches those too - which
# happens to work today only because Vendor precedes SubVendor in the output.
DEV=$(esxcli hardware pci list 2>/dev/null | awk '
    /^[0-9a-f][0-9a-f]*:[0-9a-f][0-9a-f]*:/ { addr=$1; vid=""; did="" }
    /^[ \t]*Vendor ID:/ { vid=$3 }
    /^[ \t]*Device ID:/ { did=$3 }
    vid == "0x11ab" && did == "0xbe00" && addr != "" { print addr; addr="" }
' | head -1)
[ -n "$DEV" ] || die "no 11ab:be00 device found. Is this an ENCS 5400?
       Cross-check with: lspci | grep -i 11ab"
info "switch ASIC at $DEV"

# The VMX spelling of the same device. ESXi's own format string, in
# /usr/lib/vmware/drivers/lib/libpci_bus.so, is "%05d:%03d:%02d.%d" - all four
# fields DECIMAL, so bus 0x0d is "013" and slot 0x1d would be "29". Printed in
# the Next section because a hand-written hex BDF fails in a way that names
# neither the key nor the format.
# Parameter expansion and $((0x..)), NOT awk's strtonum(): that is a gawk
# extension and ESXi's busybox awk answers "Call to undefined function".
_r=${DEV#*:}; _seg=${DEV%%:*}; _bus=${_r%%:*}
_r=${_r#*:};  _slot=${_r%%.*}; _fn=${_r#*.}
PT_ID=$(printf '%05d:%03d:%02d.%d' \
    $((0x$_seg)) $((0x$_bus)) $((0x$_slot)) $((0x$_fn)))

# A two-column table - "0000:0d:00.0    false" - not the block-per-device
# layout the rest of `esxcli hardware pci` uses.  Reading it as blocks left
# PT_STATE empty on every host, so the plan always claimed passthrough needed
# enabling and a re-run recorded it a second time.
PT_STATE=$(esxcli hardware pci pcipassthru list 2>/dev/null \
    | awk -v d="$DEV" '$1 == d { print $2; exit }')
info "passthrough currently: ${PT_STATE:-unknown}"
[ "${PT_STATE:-}" = "true" ] || plan "enable passthrough on $DEV"

# --------------------------------------------------------- the te2 backplane
# BOTH X710 functions reach the ASIC (.0 is te1, .1 is te2) but only te2
# carries the management VLAN, so ordering by PCI address and taking the
# second is the whole selection rule - the same one opt/encs-host/install.sh
# uses on Linux.  Picking te1 looks exactly like a switch that never booted.
say "Locating the X710 backplane uplink"
NICS=$(esxcli network nic list 2>/dev/null | awk '$3 == "i40en" { print $2" "$1 }' | sort | awk '{print $2}')
NICCOUNT=$(printf '%s\n' "$NICS" | grep -c . || true)
if [ -z "$BACKPLANE" ]; then
    # $NICS unquoted: word splitting joins the lines with spaces.  ESXi has
    # no `tr`, and the missing binary would have blanked both these lists.
    [ "$NICCOUNT" -ge 2 ] || die "expected 2 i40en uplinks, found ${NICCOUNT}: $(echo $NICS)
       Is this the ENCS host? Is the i40en driver loaded?
       Override with: --backplane vmnicN"
    BACKPLANE=$(printf '%s\n' "$NICS" | sed -n 2p)
fi
esxcli network nic list 2>/dev/null | awk -v n="$BACKPLANE" '$1 == n { f=1 } END { exit !f }' \
    || die "no such uplink: $BACKPLANE
       esxcli network nic list  shows what this host has."
info "i40en uplinks: $(echo $NICS) (ordered by PCI address)"
info "backplane    : $BACKPLANE  (te2 - the one carrying VLAN $VLAN)"

# ------------------------------------------------------------- the vSwitch
# A SECOND vSwitch, always.  vSwitch0 carries vmk0 on the MGMT CPU jack and is
# the one link that depends on no ASIC, no VM and no VLAN - it is how you get
# back in when the switch side goes wrong.  Nothing here touches it.
have_vswitch() {
    esxcli network vswitch standard list --vswitch-name="$1" >/dev/null 2>&1
}
have_pg() {
    esxcli network vswitch standard portgroup list 2>/dev/null \
        | awk -v p="$1" '$1 == p { found=1 } END { exit !found }'
}
have_uplink() {
    esxcli network vswitch standard list --vswitch-name="$1" 2>/dev/null \
        | awk -v n="$2" '/^ *Uplinks:/ { for (i=2; i<=NF; i++) { gsub(",", "", $i); if ($i == n) f=1 } } END { exit !f }'
}
# Which vSwitch, if any, already owns a given uplink.
# The switch name is taken from the block's own "Name:" field, not from the
# unindented header line above it: older ESXi formatters print the fields with
# no header and no indent at all, and keying off the header would then read
# "Name:" as the switch name.
uplink_owner() {
    esxcli network vswitch standard list 2>/dev/null | awk -v n="$1" '
        /^ *Name:/ { sw=$2 }
        /^ *Uplinks:/ { for (i=2; i<=NF; i++) { gsub(",", "", $i); if ($i == n) print sw } }
    ' | head -1
}
pg_switch() {
    esxcli network vswitch standard portgroup list 2>/dev/null \
        | awk -v p="$1" '$1 == p { print $2 }'
}
pg_vlan() {
    esxcli network vswitch standard portgroup list 2>/dev/null \
        | awk -v p="$1" '$1 == p { print $NF }'
}
# A portgroup of the right name on the wrong vSwitch, or with the wrong VLAN,
# is worse than one that is missing: have_pg would report it present, the plan
# would say there is nothing to do, and the guests attached to it would reach
# the wrong place with nothing on screen suggesting why. Refuse instead of
# adopting something this script did not create.
check_existing_pg() {
    have_pg "$1" || return 0
    _sw=$(pg_switch "$1"); _vl=$(pg_vlan "$1")
    [ "$_sw" = "$2" ] || die "portgroup '$1' already exists on $_sw, not $2.
       Nothing was changed. Rename or remove it, or point this script at other
       names: PG_MGMT= and PG_LAN= in the environment."
    [ "$_vl" = "$3" ] || die "portgroup '$1' already exists with VLAN $_vl, and
       this expects VLAN $3. Nothing was changed - it was not created here, so
       it is not this script's to re-tag."
}

# Current MTU of a vSwitch, empty if it does not exist.
vswitch_mtu() {
    esxcli network vswitch standard list --vswitch-name="$1" 2>/dev/null \
        | awk '/^ *MTU:/ { print $2 }'
}

say "Planning the switch vSwitch"

# Refuse to take an uplink away from another vSwitch. On this box the one that
# matters is vSwitch0: it carries vmk0 on the MGMT CPU jack, and moving its
# uplink here would cut the session doing the moving. esxcli would refuse the
# add anyway, but by then the vSwitch is half-built.
OWNER=$(uplink_owner "$BACKPLANE")
if [ -n "$OWNER" ] && [ "$OWNER" != "$VSWITCH" ]; then
    die "$BACKPLANE is already an uplink of $OWNER.
       Nothing was changed. If that is genuinely the te2 backplane port, take
       it off $OWNER yourself first - deliberately, and not over a session
       that rides on it. If $OWNER carries vmk0, it is the wrong port: the
       ESXi management VMkernel belongs on the I210 MGMT CPU jack."
fi

have_vswitch "$VSWITCH" || plan "create vSwitch $VSWITCH (mtu $MTU)"
CUR_MTU=$(vswitch_mtu "$VSWITCH")
[ -z "$CUR_MTU" ] || [ "$CUR_MTU" = "$MTU" ] \
    || plan "set $VSWITCH mtu $CUR_MTU -> $MTU"
have_uplink "$VSWITCH" "$BACKPLANE" || plan "add uplink $BACKPLANE to $VSWITCH"
check_existing_pg "$PG_MGMT" "$VSWITCH" "$VLAN"
check_existing_pg "$PG_LAN" "$VSWITCH" "0"
have_pg "$PG_MGMT" || plan "create portgroup $PG_MGMT on $VSWITCH, VLAN $VLAN"
have_pg "$PG_LAN"  || plan "create portgroup $PG_LAN on $VSWITCH, VLAN 0 (untagged)"

if [ -n "$RESET_METHOD" ]; then
    case "$RESET_METHOD" in
        flr|d3d0|link|bridge|default) ;;
        *) die "--reset-method must be one of: flr d3d0 link bridge default" ;;
    esac
    grep -q '^11ab[[:space:]]*be00' /etc/vmware/passthru.map 2>/dev/null \
        || plan "add '11ab be00 $RESET_METHOD false' to /etc/vmware/passthru.map"
fi

# ------------------------------------------------------------------- plan
say "Plan"
if [ -z "$PLAN" ]; then
    info "nothing to do - everything is already in place"
else
    printf '%s' "$PLAN" | sed 's/^/    /'
fi
cat <<EOF

    Not touched: vSwitch0, vmk0, any existing portgroup, any VM.
    Recorded in : $STATE  (uninstall.sh removes exactly what is listed there)

    $PG_MGMT   VLAN $VLAN - the bootstrap VM's second vNIC goes here.
                        This is how the switch tools reach 169.254.1.0; on
                        Proxmox that job belongs to the host, but ESXi cannot
                        run them, so they run inside the VM instead.
    $PG_LAN            untagged = switch VLAN 1 = all eight GE1/x jacks.
                        This is stock NFVIS lan-net. Attach guests here.
                        Tagged VLANs: encs-esxi-vnet add 100

EOF

if [ "$APPLY" -ne 1 ]; then
    info "dry run - nothing was changed. Re-run with --yes to apply."
    exit 0
fi

# ------------------------------------------------------------------ apply
# Guarded rather than exited on, so a re-run with nothing left to do still
# prints the next steps instead of stopping silently.
if [ -n "$PLAN" ]; then

mkdir -p "$(dirname "$STATE")"
touch "$STATE"
record() { echo "$1" >> "$STATE"; }

say "Applying"

if ! have_vswitch "$VSWITCH"; then
    esxcli network vswitch standard add --vswitch-name="$VSWITCH"
    record "vswitch $VSWITCH"
    info "created $VSWITCH"
fi
if [ "$(vswitch_mtu "$VSWITCH")" != "$MTU" ]; then
    esxcli network vswitch standard set --vswitch-name="$VSWITCH" --mtu="$MTU"
    info "mtu $MTU"
fi

if ! have_uplink "$VSWITCH" "$BACKPLANE"; then
    esxcli network vswitch standard uplink add --uplink-name="$BACKPLANE" --vswitch-name="$VSWITCH"
    record "uplink $BACKPLANE $VSWITCH"
    info "uplink $BACKPLANE"
fi

if ! have_pg "$PG_MGMT"; then
    esxcli network vswitch standard portgroup add --portgroup-name="$PG_MGMT" --vswitch-name="$VSWITCH"
    esxcli network vswitch standard portgroup set --portgroup-name="$PG_MGMT" --vlan-id="$VLAN"
    record "portgroup $PG_MGMT $VSWITCH"
    info "portgroup $PG_MGMT (VLAN $VLAN)"
fi

if ! have_pg "$PG_LAN"; then
    esxcli network vswitch standard portgroup add --portgroup-name="$PG_LAN" --vswitch-name="$VSWITCH"
    record "portgroup $PG_LAN $VSWITCH"
    info "portgroup $PG_LAN (untagged)"
fi

if [ "${PT_STATE:-}" != "true" ]; then
    # --device-id, not --device, and --apply-now is a bare flag, not
    # --active=true. Both wrong forms are rejected outright by ESXi 8.0 U3:
    # "Error: Invalid option --device". --apply-now unbinds the device now
    # instead of at the next reboot.
    esxcli hardware pci pcipassthru set --device-id="$DEV" --enable=true --apply-now
    record "passthru $DEV"
    info "passthrough enabled on $DEV"
fi

if [ -n "$RESET_METHOD" ] \
   && ! grep -q '^11ab[[:space:]]*be00' /etc/vmware/passthru.map 2>/dev/null; then
    echo "11ab  be00  $RESET_METHOD  false" >> /etc/vmware/passthru.map
    record "passthru.map 11ab be00"
    info "passthru.map: 11ab be00 $RESET_METHOD false"
fi

# ESXi restores /etc from the bootbank at boot and only saves it on a clean
# shutdown.  Without this, passthru.map silently reverts, the VM stops
# powering on, and nothing that changed appears to be the cause.
say "Persisting configuration"
if /sbin/auto-backup.sh >/dev/null 2>&1; then
    info "auto-backup.sh done"
else
    warn "auto-backup.sh failed - config may not survive a reboot"
fi

fi   # PLAN

# ----------------------------------------------------------------- verify
say "Next"
cat <<EOF
    1. Check passthrough took effect:
         esxcli hardware pci pcipassthru list | grep $DEV
       If it says pending rather than enabled, reboot the host - a device the
       VMkernel still owns cannot be given to a VM.

    2. Create the bootstrap VM (docs/ESXI.md step 4). The things that are
       easy to miss:
         - reserve ALL guest memory (passthrough will not power on otherwise)
         - EFI, Secure Boot OFF
         - SMBIOS.reflectHost = "TRUE"
         - the pciBridge0/4/5/6/7 lines, or the VM will not power on at all
         - vNIC 1 on your normal VM network, vNIC 2 on $PG_MGMT

       If you attach the device by hand rather than through the Host Client,
       the VMX id is NOT the hex address above. ESXi writes it as
       %05d:%03d:%02d.%d, every field decimal, so for $DEV it is:

         pciPassthru0.present  = "TRUE"
         pciPassthru0.id       = "$PT_ID"
         pciPassthru0.deviceId = "0xbe00"
         pciPassthru0.vendorId = "0x11ab"
         pciPassthru0.systemId = "$(esxcli system uuid get 2>/dev/null)"

       A hex BDF there is refused with "AH No device hints found", which names
       neither the key nor the format. "govc device.pci.add" gets it right too.

    3. In the VM: give vNIC 2 169.254.1.1/16, then
         ping 169.254.1.0
         /opt/encs-host/encs-switch-tui

    Revert everything: sh $(dirname "$0")/uninstall.sh
EOF
