# syntax=docker/dockerfile:1
ARG UBUNTU_IMAGE=ubuntu:26.04
FROM ${UBUNTU_IMAGE} AS runtime
ARG APT_SNAPSHOT
ARG TARGETARCH
SHELL ["/bin/sh", "-eu", "-c"]
# Resolve first with scripts/resolve-versions.py. Install the exact complete
# closure from one signed Ubuntu snapshot; retain a usable APT/dpkg installation.
RUN --mount=type=bind,source=scripts/configure-apt.sh,target=/inputs/scripts/configure-apt.sh \
    --mount=type=bind,source=scripts/verify-apt.py,target=/inputs/scripts/verify-apt.py \
    --mount=type=bind,source=.build/apt/${TARGETARCH}.txt,target=/inputs/apt.txt \
    --mount=type=bind,source=.build/apt-ca.pem,target=/inputs/.build/apt-ca.pem \
    /bin/sh /inputs/scripts/configure-apt.sh "${APT_SNAPSHOT}" && \
    apt-get update -o APT::Update::Error-Mode=any && \
    DEBIAN_FRONTEND=noninteractive xargs -a /inputs/apt.txt \
      apt-get install -y --no-install-recommends --no-remove && \
    python3 /inputs/scripts/verify-apt.py /inputs/apt.txt && \
    apt-mark hold nix-bin && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# Install directly into final-image layers so the inline metadata in the
# published image can reuse them on a fresh builder. Stable, narrow inputs keep
# reporting timestamps and unrelated architecture changes out of these keys.
RUN --mount=type=bind,source=scripts/install-tools.py,target=/tmp/install-tools.py \
    --mount=type=bind,source=.build/tools/${TARGETARCH}/node.json,target=/tmp/tool.json \
    python3 /tmp/install-tools.py /tmp/tool.json "${TARGETARCH}"

RUN --mount=type=bind,source=scripts/install-tools.py,target=/tmp/install-tools.py \
    --mount=type=bind,source=.build/tools/${TARGETARCH}/pnpm.json,target=/tmp/tool.json \
    python3 /tmp/install-tools.py /tmp/tool.json "${TARGETARCH}"

RUN --mount=type=bind,source=scripts/install-tools.py,target=/tmp/install-tools.py \
    --mount=type=bind,source=.build/tools/${TARGETARCH}/yq.json,target=/tmp/tool.json \
    python3 /tmp/install-tools.py /tmp/tool.json "${TARGETARCH}"

RUN --mount=type=bind,source=scripts/install-tools.py,target=/tmp/install-tools.py \
    --mount=type=bind,source=.build/tools/${TARGETARCH}/uv.json,target=/tmp/tool.json \
    python3 /tmp/install-tools.py /tmp/tool.json "${TARGETARCH}"

RUN --mount=type=bind,source=scripts/install-tools.py,target=/tmp/install-tools.py \
    --mount=type=bind,source=.build/tools/${TARGETARCH}/ruff.json,target=/tmp/tool.json \
    python3 /tmp/install-tools.py /tmp/tool.json "${TARGETARCH}"

RUN --mount=type=bind,source=scripts/install-tools.py,target=/tmp/install-tools.py \
    --mount=type=bind,source=.build/tools/${TARGETARCH}/c2j.json,target=/tmp/tool.json \
    python3 /tmp/install-tools.py /tmp/tool.json "${TARGETARCH}"

COPY nix/runtime.conf /etc/nix/nix.conf
COPY nix/registry.json /etc/nix/registry.json
COPY --chmod=755 scripts/nix-deny-build /usr/local/libexec/nix-deny-build
COPY --chmod=755 scripts/smoke-test.sh /usr/local/bin/base-smoke-test
COPY --chmod=755 scripts/store-preflight.py /usr/local/bin/base-store-preflight
COPY .build/versions.json /usr/local/share/base/versions.json
RUN mkdir -p /nix /workspace /root/.local/state/nix/profiles && \
    ln -s profiles/profile /root/.local/state/nix/profile && \
    ln -s /root/.local/state/nix/profile /root/.nix-profile && \
    ln -s /usr/bin/fdfind /usr/local/bin/fd && \
    ln -s /usr/bin/batcat /usr/local/bin/bat && \
    python3 -c 'import json, pathlib, subprocess; p=pathlib.Path("/usr/local/share/base/versions.json"); v=json.loads(p.read_text()); v["platform"]="linux/"+subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip(); v["registry"]=json.loads(pathlib.Path("/etc/nix/registry.json").read_text()); p.write_text(json.dumps(v, indent=2)+"\n")' && \
    test -z "$(ls -A /nix)"
ENV HOME=/root \
    USER=root \
    NIX_REMOTE=local \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/root/.local/state/nix/profile/bin \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt
LABEL io.colony-2.base.nix-shared-store="1" \
      io.colony-2.base.nix-store-compat="colony-nix-store-v1" \
      io.colony-2.base.nix-store-initialization="provider" \
      org.opencontainers.image.title="Colony 2 base2" \
      org.opencontainers.image.description="Ubuntu development tools with an empty shared Nix store" \
      org.opencontainers.image.version="shared-nix-v1"
WORKDIR /workspace
CMD ["/bin/bash"]
