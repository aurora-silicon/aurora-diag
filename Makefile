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
	install -d $(DESTDIR)$(PREFIX)/bin $(DESTDIR)$(PREFIX)/share/doc/aurora-diag
	install -m755 bin/aurora-diag $(DESTDIR)$(PREFIX)/bin/aurora-diag
	install -m644 README.md docs/LOG.md docs/SCHEMA.md docs/COMPLIANCE.md docs/MORNING-TESTS.md $(DESTDIR)$(PREFIX)/share/doc/aurora-diag/

.PHONY: all check install
