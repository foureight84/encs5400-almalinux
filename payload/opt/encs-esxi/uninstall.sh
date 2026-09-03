#!/bin/sh
# Undo opt/encs-esxi/install.sh.  EXPERIMENTAL.
#
# Removes exactly what install.sh recorded in $STATE and nothing else, in the
# order that keeps the host reachable throughout.  vSwitch0 and vmk0 are never
# touched - if management is on them (and docs/ESXI.md says it must be), you
# stay logged in for the whole run.
#
#   sh /vmfs/volumes/datastore1/encs-esxi/uninstall.sh          # show the plan
#   sh /vmfs/volumes/datastore1/encs-esxi/uninstall.sh --yes    # do it
#
# What this does NOT undo:
#   - the VM.  Power it off and unregister/delete it yourself first; this
#     refuses to remove a portgroup that still has clients on it.
#   - the SWITCH.  Its config lives in ASIC RAM and survives until the chassis
#     loses AC.  A CIMC power-off is not enough.  Pull the cord if you want
#     firmware defaults back.
set -eu

# Alongside the bundle on the datastore, not under /etc - see install.sh.
STATE="${STATE:-$(dirname "$0")/created}"
VSWITCH="${VSWITCH:-vSwitchENCS}"
APPLY=0
FORCE=0

usage() {
    cat <<EOF
usage: sh $0 [--yes] [--force]

  --yes     apply (default: print the plan and stop)
  --force   also remove things install.sh did not record, matched by the
            default names. Use only if $STATE was lost.
EOF
    exit 1
}
while [ $# -gt 0 ]; do
    case "$1" in
        --yes)   APPLY=1 ;;
        --force) FORCE=1 ;;
        -h|--help) usage ;;
        *) echo "unknown option: $1" >&2; usage ;;
    esac
    shift
done

say()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

uname -a | grep -q VMkernel || die "this is not an ESXi host"

HAVE_STATE=1
if [ ! -f "$STATE" ]; then
    if [ "$FORCE" -ne 1 ]; then
        die "no record at $STATE - install.sh either never ran or ran with a
       different STATE. Nothing removed: this script will not guess at which
       parts of your networking belong to it.
       If you are sure, re-run with --force to match the default names
       (vSwitchENCS, encs-mgmt-2363, encs-lan, encs-lan-*)."
    fi
    warn "no $STATE - falling back to default names (--force)"
    # NB: NOT `STATE=/dev/null`. It reads as a harmless empty file right up
    # until the `rm -f "$STATE"` at the end deletes the host's /dev/null.
    HAVE_STATE=0
fi

# --force has to rediscover what the record would have held. Portgroups and the
# vSwitch come from their names; the uplink and the passthrough device do not
# have guessable names, so they are read back off the vSwitch and off the PCI
# list - the same query install.sh used to find them in the first place.
force_items() {
    esxcli network vswitch standard portgroup list 2>/dev/null \
        | awk '$1 ~ /^encs-/ { print "portgroup "$1" "$2 }'
    esxcli network vswitch standard list --vswitch-name="$VSWITCH" 2>/dev/null \
        | awk -v s="$VSWITCH" '/^ *Uplinks:/ { for (i=2; i<=NF; i++) { gsub(",", "", $i); print "uplink "$i" "s } }'
    if esxcli network vswitch standard list --vswitch-name="$VSWITCH" >/dev/null 2>&1; then
        echo "vswitch $VSWITCH"
    fi
    dev=$(esxcli hardware pci list 2>/dev/null | awk '
        /^[0-9a-f][0-9a-f]*:[0-9a-f][0-9a-f]*:/ { addr=$1; vid=""; did="" }
        /^[ \t]*Vendor ID:/ { vid=$3 }
        /^[ \t]*Device ID:/ { did=$3 }
        vid == "0x11ab" && did == "0xbe00" && addr != "" { print addr; addr="" }
    ' | head -1)
    [ -z "$dev" ] || echo "passthru $dev"
    grep -q '^11ab[[:space:]]*be00' /etc/vmware/passthru.map 2>/dev/null \
        && echo "passthru.map 11ab be00"
    return 0
}

# The record is in CREATION order, which is the reverse of what is safe to
# remove: portgroups have to go before the vSwitch holding them, and the uplink
# before the switch it belongs to. Sort into removal order once, here, so that
# the plan the operator reads is the order things actually happen in - a plan
# that listed the vSwitch first would be describing a run that cannot work.
ITEMS=$( (if [ "$HAVE_STATE" -eq 1 ]; then cat "$STATE"; fi
          if [ "$FORCE" -eq 1 ]; then force_items; fi) \
        | awk 'NF && !seen[$0]++' \
        | awk '{ r = 5
                 if ($1 == "portgroup") r = 1
                 else if ($1 == "uplink") r = 2
                 else if ($1 == "vswitch") r = 3
                 else if ($1 == "passthru") r = 4
                 print r" "$0 }' \
        | sort -k1,1n | cut -d' ' -f2-)

[ -n "$ITEMS" ] || { info "nothing recorded - nothing to do"; exit 0; }

say "Will remove"
printf '%s\n' "$ITEMS" | sed 's/^/    /'
cat <<EOF

    Not touched: vSwitch0, vmk0, any VM, any other portgroup.
    The switch keeps running and keeps its config until AC is removed.
EOF

# A portgroup with a VM still on it cannot be removed, and a VM whose network
# disappears loses its NIC at the next power-on. Say so before, not after.
BUSY=""
for pg in $(printf '%s\n' "$ITEMS" | awk '$1 == "portgroup" { print $2 }'); do
    n=$(esxcli network vswitch standard portgroup list 2>/dev/null \
        | awk -v p="$pg" '$1 == p { print $3 }')
    # NB: not `[ ... ] && BUSY=...` as the last statement of the loop body -
    # under `set -e` a false test is a failing command and would end the run
    # here, silently reporting nothing busy. Same trap as in encs-host.
    if [ "${n:-0}" -gt 0 ] 2>/dev/null; then
        BUSY="$BUSY $pg($n)"
    fi
done
if [ -n "$BUSY" ]; then
    warn "still has clients:$BUSY"
    warn "move those VMs off, or power them down, before removing the portgroup."
fi

if [ "$APPLY" -ne 1 ]; then
    info "dry run - nothing was changed. Re-run with --yes to apply."
    exit 0
fi
[ -z "$BUSY" ] || die "refusing to remove a portgroup that still has clients:$BUSY"

say "Removing"

# The list goes through a file rather than a pipe: `... | while read` runs the
# loop in a subshell, and the tally of what failed would not survive it - which
# is the difference between keeping the record and deleting it while the
# objects it names are still there.
WORK="${TMPDIR:-/tmp}/encs-esxi-uninstall.$$"
LEFT="$WORK.left"
printf '%s\n' "$ITEMS" > "$WORK"
: > "$LEFT"

# ITEMS is already in removal order. Each failure is reported and skipped
# rather than aborting - a half-removed vSwitch is worse than a fully-attempted
# one, and what did not come out is written back to the record at the end.
while read -r k a b; do
    OK=1
    case "$k" in
    portgroup)
        if esxcli network vswitch standard portgroup remove \
               --portgroup-name="$a" --vswitch-name="${b:-$VSWITCH}"; then
            info "removed portgroup $a"
        else
            warn "could not remove portgroup $a"; OK=0
        fi ;;
    uplink)
        if esxcli network vswitch standard uplink remove \
               --uplink-name="$a" --vswitch-name="${b:-$VSWITCH}"; then
            info "removed uplink $a"
        else
            warn "could not remove uplink $a"; OK=0
        fi ;;
    vswitch)
        if esxcli network vswitch standard remove --vswitch-name="$a"; then
            info "removed vSwitch $a"
        else
            warn "could not remove vSwitch $a"; OK=0
        fi ;;
    passthru)
        # --device-id / --apply-now: see the note in install.sh. ESXi 8.0 U3
        # rejects --device and --active outright.
        if esxcli hardware pci pcipassthru set --device-id="$a" --enable=false --apply-now; then
            info "passthrough disabled on $a"
        else
            warn "could not disable passthrough on $a"; OK=0
        fi ;;
    passthru.map)
        if [ -f /etc/vmware/passthru.map ]; then
            if sed -i '/^11ab[[:space:]]*be00/d' /etc/vmware/passthru.map; then
                info "removed the 11ab be00 line from passthru.map"
            else
                warn "could not edit passthru.map"; OK=0
            fi
        fi ;;
    *)
        warn "unknown entry in the record, skipped: $k $a $b" ;;
    esac
    [ "$OK" -eq 1 ] || echo "$k $a $b" >> "$LEFT"
done < "$WORK"

say "Persisting configuration"
if /sbin/auto-backup.sh >/dev/null 2>&1; then
    info "auto-backup.sh done"
else
    warn "auto-backup.sh failed - the removal may not survive a reboot"
fi

say "Done"
if [ -s "$LEFT" ]; then
    warn "some items could not be removed:"
    sed 's/^/      /' "$LEFT" >&2
    if [ "$HAVE_STATE" -eq 1 ]; then
        sed 's/  *$//' "$LEFT" > "$STATE"
        warn "$STATE now lists only those - fix the cause and re-run."
    fi
elif [ "$HAVE_STATE" -eq 1 ]; then
    rm -f "$STATE" 2>/dev/null || true
fi
# `[ ... ] && LEFTOVER=1` on its own line would end the script here under
# `set -e` whenever the test is false, i.e. on every clean run.
LEFTOVER=0
if [ -s "$LEFT" ]; then LEFTOVER=1; fi
rm -f "$WORK" "$LEFT" 2>/dev/null || true

if [ "$LEFTOVER" -eq 0 ]; then
    cat <<EOF
    The host is back to stock: the X710 is an unused vmnic again and the
    Marvell device returns to the VMkernel, which has no driver for it and
    does nothing with it.
EOF
else
    cat <<EOF
    NOT fully removed - see the warnings above. Everything else is gone; the
    host is reachable either way, because none of this touches vSwitch0.
EOF
fi
cat <<EOF

    Reboot to be sure passthrough is fully released:
      reboot

    The SWITCH is still running with whatever config it last had. Only
    removing AC power resets it to firmware defaults.
EOF
