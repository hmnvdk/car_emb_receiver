#!/bin/bash
# Generate self-signed certificate for Android Auto head unit
# NOTE: Real Android phones (Android 10+) may reject self-signed certs.
#       For testing use Android 9 or lower, or a rooted device.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ -f "hu.crt" ] && [ -f "hu.key" ]; then
    echo "Certificates already exist, skipping generation."
    exit 0
fi

openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 \
    -nodes \
    -keyout hu.key \
    -out hu.crt \
    -subj "/C=US/ST=CA/L=Mountain View/O=Android/OU=Head Unit/CN=AndroidAuto" \
    -extensions v3_ca \
    -addext "subjectAltName=DNS:androidauto,IP:127.0.0.1" \
    2>/dev/null

echo "Generated hu.key and hu.crt"
