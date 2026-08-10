#!/bin/bash
# Cut a GitHub release of the HYPERVISOR-side tools.
#
#   ./scripts/release.sh            # build the tarball into out/, do not publish
#   ./scripts/release.sh --publish  # ... and create the GitHub release
#
# The version comes from VERSION in encs-switch-tui - that is the single
# source of truth, and it is what --update compares against. Bump it there.
#
# ONLY our own code ships here. Nothing extracted from a Cisco ISO is in the
# host bundle, so a release carries no proprietary material.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
. "$HERE/lib.sh"

PUBLISH=0
[ "${1:-}" = "--publish" ] && PUBLISH=1
[ $# -gt 1 ] && die "usage: $0 [--publish]"
[ $# -eq 1 ] && [ "$PUBLISH" -eq 0 ] && die "unknown option: $1"

BUNDLE="$ROOT/payload/opt/encs-host"
TUI="$BUNDLE/encs-switch-tui"
[ -f "$TUI" ] || die "not found: $TUI"

VER=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$TUI" | head -1)
[ -n "$VER" ] || die "could not read VERSION from $TUI"
TAG="v$VER"
NAME="encs-host-$VER"
OUT="$ROOT/out"
STAGE="$OUT/$NAME"

say "Releasing $NAME"

# --- sanity checks before anything is packaged ----------------------------
command -v gh >/dev/null 2>&1 || warn "gh not installed - can build but not publish"
python3 -m py_compile "$TUI" || die "encs-switch-tui does not compile"
bash -n "$BUNDLE/install.sh" || die "install.sh is not valid bash"
bash -n "$BUNDLE/encs-switch-api" || die "encs-switch-api is not valid bash"

if git -C "$ROOT" rev-parse "$TAG" >/dev/null 2>&1; then
    die "tag $TAG already exists - bump VERSION in $TUI first"
fi
if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
    warn "working tree is dirty; the release will not match a clean checkout"
fi

# --- stage ----------------------------------------------------------------
rm -rf "$STAGE"; mkdir -p "$STAGE"
install -m 0755 "$BUNDLE/encs-switch-tui"           "$STAGE/"
install -m 0755 "$BUNDLE/encs-switch-api"           "$STAGE/"
install -m 0755 "$BUNDLE/install.sh"                "$STAGE/"
install -m 0644 "$BUNDLE/encs-switch-replay.service" "$STAGE/"
install -m 0644 "$BUNDLE/README"                    "$STAGE/"

# The MANIFEST is what --update obeys: mode, path in tarball, destination.
# An OLD updater installs a NEW release, so this format has to stay stable.
# Destinations must sit under one of ALLOWED_DESTS in encs-switch-tui.
cat > "$STAGE/MANIFEST" <<'EOF'
# mode  file                        destination
0755    encs-switch-tui             /usr/local/sbin/encs-switch-tui
0755    encs-switch-api             /usr/local/sbin/encs-switch-api
0644    encs-switch-replay.service  /etc/systemd/system/encs-switch-replay.service
0755    install.sh                  /opt/encs-host/install.sh
0644    README                      /opt/encs-host/README
EOF

# --- package --------------------------------------------------------------
TARBALL="$OUT/$NAME.tar.gz"
rm -f "$TARBALL"
# No owner metadata, so rebuilding the same commit gives the same bytes.
# COPYFILE_DISABLE and --no-xattrs keep macOS from stapling
# LIBARCHIVE.xattr.com.apple.provenance headers onto every member, which GNU
# tar on the target then warns about on every single file.
COPYFILE_DISABLE=1 tar -C "$OUT" --numeric-owner --owner=0 --group=0 \
    $(tar --no-xattrs --version >/dev/null 2>&1 && echo --no-xattrs) \
    -czf "$TARBALL" "$NAME"
rm -rf "$STAGE"

SUMS="$OUT/SHA256SUMS"
# sha256sum on Linux, shasum -a 256 on macOS. Both emit "<hex>  <name>", the
# format encs-switch-tui --update parses.
if command -v sha256sum >/dev/null 2>&1; then
    ( cd "$OUT" && sha256sum "$(basename "$TARBALL")" > "$(basename "$SUMS")" )
else
    ( cd "$OUT" && shasum -a 256 "$(basename "$TARBALL")" > "$(basename "$SUMS")" )
fi

info "$(basename "$TARBALL")  $(du -h "$TARBALL" | cut -f1)"
info "$(cat "$SUMS")"

# Prove the tarball is installable before it goes anywhere.
say "Verifying the tarball"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
tar -C "$TMP" -xzf "$TARBALL"
[ -f "$TMP/$NAME/MANIFEST" ] || die "MANIFEST missing from the tarball"
while read -r mode file dest; do
    case "$mode" in ''|\#*) continue ;; esac
    [ -f "$TMP/$NAME/$file" ] || die "MANIFEST names $file, not in the tarball"
    case "$dest" in
        /usr/local/sbin/*|/etc/systemd/system/*|/opt/encs-host/*) ;;
        *) die "MANIFEST destination $dest is outside the allowed prefixes" ;;
    esac
done < "$TMP/$NAME/MANIFEST"
"$TMP/$NAME/encs-switch-tui" --version | grep -q "$VER" \
    || die "packaged binary does not report $VER"
info "manifest, paths and version all check out"

if [ "$PUBLISH" -eq 0 ]; then
    cat <<EOF

Built but NOT published. To publish:

    ./scripts/release.sh --publish

That will tag $TAG and create the GitHub release. Until then nothing has
left this machine.
EOF
    exit 0
fi

# --- publish --------------------------------------------------------------
command -v gh >/dev/null 2>&1 || die "gh is required to publish"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated (gh auth login)"

say "Tagging $TAG"
git -C "$ROOT" tag -a "$TAG" -m "encs-host $VER"
git -C "$ROOT" push origin "$TAG"

say "Creating the GitHub release"
gh release create "$TAG" "$TARBALL" "$SUMS" \
    --repo "$(git -C "$ROOT" remote get-url origin \
              | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')" \
    --title "encs-host $VER" \
    --notes "Hypervisor-side switch tools for the Cisco ENCS 5400.

Install or upgrade an existing host:

    encs-switch-tui --update

First install: see README.md. No Cisco software is included in this release."

say "Published $TAG"
