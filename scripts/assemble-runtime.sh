#!/bin/sh
set -eu

# Copy only the installed profile's runtime closure into a new, registered store.
# nix copy omits build inputs, old profiles, the base-system/channel root, and caches.
profile=$(readlink -f /nix/var/nix/profiles/runtime)
# The trusted build stage contains locally built, unsigned outputs (such as c2j).
# Disable signature checks only for this local transfer, never for runtime caches.
nix copy --no-check-sigs --to 'local?root=/runtime' "$profile"

mkdir -p /runtime/bin /runtime/usr/bin /runtime/root \
    /runtime/etc/nix /runtime/etc/ssl/certs \
    /runtime/usr/local/bin /runtime/usr/local/libexec \
    /runtime/usr/local/share/base /runtime/workspace /runtime/tmp \
    /runtime/nix/var/nix/profiles /runtime/nix/var/nix/gcroots
chmod 1777 /runtime/tmp
ln -s "$profile" /runtime/nix/var/nix/profiles/default-1-link
ln -s default-1-link /runtime/nix/var/nix/profiles/default
ln -s /nix/var/nix/profiles/default /runtime/root/.nix-profile
ln -s /root/.nix-profile/bin/bash /runtime/bin/bash
ln -s bash /runtime/bin/sh
ln -s /root/.nix-profile/bin/env /runtime/usr/bin/env
ln -s /root/.nix-profile/etc/ssl/certs/ca-bundle.crt /runtime/etc/ssl/certs/ca-certificates.crt
printf 'root:x:0:0:root:/root:/bin/bash\nnobody:x:65534:65534:nobody:/var/empty:/bin/false\n' > /runtime/etc/passwd
printf 'root:x:0:\nnobody:x:65534:\n' > /runtime/etc/group
printf 'hosts: files dns\n' > /runtime/etc/nsswitch.conf
cp /tmp/runtime.conf /runtime/etc/nix/nix.conf
cp /usr/local/share/base/*.json /runtime/usr/local/share/base/
cp /usr/local/bin/base-smoke-test /runtime/usr/local/bin/
cat > /runtime/usr/local/libexec/nix-deny-build <<'EOF'
#!/bin/sh
echo 'This image only supports prebuilt Nix packages from binary caches; local builds are disabled.' >&2
exit 1
EOF
chmod +x /runtime/usr/local/bin/base-smoke-test /runtime/usr/local/libexec/nix-deny-build

# Resolve immutable executable paths now. Internal wrapper operations never
# consult PATH, while ordinary nested user commands keep using these wrappers.
mkdir -p /runtime/usr/local/libexec/nix-real
python=$(readlink -f "$profile/bin/python3")
printf '#!%s -I\n' "$python" > /runtime/usr/local/libexec/nix-cache-wrapper
tail -n +2 /tmp/nix-cache-wrapper.py >> /runtime/usr/local/libexec/nix-cache-wrapper
chmod +x /runtime/usr/local/libexec/nix-cache-wrapper
for command in nix nix-env nix-store nix-build nix-shell \
    nix-channel nix-copy-closure nix-instantiate nix-prefetch-url; do
    ln -s "$(readlink -f "$profile/bin/$command")" "/runtime/usr/local/libexec/nix-real/$command"
    ln -s /usr/local/libexec/nix-cache-wrapper "/runtime/usr/local/bin/$command"
done
printf '{"nixBinaryCache":{"version":1,"rootEnv":"COLONY_NIX_CACHE_DIR","layout":"file-binary-cache-v1"}}\n' \
    > /runtime/usr/local/share/base/capabilities.json
