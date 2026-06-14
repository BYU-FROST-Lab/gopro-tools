#!/bin/bash
set -euo pipefail

# Simple rsync wrapper: copy from SOURCE to DEST
# Usage: copy_file.sh -s <SOURCE> -d <DEST> [-o "additional rsync options"]

SOURCE=""
DEST=""
RSYNC_OPTS=""

usage() {
  cat <<EOF >&2
Usage: $0 -s <SOURCE> -d <DEST> [-o "RSYNC_OPTIONS"]

Examples:
  $0 -s /path/to/src -d /path/to/dest
  $0 -s user@host:/remote/path -d ./localdir
EOF
  exit 1
}

while getopts "s:d:o:h" opt; do
  case $opt in
    s) SOURCE="$OPTARG" ;;
    d) DEST="$OPTARG" ;;
    o) RSYNC_OPTS="$OPTARG" ;;
    h) usage ;;
    *) usage ;;
  esac
done

if [ -z "$SOURCE" ] || [ -z "$DEST" ]; then
  usage
fi

# Ensure destination exists (for local destinations)
mkdir -p "${DEST%/}"

# Run rsync (trailing slash on SOURCE copies contents)
rsync -avzh --progress $RSYNC_OPTS "${SOURCE%/}/" "${DEST%/}/"

echo "Done: ${SOURCE} -> ${DEST}"