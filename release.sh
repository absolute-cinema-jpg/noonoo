#!/bin/bash
# Publishes a new version of NooNoo. Everyone's copy finds it through the public update feed and
# offers a one-click update.
#   ./release.sh 1.0.3              release notes = commit messages since the last release
#   ./release.sh 1.0.3 notes.md     ...or your own notes (Markdown)
# Steps: bump the version, build Apple Silicon + Intel apps, commit + tag + push, then publish the
# release to this repo and (if it's a different one) to the public feed repo named in noonoo/updater.py.
set -euo pipefail
cd "$(dirname "$0")"

VERSION="${1:-}"
NOTES="${2:-}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "usage: ./release.sh X.Y.Z [notes.md]" >&2; exit 2; }
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "You have uncommitted changes. Commit (or stash) them first, so the release matches the code." >&2
  exit 1
fi
if git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null; then
  echo "v$VERSION already exists." >&2
  exit 1
fi
FEED=$(python3 -c "import re; print(re.search(r'FEED_REPO = \"([^\"]+)\"', open('noonoo/updater.py').read()).group(1))")
SOURCE=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
PREVIOUS=$(git describe --tags --abbrev=0 2>/dev/null || true)

# A separate feed repo (if FEED_REPO isn't this repo) holds only downloads + notes.
if [ "$FEED" != "$SOURCE" ] && ! gh repo view "$FEED" >/dev/null 2>&1; then
  read -r -p "Create the public update repo $FEED (downloads and release notes only)? [y/N] " answer
  [ "$answer" = "y" ] || { echo "Stopped - nothing was published." >&2; exit 1; }
  gh repo create "$FEED" --public --description "Downloads and updates for NooNoo"
  printf '# NooNoo downloads\n\nGet the latest version from [Releases](../../releases/latest). NooNoo updates itself from here.\n' \
    | base64 | gh api -X PUT "repos/$FEED/contents/README.md" -f message="Add README" -F content=@- >/dev/null
fi

echo "Releasing NooNoo $VERSION (previous: ${PREVIOUS:-none})"
BUILD_NUMBER=$(( $(/usr/libexec/PlistBuddy -c "Print CFBundleVersion" app/Info.plist) + 1 ))
/usr/libexec/PlistBuddy -c "Set CFBundleShortVersionString $VERSION" -c "Set CFBundleVersion $BUILD_NUMBER" app/Info.plist
./build_app.sh --arch=arm64 --no-install --zip
./build_app.sh --arch=x86_64 --no-install --zip
ZIPS=("build/NooNoo-$VERSION-macOS-Apple-Silicon.zip" "build/NooNoo-$VERSION-macOS-Intel.zip")

if [ -z "$NOTES" ]; then
  NOTES="build/notes-$VERSION.md"
  {
    echo "### What's new"
    git log --format='- %s' ${PREVIOUS:+"$PREVIOUS"..}HEAD | grep -v '^- Release [0-9]' || echo "- Fixes and improvements"
    echo
    echo "Already using NooNoo? It will offer this update itself (or use NooNoo › Check for Updates)."
    echo "New here? Download the Apple Silicon or Intel zip below and drag NooNoo.app into Applications."
  } > "$NOTES"
fi

if ! git diff --quiet app/Info.plist; then
  git commit -q -m "Release $VERSION" app/Info.plist
fi
git tag -a "v$VERSION" -m "NooNoo $VERSION"
git push -q origin HEAD "v$VERSION"

gh release create "v$VERSION" "${ZIPS[@]}" --repo "$SOURCE" --title "NooNoo $VERSION" --notes-file "$NOTES" --latest
if [ "$FEED" != "$SOURCE" ]; then
  gh release create "v$VERSION" "${ZIPS[@]}" --repo "$FEED" --title "NooNoo $VERSION" --notes-file "$NOTES" --latest
fi
echo "Published NooNoo $VERSION - copies of NooNoo will offer the update within a few hours (or on next launch)."
