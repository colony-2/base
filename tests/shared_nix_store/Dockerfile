ARG UBUNTU_IMAGE=ubuntu:26.04@sha256:f144425ff09be612d6d9ad965196e9cdc23dae1f42110a8a11a3e9a8198759f7
FROM ${UBUNTU_IMAGE}
ARG NIX_PACKAGE_VERSION=2.34.3+dfsg-1
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      nix-bin="${NIX_PACKAGE_VERSION}" ca-certificates python3 strace && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /nix /etc/nix /usr/local/libexec /root/.local/state/nix/profiles && \
    ln -s profiles/profile /root/.local/state/nix/profile && \
    ln -s /root/.local/state/nix/profile /root/.nix-profile
COPY nix.conf /etc/nix/nix.conf
COPY deny-build /usr/local/libexec/nix-deny-build
COPY preflight.py worker.py server.py /probe/
RUN chmod +x /usr/local/libexec/nix-deny-build && test -z "$(ls -A /nix)"
ENV NIX_REMOTE=local \
    NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/root/.local/state/nix/profile/bin
CMD ["/bin/bash"]
