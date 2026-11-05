#!/usr/bin/env bash
# Install the MIDI Remote script into Cubase/Nuendo's Driver Scripts folder(s).
#
# Usage: ./scripts/install_cubase_script.sh [cubase|nuendo]
#   - Installs into EVERY existing "MIDI Remote/Driver Scripts" folder under
#     ~/Documents/Steinberg (Cubase and/or Nuendo, including versioned dirs),
#     so whichever host instance is running picks it up.
#   - If none exists yet, creates one for the given host (default: cubase).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS_SRC="$REPO_DIR/cubase_script/Local"
HOST_ARG="${1:-}"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "Run this script manually on Windows: copy cubase_script/Local/* to" \
         "%USERPROFILE%\\Documents\\Steinberg\\<Cubase or Nuendo>\\MIDI Remote\\Driver Scripts\\Local\\"
    exit 1
fi

if [[ -n "$HOST_ARG" && "$HOST_ARG" != "cubase" && "$HOST_ARG" != "nuendo" ]]; then
    echo "Usage: $0 [cubase|nuendo]" >&2
    exit 1
fi

SHARED_ROOT="$HOME/Documents/Steinberg"

# Every host folder that already has a Driver Scripts folder
TARGETS=()
if [[ -d "$SHARED_ROOT" ]]; then
    while IFS= read -r -d '' host; do
        if [[ -d "$host/MIDI Remote/Driver Scripts" ]]; then
            TARGETS+=("$host/MIDI Remote/Driver Scripts")
        fi
    done < <(find "$SHARED_ROOT" -maxdepth 1 -type d \( -iname "cubase*" -o -iname "nuendo*" \) -print0 | sort -z)
fi

if [[ "${#TARGETS[@]}" -eq 0 ]]; then
    # Hosts create this folder on first MIDI Remote panel open; make one up front
    FALLBACK_HOST="Cubase"
    if [[ "$HOST_ARG" == "nuendo" ]]; then
        FALLBACK_HOST="Nuendo"
    fi
    echo "No existing Driver Scripts folder found; creating one for $FALLBACK_HOST."
    if [[ -z "$HOST_ARG" ]]; then
        echo "(Nuendo ユーザーは明示的に指定できます: ./scripts/install_cubase_script.sh nuendo)"
    fi
    TARGETS+=("$SHARED_ROOT/$FALLBACK_HOST/MIDI Remote/Driver Scripts")
    mkdir -p "${TARGETS[0]}/Local"
fi

for target in "${TARGETS[@]}"; do
    mkdir -p "$target/Local"
    cp -R "$SCRIPTS_SRC/." "$target/Local/"
    echo "Installed into: $target/Local/ClaudeCode/"
done

echo
echo "Next steps in Cubase/Nuendo:"
echo "  1. Open (or reopen) the MIDI Remote panel (lower zone 'Remote' tab)"
echo "  2. Press 'Reload Scripts' if the panel was already open"
echo "  3. The device 'ClaudeCode MCP_Bridge' should appear once the IAC/loopMIDI port exists"
