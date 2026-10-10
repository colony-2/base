# syntax=docker/dockerfile:1
ARG NIX_IMAGE=nixos/nix:latest
FROM ${NIX_IMAGE}

SHELL ["/bin/sh", "-eu", "-c"]
ENV PATH="/usr/local/bin:${PATH}" \
    NIX_CONFIG="experimental-features = nix-command flakes"

# Install every tool through one pinned Nix package set.
ARG NIXPKGS_REV
ARG C2J_VERSION
ARG C2J_AMD64_SHA256
ARG C2J_ARM64_SHA256
ARG BUILDPLATFORM
ARG TARGETPLATFORM
COPY nix/packages.nix /tmp/packages.nix
# QEMU cannot load Nix's seccomp filter for a different host architecture.
# Apply this exception only to installation during an emulated build.
RUN filter_syscalls=true; \
    if [ "${BUILDPLATFORM}" != "${TARGETPLATFORM}" ]; then filter_syscalls=false; fi; \
    nix-env --option filter-syscalls "$filter_syscalls" -f /tmp/packages.nix \
      --argstr nixpkgsRev "${NIXPKGS_REV}" \
      --argstr c2jVersion "${C2J_VERSION}" \
      --argstr c2jAmd64Sha256 "${C2J_AMD64_SHA256}" \
      --argstr c2jArm64Sha256 "${C2J_ARM64_SHA256}" \
      -iA bashInteractive nodejs_24 python3 pnpm uv c2j && \
    mkdir -p /usr/local/bin /usr/local/share/base /workspace && \
    ln -sf "$(command -v bash)" /bin/bash && \
    nix-env --query --installed --json > /usr/local/share/base/packages.json && \
    rm /tmp/packages.nix

COPY .build/versions.json /usr/local/share/base/versions.json
COPY scripts/smoke-test.sh /usr/local/bin/base-smoke-test
RUN chmod +x /usr/local/bin/base-smoke-test && base-smoke-test

WORKDIR /workspace
CMD ["/bin/bash"]
