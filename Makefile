# Preview developer tasks.  `make help` lists them.
# the system Python (it has PyGObject); a conda/pyenv python3 on PATH would not
PYTHON   ?= $(shell test -x /usr/bin/python3 && echo /usr/bin/python3 || echo python3)
VENV     ?= .venv
VPY      := $(VENV)/bin/python
VERSION  := $(shell $(PYTHON) -c 'import re;print(re.search(r"__version__\s*=\s*\"([^\"]+)\"", open("preview/__init__.py").read()).group(1))')
DEB      := dist/preview-app_$(VERSION)_all.deb
IMAGE    ?= ubuntu:26.04
ARGS     ?=

.PHONY: help run selftest selftest-headless venv ocr deb docker-test install uninstall check clean

help:
	@echo "make run [ARGS='file.pdf']   run Preview from the working tree"
	@echo "make selftest                check every viewer (opens windows briefly)"
	@echo "make selftest-headless       same, without a display"
	@echo "make ocr                     add the optional OCR engine to $(VENV)"
	@echo "make deb                     build $(DEB)"
	@echo "make docker-test [IMAGE=…]   apt-install the .deb in a clean container + selftest"
	@echo "make install / uninstall     per-user install from this checkout (install.sh)"
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

deb: $(DEB)

$(DEB): $(shell find preview data -type f -not -path '*/__pycache__/*') packaging/deb/build-deb.sh
	packaging/deb/build-deb.sh

docker-test: deb
	packaging/deb/test-in-docker.sh $(IMAGE) $(DEB)

install:
	./install.sh

uninstall:
	./install.sh --uninstall

check:
	$(PYTHON) -m compileall -q preview

clean:
	rm -rf dist build $(VENV)
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
