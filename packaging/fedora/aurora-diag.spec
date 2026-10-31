%global commit @COMMIT@
Name: aurora-diag
Version: 0.2.0
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
%{_datadir}/aurora-diag
%{_datadir}/applications/org.aurorasilicon.Diag.desktop

%changelog
* Fri Oct 30 2026 Aurora Silicon <ryan@aurorasilicon.org> - 0.2.0-1
- GPU, Vulkan, GL/GLES, compliance, clocks, subsystems, services.
- check, snapshot, diff and report for agents; Textual dashboard.

* Fri Oct 30 2026 Aurora Silicon <ryan@aurorasilicon.org> - 0.1.0-1
- Initial package.
