# SPDX-License-Identifier: MIT
PREFIX ?= /usr
DESTDIR ?=
PYTHON ?= python3
SHARE = $(DESTDIR)$(PREFIX)/share/aurora-diag

all:

check:
	$(PYTHON) -B -m unittest discover -s tests

install:
	install -d $(SHARE)/aurora_diag
	install -m644 aurora_diag/*.py $(SHARE)/aurora_diag/
	install -m644 aurora_diag/macos_baseline.json $(SHARE)/aurora_diag/
	install -d $(DESTDIR)$(PREFIX)/bin $(DESTDIR)$(PREFIX)/share/doc/aurora-diag
	install -m755 bin/aurora-diag $(DESTDIR)$(PREFIX)/bin/aurora-diag
	install -Dm644 data/org.aurorasilicon.Diag.desktop $(DESTDIR)$(PREFIX)/share/applications/org.aurorasilicon.Diag.desktop
	install -m644 README.md docs/LOG.md docs/SCHEMA.md docs/COMPLIANCE.md docs/MORNING-TESTS.md $(DESTDIR)$(PREFIX)/share/doc/aurora-diag/

# For machines that run aurora-diag while it is replaced (a fleet install
# over ssh): the new tree goes into its own release directory, is synced,
# and then replaces the old one with one rename of a symlink, so a hang or
# power loss mid-install leaves the previous version whole. Keeps the last
# three releases.
install-atomic:
	set -e; S=$(SHARE); N=releases/$$(date +%Y%m%d%H%M%S)-$$$$; R=$$S/$$N; \
	install -d $$R/aurora_diag $(DESTDIR)$(PREFIX)/bin; \
	install -m644 aurora_diag/*.py aurora_diag/macos_baseline.json $$R/aurora_diag/; \
	install -m755 bin/aurora-diag $$S/.aurora-diag.bin.new; \
	sync; \
	if [ -d $$S/aurora_diag ] && [ ! -L $$S/aurora_diag ]; then rm -rf $$S/aurora_diag.pre-atomic; mv $$S/aurora_diag $$S/aurora_diag.pre-atomic; fi; \
	ln -sfn $$N/aurora_diag $$S/.aurora_diag.new && mv -T $$S/.aurora_diag.new $$S/aurora_diag; \
	mv -f $$S/.aurora-diag.bin.new $(DESTDIR)$(PREFIX)/bin/aurora-diag; \
	sync; \
	ls -1d $$S/releases/* | head -n -3 | xargs -r rm -rf; \
	rm -rf $$S/aurora_diag.pre-atomic; \
	echo "aurora-diag installed atomically in $$R"

.PHONY: all check install install-atomic
