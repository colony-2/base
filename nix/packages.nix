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
{
  inherit (pkgs) bashInteractive nodejs_24 python3 pnpm uv;

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
