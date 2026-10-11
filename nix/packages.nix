{
  nixpkgsRev,
  c2jVersion,
  c2jAmd64Sha256,
  c2jArm64Sha256,
}:
let
  pkgs = import (builtins.fetchTarball {
    url = "https://github.com/NixOS/nixpkgs/archive/${nixpkgsRev}.tar.gz";
  }) { };

  releases = {
    x86_64-linux = {
      arch = "x86_64";
      sha256 = c2jAmd64Sha256;
    };
    aarch64-linux = {
      arch = "arm64";
      sha256 = c2jArm64Sha256;
    };
  };
  release = releases.${pkgs.stdenv.hostPlatform.system}
    or (throw "c2j supports only x86_64-linux and aarch64-linux in this image");
in
rec {
  # Install a fresh profile so inherited base packages cannot collide with or
  # override this pinned tool set. Keep Nix itself in the runtime closure.
  packages = (with pkgs; [
    nix

    # Core utilities
    bashInteractive coreutils findutils gnugrep gnused gawk diffutils patch
    which file less tree

    # Search and navigation
    ripgrep fd fzf bat

    # Git and GitHub
    git git-lfs gh openssh

    # HTTP and networking
    curl wget cacert dnsutils iputils iproute2 netcat-openbsd

    # JSON / YAML processing
    jq yq-go

    # Archives and compression
    zip unzip gnutar gzip xz zstd

    # Build tools
    gnumake gcc pkg-config cmake

    # JavaScript / TypeScript
    nodejs_24 pnpm

    # Python
    python3 uv ruff

    # Databases
    sqlite

    # Shell development
    shellcheck shfmt entr

    # Process inspection and debugging
    procps psmisc lsof strace htop

    # File synchronization
    rsync
  ]) ++ [ c2j ];

  # c2j is packaged here from its static upstream release. Nix verifies the
  # architecture-specific hash and manages installation into the Nix store.
  c2j = pkgs.stdenvNoCC.mkDerivation {
    pname = "c2j";
    version = c2jVersion;
    src = pkgs.fetchurl {
      url = "https://github.com/colony-2/c2j/releases/download/v${c2jVersion}/c2j_${c2jVersion}_Linux_${release.arch}.tar.gz";
      inherit (release) sha256;
    };

    sourceRoot = ".";
    dontConfigure = true;
    dontBuild = true;
    installPhase = ''
      runHook preInstall
      install -Dm755 c2j "$out/bin/c2j"
      runHook postInstall
    '';
    doInstallCheck = true;
    installCheckPhase = ''
      runHook preInstallCheck
      test "$("$out/bin/c2j" version)" = "c2j version ${c2jVersion}"
      runHook postInstallCheck
    '';

    meta = {
      description = "Colony 2 job CLI";
      homepage = "https://github.com/colony-2/c2j";
      license = pkgs.lib.licenses.asl20;
      platforms = builtins.attrNames releases;
      mainProgram = "c2j";
    };
  };
}
