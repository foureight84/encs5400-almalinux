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

STATE="${STATE:-/etc/encs-esxi/created}"
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

if [ ! -f "$STATE" ]; then
    if [ "$FORCE" -ne 1 ]; then
        die "no record at $STATE - install.sh either never ran or ran with a
       different STATE. Nothing removed: this script will not guess at which
       parts of your networking belong to it.
       If you are sure, re-run with --force to match the default names
       (vSwitchENCS, encs-mgmt-2363, encs-lan, encs-lan-*)."
    fi
    warn "no $STATE - falling back to default names (--force)"
    STATE=/dev/null
fi

# Read the record newest-first: portgroups before the vSwitch that holds them,
# uplinks before the switch, so nothing is removed out from under something.
ITEMS=$( (cat "$STATE" 2>/dev/null; \
          if [ "$FORCE" -eq 1 ]; then
              esxcli network vswitch standard portgroup list 2>/dev/null \
                | awk '$1 ~ /^encs-/ { print "portgroup "$1" "$2 }'
              esxcli network vswitch standard list --vswitch-name=vSwitchENCS >/dev/null 2>&1 \
                && echo "vswitch vSwitchENCS"
          fi) | awk '!seen[$0]++')

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

# Order: portgroups, then uplinks, then the vSwitch, then passthrough. Each
# failure is reported and skipped rather than aborting - a half-removed
# vSwitch is worse than a fully-attempted one.
for kind in portgroup uplink vswitch passthru passthru.map; do
    printf '%s\n' "$ITEMS" | while read -r k a b; do
        [ "$k" = "$kind" ] || continue
        case "$k" in
        portgroup)
            if esxcli network vswitch standard portgroup remove \
                   --portgroup-name="$a" --vswitch-name="${b:-vSwitchENCS}"; then
                info "removed portgroup $a"
            else
                warn "could not remove portgroup $a"
            fi ;;
        uplink)
            if esxcli network vswitch standard uplink remove \
                   --uplink-name="$a" --vswitch-name="${b:-vSwitchENCS}"; then
                info "removed uplink $a"
            else
                warn "could not remove uplink $a"
            fi ;;
        vswitch)
            if esxcli network vswitch standard remove --vswitch-name="$a"; then
                info "removed vSwitch $a"
            else
                warn "could not remove vSwitch $a"
            fi ;;
        passthru)
            if esxcli hardware pci pcipassthru set --device="$a" --enable=false --active=true; then
                info "passthrough disabled on $a"
            else
                warn "could not disable passthrough on $a"
            fi ;;
        passthru.map)
            if [ -f /etc/vmware/passthru.map ]; then
                if sed -i '/^11ab[[:space:]]*be00/d' /etc/vmware/passthru.map; then
                    info "removed the 11ab be00 line from passthru.map"
                else
                    warn "could not edit passthru.map"
                fi
            fi ;;
        esac
    done
done

say "Persisting configuration"
if /sbin/auto-backup.sh >/dev/null 2>&1; then
    info "auto-backup.sh done"
else
    warn "auto-backup.sh failed - the removal may not survive a reboot"
fi

rm -f "$STATE" 2>/dev/null || true

say "Done"
cat <<EOF
    The host is back to stock: the X710 is an unused vmnic again and the
    Marvell device returns to the VMkernel, which has no driver for it and
    does nothing with it.

    Reboot to be sure passthrough is fully released:
      reboot

    The SWITCH is still running with whatever config it last had. Only
    removing AC power resets it to firmware defaults.
EOF
