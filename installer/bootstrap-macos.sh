#!/bin/sh
set -eu

SOURCE_REF=80a43fcc77b69113da4f617a33005262b7601e0b
SOURCE_SHA256=44bb7b3054688cc3f4c4970f103186bd68ff9d1eef3974eb740d8e8a8b08c39e
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
