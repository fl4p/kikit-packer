#!/bin/sh
set -eu

SOURCE_REF=43286bd2f97a3f36d97fef67e27c20e4ef45d0b0
SOURCE_SHA256=34003c971680124c3b1eeb77ff35d1178495835bc584feff36c1071493d365d3
SOURCE_URL="https://codeload.github.com/fl4p/kikit-packer/tar.gz/$SOURCE_REF"

workdir=$(mktemp -d "${TMPDIR:-/tmp}/kikit-packer-bootstrap.XXXXXX")
trap 'rm -rf "$workdir"' EXIT HUP INT TERM
archive="$workdir/source.tar.gz"
source_dir="$workdir/source"

curl -fsSL "$SOURCE_URL" -o "$archive"
actual_sha256=$(shasum -a 256 "$archive" | awk '{print $1}')
if [ "$actual_sha256" != "$SOURCE_SHA256" ]; then
    echo "KiKit Packer source archive SHA-256 mismatch" >&2
    exit 1
fi
mkdir "$source_dir"
tar -xzf "$archive" -C "$source_dir" --strip-components=1
"$source_dir/installer/install-macos.sh" "$@"
