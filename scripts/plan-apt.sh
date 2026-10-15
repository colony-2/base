#!/bin/sh
set -eu
export LC_ALL=C
/bin/sh /inputs/scripts/configure-apt.sh "$1"
apt-get update -o APT::Update::Error-Mode=any >&2
dpkg-query -W -f='${Package}=${Version}\n'
printf '# PLAN\n'
# Upgrade the base packages as well, so their security updates are fingerprinted.
# Inputs are package names from source control and the validated Nix version.
# shellcheck disable=SC2046
apt-get --simulate --no-install-recommends install \
    $(cat /inputs/apt/packages.txt) $(dpkg-query -W -f='${Package}\n') "nix-bin=$2"
printf '# NIX-CANDIDATE\n'
apt-cache policy nix-bin
