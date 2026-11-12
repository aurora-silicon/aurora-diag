%global commit @COMMIT@
Name: aurora-diag
Version: 0.5.0
Release: 1%{?dist}
Summary: Bring-up diagnostics for Apple silicon Macs (development only)
License: MIT
URL: https://github.com/aurora-silicon/aurora-diag
Source0: aurora-diag-%{commit}.tar.gz
BuildArch: noarch
BuildRequires: make python3
Requires: python3
Recommends: alsa-utils
Recommends: python3-textual
Recommends: vulkan-tools
Recommends: mesa-demos
Recommends: iw
Recommends: smartmontools
Recommends: bluez

%description
Shows every sensor, clock, hardware block, service and GPU API (Vulkan,
OpenGL, GLES) on Apple silicon Macs, says what works and what does not,
changes what may be changed with a journal and automatic restore, and
gives agents a fast pass/warn/fail check plus snapshots and diffs to
confirm a new kernel did not regress. Development and bring-up tool: not
installed by default.

%prep
%autosetup -n aurora-diag-%{commit}

%build

%check
make check

%install
make install DESTDIR=%{buildroot} PREFIX=%{_prefix}

%files
%license LICENSE
%doc %{_docdir}/aurora-diag
%{_bindir}/aurora-diag
%{_bindir}/aurora-panic-log
%{_datadir}/aurora-diag
%{_datadir}/applications/org.aurorasilicon.Diag.desktop

%changelog
* Thu Nov 12 2026 Aurora Silicon <ryan@aurorasilicon.org> - 0.5.0-1
- status: what works on one page (kernel, fleet tag, boot chain, firmware,
  each block reported vs working, GPU APIs, services).
- report: host and user names, MAC/IP addresses, serial, Wi-Fi names and
  UUIDs taken out; files named after the board, mode 0600.
- Audio category, services tab and summary report speakerguardd v2.
- aurora-panic-log and dj-check (since 0.4.0).

* Sun Nov 01 2026 Aurora Silicon <ryan@aurorasilicon.org> - 0.4.0-1
- Demos home: motion (spirit level, horizon, compass), speaker protection,
  AVE/AVD benches vs macOS, ANE classify, GPU, aurora-try camera, captions,
  mic denoise, photos, upscale; `aurora-diag demo`.

* Sun Nov 01 2026 Aurora Silicon <ryan@aurorasilicon.org> - 0.3.0-1
- Thirteen categories with live drill-downs: health, thermal, CPU, GPU,
  memory, display, radio, storage, power, audio, engines, sensors, system.
- Display: KMS properties, EDID, DCP attributes, VRR, HDR, DCP per connector.
- Motion sensors, lid and jack switches; absent items say why.
- show and coverage commands; inventory in check, snapshot and diff.

* Fri Oct 30 2026 Aurora Silicon <ryan@aurorasilicon.org> - 0.2.0-1
- GPU, Vulkan, GL/GLES, compliance, clocks, subsystems, services.
- check, snapshot, diff and report for agents; Textual dashboard.

* Fri Oct 30 2026 Aurora Silicon <ryan@aurorasilicon.org> - 0.1.0-1
- Initial package.
