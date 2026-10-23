.PHONY: install test smoke contract parallel drift report clean

install:
	pip install -e ".[dev]"

test:
	pytest -q

smoke:
	pytest -m "smoke and not destructive" -q

contract:
	pytest -m contract -q

parallel:
	pytest -q -n auto --dist loadscope

# 演示：上游字段类型漂移时，契约层应当变红
drift:
	pytest -m contract --schema-drift -q

report:
	pytest --html=out/report.html --self-contained-html

clean:
	rm -rf out .pytest_cache
