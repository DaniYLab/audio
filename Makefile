.PHONY: install install-dev lint format typecheck test run clean

install:
	python -m pip install --upgrade pip
	python -m pip install -e .

install-dev:
	python -m pip install --upgrade pip
	python -m pip install -e ".[dev,transcription,knowledge,tts]"

lint:
	python -m ruff check src tests
	python -m ruff format --check src tests

format:
	python -m ruff format src tests
	python -m ruff check --fix src tests

typecheck:
	python -m mypy src

test:
	python -m pytest

# Full production run for one project (see README for CLI details).
run:
	python -m storyforge.cli run --project demo --source-config config/story_config.example.yaml

clean:
	rm -rf .ruff_cache .mypy_cache .pytest_cache dist build src/*.egg-info
