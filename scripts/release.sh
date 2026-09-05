#!/bin/bash
# Cut a GitHub release: the HYPERVISOR-side tools (encs-host-<ver>.tar.gz,
# what encs-switch-tui --update installs) and the image build tree
# (encs-image-builder-<ver>.tar.gz: build.sh, scripts/, kickstart/, payload/, docs -
# everything needed to build the ISO/qcow2 without a git checkout).
#
#   ./scripts/release.sh            # build the tarballs into out/, do not publish
#   ./scripts/release.sh --publish  # ... and create the GitHub release
#
# The version comes from VERSION in encs-switch-tui - that is the single
# source of truth, and it is what --update compares against. Bump it there.
#
# ONLY our own code ships here. Nothing extracted from a Cisco ISO is in
# either tarball (the builder extracts it from the ISO you supply, at build
# time), so a release carries no proprietary material.
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
python3 -m py_compile "$BUNDLE/encs-switch-vnet" \
    || die "encs-switch-vnet does not compile"
bash -n "$BUNDLE/install.sh" || die "install.sh is not valid bash"
bash -n "$BUNDLE/encs-switch-api" || die "encs-switch-api is not valid bash"
bash -n "$ROOT/build.sh" || die "build.sh is not valid bash"
for f in "$ROOT"/scripts/*.sh; do bash -n "$f" || die "$(basename "$f") is not valid bash"; done
for f in "$ROOT"/scripts/*.py; do python3 -m py_compile "$f" || die "$(basename "$f") does not compile"; done

# The offline suite. It cannot prove the firmware accepts a write, but it
# does prove every view renders, every write is well-formed XML with the
# element names switch-confd used, and config save/replay round-trips.
# Shipping a release that fails it is never right.
say "Running the offline test suite"
python3 "$HERE/60-test-tui.py" || die "offline tests failed - not releasing"
python3 "$HERE/64-test-vnet.py" || die "vnet tests failed - not releasing"

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
install -m 0755 "$BUNDLE/encs-switch-vnet"          "$STAGE/"
install -m 0755 "$BUNDLE/install.sh"                "$STAGE/"
install -m 0644 "$BUNDLE/encs-switch-replay.service" "$STAGE/"
install -m 0644 "$BUNDLE/encs-switch-startup.service" "$STAGE/"
install -m 0644 "$BUNDLE/README"                    "$STAGE/"

# The MANIFEST is what --update obeys: mode, path in tarball, destination.
# An OLD updater installs a NEW release, so this format has to stay stable.
# Destinations must sit under one of ALLOWED_DESTS in encs-switch-tui.
cat > "$STAGE/MANIFEST" <<'EOF'
# mode  file                        destination
0755    encs-switch-tui             /usr/local/sbin/encs-switch-tui
0755    encs-switch-api             /usr/local/sbin/encs-switch-api
0755    encs-switch-vnet            /usr/local/sbin/encs-switch-vnet
0644    encs-switch-replay.service  /etc/systemd/system/encs-switch-replay.service
0644    encs-switch-startup.service /etc/systemd/system/encs-switch-startup.service
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

# --- the builder: the tree minus build outputs, Cisco material and git ----
BNAME="encs-image-builder-$VER"
BSTAGE="$OUT/$BNAME"
BTARBALL="$OUT/$BNAME.tar.gz"
rm -rf "$BSTAGE"; mkdir -p "$BSTAGE"
for item in build.sh scripts kickstart payload docs README.md LICENSE NOTICE; do
    [ -e "$ROOT/$item" ] || die "builder item missing: $item"
    cp -R "$ROOT/$item" "$BSTAGE/"
done
find "$BSTAGE" \( -name __pycache__ -o -name '*.pyc' -o -name .DS_Store \) -prune -exec rm -rf {} +
# Belt and braces: nothing proprietary can be in the tree, but .gitignore is
# the only thing keeping it that way, so refuse if anything slipped through.
if find "$BSTAGE" \( -name '*.iso' -o -name '*.qcow2' -o -name '*.rpm' -o -name '*.ko' \
        -o -name '*.ko.xz' -o -name '*.bin' -o -name '*.SPA' \) | grep -q .; then
    die "the builder stage contains binary/proprietary files - not releasing"
fi
rm -f "$BTARBALL"
COPYFILE_DISABLE=1 tar -C "$OUT" --numeric-owner --owner=0 --group=0 \
    $(tar --no-xattrs --version >/dev/null 2>&1 && echo --no-xattrs) \
    -czf "$BTARBALL" "$BNAME"
rm -rf "$BSTAGE"

SUMS="$OUT/SHA256SUMS"
# sha256sum on Linux, shasum -a 256 on macOS. Both emit "<hex>  <name>", the
# format encs-switch-tui --update parses (it looks up its own tarball by
# name, so the second line does not confuse it).
if command -v sha256sum >/dev/null 2>&1; then
    ( cd "$OUT" && sha256sum "$(basename "$TARBALL")" "$(basename "$BTARBALL")" > "$(basename "$SUMS")" )
else
    ( cd "$OUT" && shasum -a 256 "$(basename "$TARBALL")" "$(basename "$BTARBALL")" > "$(basename "$SUMS")" )
fi

info "$(basename "$TARBALL")  $(du -h "$TARBALL" | cut -f1)"
info "$(basename "$BTARBALL")  $(du -h "$BTARBALL" | cut -f1)"
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
tar -C "$TMP" -xzf "$BTARBALL"
[ -x "$TMP/$BNAME/build.sh" ] || die "build.sh missing from the builder tarball"
[ -f "$TMP/$BNAME/kickstart/ks-encs.cfg" ] || die "kickstart missing from the builder tarball"
[ -f "$TMP/$BNAME/scripts/50-verify-qcow2.py" ] || die "scripts/ missing from the builder tarball"
( cd "$TMP/$BNAME" && ./build.sh --help >/dev/null 2>&1 || [ $? -eq 1 ] ) \
    || die "the packaged build.sh does not run"
info "builder tarball unpacks and runs"

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
# A pre-0.2.4 --update takes the first .tar.gz asset it sees, and GitHub
# lists assets by name - which is why the builder is "encs-image-builder"
# (sorts after "encs-host"), not "encs-builder".
gh release create "$TAG" "$TARBALL" "$BTARBALL" "$SUMS" \
    --repo "$(git -C "$ROOT" remote get-url origin \
              | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')" \
    --title "encs-host $VER" \
    --notes "Hypervisor-side switch tools for the Cisco ENCS 5400.

Install or upgrade an existing host:

    encs-switch-tui --update

First install: see README.md.

encs-image-builder-$VER.tar.gz is the image build tree (build.sh, kickstart,
scripts, payload, docs) for building the installer ISO and qcow2 from your
own Cisco NFVIS ISO - no git checkout needed. No Cisco software is included
in this release."

say "Published $TAG"
