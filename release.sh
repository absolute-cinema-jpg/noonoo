#!/bin/bash
# Publishes a new version of NooNoo. Everyone's copy finds it through the public update feed and
# offers a one-click update.
#   ./release.sh 1.0.3              release notes = commit messages since the last release
#   ./release.sh 1.0.3 notes.md     ...or your own notes (Markdown)
# Steps: bump the version, build Apple Silicon + Intel apps, sign them, commit + tag + push, then publish
# the release to this repo and (if it's a different one) to the public feed repo named in noonoo/updater.py.
# Copies of NooNoo only install updates signed with the key from ./make_update_key.sh.
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

OPENSSL=/usr/bin/openssl
SIGNING_KEY="${NOONOO_SIGNING_KEY:-$HOME/.config/noonoo/update-signing-key.pem}"
PUBLIC_KEY=noonoo/update_key.pem
if [ ! -f "$SIGNING_KEY" ] || ! git ls-files --error-unmatch "$PUBLIC_KEY" >/dev/null 2>&1; then
  echo "No update key. Run ./make_update_key.sh and commit $PUBLIC_KEY first." >&2
  exit 1
fi
# The key's passphrase: from the Keychain once it's been saved there, otherwise asked for (and offered
# a place in the Keychain). The key file itself stays encrypted either way.
KEYCHAIN_ITEM="NooNoo update signing key"
FROM_KEYCHAIN=0
if [ -z "${NOONOO_SIGN_PASS:-}" ] && NOONOO_SIGN_PASS=$(security find-generic-password -s "$KEYCHAIN_ITEM" -a "$USER" -w 2>/dev/null); then
  FROM_KEYCHAIN=1
else
  read -r -s -p "Passphrase for the update signing key: " NOONOO_SIGN_PASS; echo
fi
export NOONOO_SIGN_PASS
# Catch a wrong key or passphrase now, before anything is built or bumped.
if ! "$OPENSSL" ec -in "$SIGNING_KEY" -passin env:NOONOO_SIGN_PASS -pubout 2>/dev/null | cmp -s - "$PUBLIC_KEY"; then
  if [ "$FROM_KEYCHAIN" = 1 ]; then
    security delete-generic-password -s "$KEYCHAIN_ITEM" -a "$USER" >/dev/null 2>&1 || true
    echo "The passphrase saved in your Keychain doesn't open the key, so it's been removed. Run this again to type it." >&2
  else
    echo "The passphrase is wrong, or that key doesn't match $PUBLIC_KEY." >&2
  fi
  exit 1
fi
if [ "$FROM_KEYCHAIN" = 0 ]; then
  read -r -p "Save the passphrase in your Keychain, so releases stop asking? [Y/n] " answer
  if [ "${answer:-y}" != "n" ] && [ "${answer:-y}" != "N" ]; then
    # through stdin rather than the command line, so it never shows up in the process list
    esc=$(printf '%s' "$NOONOO_SIGN_PASS" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
    printf 'add-generic-password -U -s "%s" -a "%s" -l "%s" -j "%s" -w "%s"\n' "$KEYCHAIN_ITEM" "$USER" "$KEYCHAIN_ITEM" \
      "Passphrase for ~/.config/noonoo/update-signing-key.pem, used by NooNoo's release.sh" "$esc" | security -i >/dev/null \
      && echo "Saved. To stop saving it: security delete-generic-password -s \"$KEYCHAIN_ITEM\"" \
      || echo "Couldn't save it to the Keychain; releases will keep asking." >&2
    unset esc
  fi
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
ASSETS=()
for zip in "${ZIPS[@]}"; do
  "$OPENSSL" dgst -sha256 -sign "$SIGNING_KEY" -passin env:NOONOO_SIGN_PASS -out "$zip.sig" "$zip"
  "$OPENSSL" dgst -sha256 -verify "$PUBLIC_KEY" -signature "$zip.sig" "$zip" >/dev/null
  ASSETS+=("$zip" "$zip.sig")
done
unset NOONOO_SIGN_PASS

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

gh release create "v$VERSION" "${ASSETS[@]}" --repo "$SOURCE" --title "NooNoo $VERSION" --notes-file "$NOTES" --latest
if [ "$FEED" != "$SOURCE" ]; then
  gh release create "v$VERSION" "${ASSETS[@]}" --repo "$FEED" --title "NooNoo $VERSION" --notes-file "$NOTES" --latest
fi
echo "Published NooNoo $VERSION - copies of NooNoo will offer the update within a few hours (or on next launch)."
