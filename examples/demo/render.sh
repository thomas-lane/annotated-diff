#!/bin/sh
# Render the demo: three files compared in file mode, with the notes in notes.json.
# Usage: examples/demo/render.sh [OUT.html] [--open]
set -e
here=$(cd "$(dirname "$0")" && pwd)
script="$here/../../plugins/annotated-diff/skills/annotated-diff/scripts/annotated_diff.py"
out=${1:-"${TMPDIR:-/tmp}/annotated-diff-demo.html"}
[ $# -gt 0 ] && shift
python3 "$script" \
  --pair inventory.py "$here/before/inventory.py" "$here/after/inventory.py" \
  --pair README.md "$here/before/README.md" "$here/after/README.md" \
  --pair stock.json /dev/null "$here/after/stock.json" \
  --notes "$here/notes.json" \
  --title "Inventory fixes" --subtitle "demo: \`before/\` → \`after/\`" \
  --out "$out" "$@"
