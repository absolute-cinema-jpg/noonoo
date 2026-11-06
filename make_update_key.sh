#!/bin/bash
# Creates the key that signs NooNoo updates. Run it once, on the machine you release from.
#   private key  ~/.config/noonoo/update-signing-key.pem   passphrase protected, never commit or share it
#   public key   noonoo/update_key.pem                     ships inside the app, so commit it
# Copies of NooNoo refuse any update this key didn't sign, even one published from your GitHub account.
# Keep a backup of the private key and passphrase somewhere safe, such as a password manager. Without
# them you can't sign updates, and everyone would have to reinstall NooNoo by hand.
set -euo pipefail
cd "$(dirname "$0")"

OPENSSL=/usr/bin/openssl
PRIVATE="${NOONOO_SIGNING_KEY:-$HOME/.config/noonoo/update-signing-key.pem}"
PUBLIC=noonoo/update_key.pem

if [ -e "$PRIVATE" ]; then
  echo "$PRIVATE already exists. Replacing it would lock out every installed copy, so nothing was changed." >&2
  exit 1
fi
mkdir -p "$(dirname "$PRIVATE")"
chmod 700 "$(dirname "$PRIVATE")"
umask 077
echo "Choose a passphrase for the signing key (you'll type it for each release)."
"$OPENSSL" ecparam -name prime256v1 -genkey -noout | "$OPENSSL" ec -aes256 -out "$PRIVATE" 2>/dev/null
umask 022
"$OPENSSL" ec -in "$PRIVATE" -pubout -out "$PUBLIC" 2>/dev/null
echo "Private key: $PRIVATE"
echo "Public key:  $PUBLIC  (commit this)"
