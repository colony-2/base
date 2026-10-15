#!/bin/sh
set -eu
snapshot=$1
case "$snapshot" in
    ????????T??????Z) ;;
    *) echo 'Expected an Ubuntu snapshot timestamp' >&2; exit 1 ;;
esac
# Bootstrap TLS from the resolved Mozilla CA bundle; Ubuntu's ca-certificates
# package replaces it during installation. APT additionally authenticates the
# archive using Ubuntu's bundled signing keyring.
mkdir -p /etc/ssl/certs
if [ ! -s /etc/ssl/certs/ca-certificates.crt ]; then
    cp /inputs/.build/apt-ca.pem /etc/ssl/certs/ca-certificates.crt
fi
cat > /etc/apt/sources.list.d/ubuntu.sources <<EOF
Types: deb
URIs: https://snapshot.ubuntu.com/ubuntu/$snapshot/
Suites: resolute resolute-updates resolute-security
Components: main universe restricted multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
Check-Valid-Until: no
EOF
# Phasing must not make the selected closure depend on a container machine ID.
printf 'APT::Get::Always-Include-Phased-Updates "true";\nAcquire::Retries "3";\nAcquire::https::CaInfo "/etc/ssl/certs/ca-certificates.crt";\n' > /etc/apt/apt.conf.d/99base
