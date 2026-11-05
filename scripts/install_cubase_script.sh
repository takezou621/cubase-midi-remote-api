#!/usr/bin/env bash
# Install the MIDI Remote script into Cubase/Nuendo's Driver Scripts folder.
# macOS: ~/Documents/Steinberg/<Cubase|Nuendo>/MIDI Remote/Driver Scripts
# Windows: %USERPROFILE%\Documents\Steinberg\<Cubase|Nuendo>\MIDI Remote\Driver Scripts
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS_SRC="$REPO_DIR/cubase_script/Local"

if [[ "$(uname)" == "Darwin" ]]; then
    DOCS="$HOME/Documents"
    HOST_CANDIDATES=("$DOCS/Steinberg/Cubase" "$DOCS/Steinberg/Nuendo" \
                     "$DOCS"/Steinberg/Cubase\ * "$DOCS"/Steinberg/Nuendo\ *)
    DIR_SEP="/"
else
    echo "Run this script manually on Windows: copy cubase_script/Local/* to" \
         "%USERPROFILE%\\Documents\\Steinberg\\<Cubase or Nuendo>\\MIDI Remote\\Driver Scripts\\Local\\"
    exit 1
fi

TARGET_BASE=""
for host in "${HOST_CANDIDATES[@]}"; do
    if [[ -d "$host/MIDI Remote/Driver Scripts" ]]; then
        TARGET_BASE="$host/MIDI Remote/Driver Scripts"
        break
    fi
done

if [[ -z "$TARGET_BASE" ]]; then
    # Cubase has not created the folder yet (it is created on first MIDI Remote panel open)
    TARGET_BASE="$DOCS/Steinberg/Cubase/MIDI Remote/Driver Scripts"
    echo "Driver Scripts folder not found; creating: $TARGET_BASE"
    mkdir -p "$TARGET_BASE/Local"
fi

mkdir -p "$TARGET_BASE/Local"
cp -R "$SCRIPTS_SRC/." "$TARGET_BASE/Local/"

echo "Installed:"
find "$TARGET_BASE/Local/ClaudeCode" -type f -print
echo
echo "Next steps in Cubase:"
echo "  1. Open (or reopen) the MIDI Remote panel (lower zone 'Remote' tab)"
echo "  2. Press 'Reload Scripts' if the panel was already open"
echo "  3. The device 'ClaudeCode MCP_Bridge' should appear once the IAC/loopMIDI port exists"
