PYTHON = python
KEEPASS_DB ?=

.PHONY: install preprocess train test start

install:
	$(PYTHON) -m pip install -r requirements.txt

preprocess:
	$(PYTHON) -m src.data_preprocessing

train:
	$(PYTHON) -m src.train

test:
	$(PYTHON) -m pytest tests -v

start:
	$(PYTHON) -m scripts.deploy_release --resume --keepass-db "$(KEEPASS_DB)"
