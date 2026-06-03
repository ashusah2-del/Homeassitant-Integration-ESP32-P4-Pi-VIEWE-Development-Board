#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <repo-name> [visibility: private|public]"
  exit 1
fi

repo_name="$1"
visibility="${2:-private}"

if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI (gh) is required. Install it and run 'gh auth login'."
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "Please authenticate first: gh auth login"
  exit 1
fi

git remote remove origin >/dev/null 2>&1 || true
git remote add upstream https://github.com/VIEWESMART/7-1024X600-ESP32-P4-C6-TOUCH-DISPLAY.git

gh repo create "$repo_name" --"$visibility" --source=. --remote=origin --push

echo "Created and pushed to origin."
echo "Upstream set to VIEWESMART source repository."
