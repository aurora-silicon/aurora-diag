# Overlay entry for nixos-aurora (overlay.nix), next to the other pinned
# packages. Opt-in: add pkgs.aurora-diag to environment.systemPackages.
{ stdenvNoCC, python3, makeWrapper, alsa-utils, glib, lib, src, version }:
stdenvNoCC.mkDerivation {
  pname = "aurora-diag";
  inherit version src;
  nativeBuildInputs = [ makeWrapper python3 ];
  buildInputs = [ python3 ];
  dontBuild = true;
  doCheck = true;
  checkPhase = "make check PYTHON=${python3.interpreter}";
  installPhase = ''
    make install DESTDIR=$out PREFIX=
    patchShebangs $out/bin
    wrapProgram $out/bin/aurora-diag --prefix PATH : ${lib.makeBinPath [ alsa-utils glib ]}
  '';
  meta = {
    description = "Bring-up diagnostics for Apple silicon Macs (development only)";
    license = lib.licenses.mit;
    mainProgram = "aurora-diag";
  };
}
