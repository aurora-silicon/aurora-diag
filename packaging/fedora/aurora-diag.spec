%global commit @COMMIT@
Name: aurora-diag
Version: 0.1.0
Release: 1%{?dist}
Summary: Bring-up diagnostics for Apple silicon Macs (development only)
License: MIT
URL: https://github.com/aurora-silicon/aurora-diag
Source0: aurora-diag-%{commit}.tar.gz
BuildArch: noarch
BuildRequires: make python3
Requires: python3
Recommends: alsa-utils

%description
Shows every sensor and setting the kernel exposes on Apple silicon Macs
(temperatures, power rails, fans, lid angle, ambient light, speaker
protection state, displays, Type-C and more), changes what may be changed
with a journal and automatic restore, and logs everything as JSON lines.
Development and bring-up tool: not installed by default.

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

%changelog
* Fri Oct 30 2026 Aurora Silicon <ryan@aurorasilicon.org> - 0.1.0-1
- Initial package.
