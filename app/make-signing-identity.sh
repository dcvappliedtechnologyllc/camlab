#!/bin/zsh
# One-time: create the self-signed "camlab dev" code-signing identity in the login keychain.
# app/build.sh signs dist/camlab.app with it so macOS privacy (TCC) grants such as Bluetooth
# survive rebuilds: TCC keys a grant on the app's designated requirement, which for an ad-hoc
# signature is the per-build code hash (a new app every build) but for a certificate-signed app is
# "identifier com.dcv.camlab and certificate leaf = <this cert>" (stable across builds).
#
#   ./app/make-signing-identity.sh          create (no-op if it already exists)
#   ./app/make-signing-identity.sh --force  replace an existing one (the Bluetooth grant is asked once more)
#
# Up to two GUI prompts may appear the first time: one to add the certificate to your trust settings,
# and one from codesign asking to use the key (click "Always Allow"). Nothing leaves this Mac.
set -euo pipefail

NAME="camlab dev"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

if security find-identity -v -p codesigning "$KEYCHAIN" 2>/dev/null | grep -q "\"$NAME\""; then
  if [[ "${1:-}" == "--force" ]]; then
    echo "removing existing \"$NAME\" identity"
    security delete-identity -c "$NAME" "$KEYCHAIN" || true
  else
    echo "identity \"$NAME\" already present:"; security find-identity -v -p codesigning "$KEYCHAIN" | grep "\"$NAME\""
    exit 0
  fi
fi

# 1. key + self-signed certificate with the Code Signing extended key usage (10 years)
cat > "$TMP/ext.cnf" <<CNF
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = $NAME
[v3]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
subjectKeyIdentifier = hash
CNF
openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
  -keyout "$TMP/key.pem" -out "$TMP/cert.pem" -config "$TMP/ext.cnf" 2>/dev/null
# legacy PKCS12 (RC2/3DES) is what `security import` accepts across macOS versions
openssl pkcs12 -export -legacy -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
  -name "$NAME" -passout pass:camlab -out "$TMP/id.p12" 2>/dev/null \
  || openssl pkcs12 -export -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
       -name "$NAME" -passout pass:camlab -out "$TMP/id.p12"

# 2. into the login keychain; -T lets codesign use the key without a per-build prompt
security import "$TMP/id.p12" -k "$KEYCHAIN" -P camlab -f pkcs12 \
  -T /usr/bin/codesign -T /usr/bin/security -T /usr/bin/productbuild >/dev/null
# let Apple's signing tools past the key's partition list (may ask for the login password once)
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "" "$KEYCHAIN" >/dev/null 2>&1 \
  || echo "note: could not pre-authorize codesign for the key; click \"Always Allow\" on the first codesign prompt"

# 3. trust it for code signing (user trust settings; may prompt once)
security add-trusted-cert -p codeSign -k "$KEYCHAIN" "$TMP/cert.pem"

echo "created:"; security find-identity -v -p codesigning "$KEYCHAIN" | grep "\"$NAME\""
echo "SHA-1: $(openssl x509 -in "$TMP/cert.pem" -noout -fingerprint -sha1 | cut -d= -f2)"
