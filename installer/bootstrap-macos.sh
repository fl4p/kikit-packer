#!/bin/sh
set -eu

SOURCE_REF=d4746e6e77b8a9b4c601f4307cbeaf7fdd4adb2d
SOURCE_SHA256=7f1a87032cc6e17504c80a89e9b2fee753f149779a9cd7ec8839338dccacb498
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
