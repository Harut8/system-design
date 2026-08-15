#!/usr/bin/env bash
#
# Stage the notes into a single tree that MkDocs can build from.
#
# The notes live in topic directories at the repo root, but MkDocs needs one
# docs_dir. Rather than move 200+ files (which would break the ~1200 relative
# .md cross-links between them), we copy the topic directories into
# .docs-build/ preserving their relative layout, so every ../other-topic/foo.md
# link still resolves.
#
# File selection comes from `git ls-files`, so anything gitignored -- the 2GB
# lab .venv, __pycache__, personal-roadmap.md -- is excluded for free.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$ROOT/.docs-build"

# Topic directories published to the site. Add new ones here.
CONTENT_DIRS=(
  ai-rag
  databases
  distributed-systems
  gpu-observability
  implementation
  k8s-learn
  kubernetes
  python-mastery
  solutions
  sre-observability
  tasks
)

# Root-level notes published alongside them.
ROOT_DOCS=(
  SYSTEM-DESIGN-GUIDE.md
)

cd "$ROOT"

for d in "${CONTENT_DIRS[@]}"; do
  [ -d "$d" ] || { echo "stage-docs: missing content dir '$d'" >&2; exit 1; }
done

rm -rf "$STAGE"
mkdir -p "$STAGE"

# Markdown (and any per-directory .nav.yml) from the tracked tree.
LIST="$(mktemp)"
trap 'rm -f "$LIST"' EXIT
git ls-files -- "${CONTENT_DIRS[@]}" "${ROOT_DOCS[@]}" \
  | grep -E '(\.md|\.MD|(^|/)\.nav\.yml)$' \
  > "$LIST"

count=$(wc -l < "$LIST" | tr -d ' ')
[ "$count" -gt 0 ] || { echo "stage-docs: no markdown found" >&2; exit 1; }

rsync -a --files-from="$LIST" "$ROOT/" "$STAGE/"

# Site-only files (landing page, top-level nav) overlay the staged tree.
cp -R "$ROOT/docs/." "$STAGE/"

echo "stage-docs: staged $count tracked file(s) + docs/ overlay into $STAGE"
