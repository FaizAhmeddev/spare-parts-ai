.PHONY: install run serve test validate docs docker
install:  ; pip install -e ".[dev]"
validate: ; python -m spareai validate
run:      ; python -m spareai run
serve:    ; python -m spareai serve
test:     ; pytest -q
docs:     ; python scripts/generate_data_contract.py
docker:   ; docker compose up --build
