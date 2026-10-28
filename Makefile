# View developer tasks.  `make help` lists them.
# the system Python (it has PyGObject); a conda/pyenv python3 on PATH would not
PYTHON   ?= $(shell test -x /usr/bin/python3 && echo /usr/bin/python3 || echo python3)
VENV     ?= .venv
VPY      := $(VENV)/bin/python
VERSION  := $(shell $(PYTHON) -c 'import re;print(re.search(r"__version__\s*=\s*\"([^\"]+)\"", open("preview/__init__.py").read()).group(1))')
DEB      := dist/view_$(VERSION)_all.deb
IMAGE    ?= ubuntu:26.04
ARGS     ?=

.PHONY: help run selftest selftest-headless venv ocr translate deb docker-test install uninstall purge check clean i18n

help:
	@echo "make run [ARGS='file.pdf']   run View from the working tree"
	@echo "make selftest                check every viewer (opens windows briefly)"
	@echo "make selftest-headless       same, without a display"
	@echo "make i18n                    refresh and compile the UI translations (po/)"
	@echo "make ocr                     add the optional OCR engine to $(VENV)"
	@echo "make translate               add the optional translation engine to $(VENV)"
	@echo "make deb                     build $(DEB)"
	@echo "make docker-test [IMAGE=…]   apt-install the .deb in a clean container + selftest"
	@echo "make install / uninstall     per-user \"View (Dev)\" install from this checkout (install.sh)"
	@echo "make purge                   uninstall and delete its venv"
	@echo "make check                   byte-compile everything"
	@echo "make clean"

$(VENV)/.stamp: requirements.txt
	$(PYTHON) -m venv --system-site-packages $(VENV)
	$(VPY) -m pip install -q -r requirements.txt
	@touch $@

venv: $(VENV)/.stamp

run: venv
	PYTHONPATH=$(CURDIR) $(VPY) -m preview $(ARGS)

selftest: venv
	PYTHONPATH=$(CURDIR) $(VPY) -m preview --selftest $(ARGS)

selftest-headless: venv
	PYTHONPATH=$(CURDIR) $(VPY) -m preview --selftest --no-ui $(ARGS)

ocr: venv
	$(VPY) -m pip install -r requirements-ocr.txt

translate: venv
	$(VPY) -m pip install -r requirements-translate.txt

deb: $(DEB)

$(DEB): $(shell find preview data -type f -not -path '*/__pycache__/*') packaging/deb/build-deb.sh
	packaging/deb/build-deb.sh

docker-test: deb
	packaging/deb/test-in-docker.sh $(IMAGE) $(DEB)

install:
	./install.sh

uninstall:
	./install.sh --uninstall

purge:
	./install.sh --purge

check:
	$(PYTHON) -m compileall -q preview

# UI translations: refresh po/view.pot from the sources, merge it into every
# po/*.po, and compile them into preview/locale/<lang>/LC_MESSAGES/view.mo.
i18n:
	xgettext --language=Python --from-code=UTF-8 --keyword=_ --keyword=N_ --keyword=ngettext:1,2 \
		--package-name=View --add-comments=TRANSLATORS --no-location -o po/view.pot preview/*.py
	@for po in po/*.po; do \
		lang=$$(basename $$po .po); \
		msgmerge --quiet --update --backup=none --no-location $$po po/view.pot; \
		mkdir -p preview/locale/$$lang/LC_MESSAGES; \
		msgfmt --check -o preview/locale/$$lang/LC_MESSAGES/view.mo $$po; \
	done

clean:
	rm -rf dist build $(VENV)
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
