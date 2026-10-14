# syntax=docker/dockerfile:1
ARG NIX_IMAGE=nixos/nix:latest
FROM ${NIX_IMAGE} AS build

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
    nix-env --option filter-syscalls "$filter_syscalls" \
      --profile /nix/var/nix/profiles/runtime -f /tmp/packages.nix \
      --argstr nixpkgsRev "${NIXPKGS_REV}" \
      --argstr c2jVersion "${C2J_VERSION}" \
      --argstr c2jAmd64Sha256 "${C2J_AMD64_SHA256}" \
      --argstr c2jArm64Sha256 "${C2J_ARM64_SHA256}" \
      -iA packages && \
    mkdir -p /usr/local/bin /usr/local/share/base /workspace && \
    nix-env --profile /nix/var/nix/profiles/runtime --query --installed --json \
      > /usr/local/share/base/packages.json && \
    rm /tmp/packages.nix

COPY .build/versions.json /usr/local/share/base/versions.json
COPY scripts/smoke-test.sh /usr/local/bin/base-smoke-test
COPY nix/runtime.conf /tmp/runtime.conf
COPY scripts/assemble-runtime.sh /tmp/assemble-runtime.sh
COPY scripts/nix-cache-wrapper.py /tmp/nix-cache-wrapper.py
RUN /bin/sh /tmp/assemble-runtime.sh

FROM scratch
LABEL io.colony-2.base.nix-binary-cache="1"
COPY --from=build /runtime/ /
ENV HOME=/root \
    USER=root \
    PATH=/usr/local/bin:/root/.nix-profile/bin:/bin \
    SSL_CERT_FILE=/root/.nix-profile/etc/ssl/certs/ca-bundle.crt \
    NIX_SSL_CERT_FILE=/root/.nix-profile/etc/ssl/certs/ca-bundle.crt \
    GIT_SSL_CAINFO=/root/.nix-profile/etc/ssl/certs/ca-bundle.crt
SHELL ["/bin/sh", "-eu", "-c"]
RUN --mount=type=bind,source=scripts/test-cache.sh,target=/tmp/test-cache.sh \
    base-smoke-test && /bin/sh /tmp/test-cache.sh && \
    nix-collect-garbage > /dev/null 2>&1 && rm -rf /root/.cache

WORKDIR /workspace
CMD ["/bin/bash"]
