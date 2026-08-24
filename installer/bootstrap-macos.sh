#!/bin/sh
set -eu

SOURCE_REF=666e17c3c7c1b278b42b1041dd58fbe01fb97373
SOURCE_SHA256=78b87b76cab5eb2f100945b3236fe44c65243ef487a23244109b98ea958cc9ed
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
