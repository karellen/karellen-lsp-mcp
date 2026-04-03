# Python Integration

## Default LSP Server

[pyright](https://github.com/microsoft/pyright) via `pyright-langserver --stdio`.

Install: `pip install --user karellen-lsp-mcp[pyright]` or `pip install --user pyright`.

## Build System Detection

The `PythonDetector` checks markers in priority order:

| Marker | Build System | Confidence | Notes |
|--------|-------------|------------|-------|
| `build.py` (with pybuilder import) | pybuilder | high | Checks first lines for `from pybuilder` / `import pybuilder` |
| `pyproject.toml` | varies | high | Identifies backend from `[build-system].requires`: poetry, hatch, flit, setuptools, maturin, pdm |
| `setup.py` | setuptools | high | Dynamic config; can run `egg_info` for metadata extraction |
| `setup.cfg` | setuptools | high | Parsed via `configparser` for `python_requires`, `packages` |
| `Pipfile` | pipenv | medium | &mdash; |
| `requirements.txt` | pip | medium | &mdash; |

When `pyproject.toml` is present and a TOML parser is available (`tomllib` in
Python 3.11+, or `tomli` backport), the detector reads:
- `[build-system].requires` to identify the build backend
- `[project].requires-python` for Python version constraints
- `[tool.pyright]` presence for pyright configuration

## Virtual Environment Detection

The detector searches for virtual environments in this order:

1. `$VIRTUAL_ENV` environment variable
2. In-project directories: `.venv/`, `venv/`, `env/` — checked for `pyvenv.cfg`
   (PEP 405 marker, universal for venv/virtualenv/poetry/uv/pdm)
3. Conda environments — detected by `conda-meta/history` (no `pyvenv.cfg`)

Detected venv path and Python interpreter are forwarded to pyright via
`initializationOptions` so it uses the correct packages and type stubs.

## Detection Details

The detector populates these detail fields:

| Field | Source | Description |
|-------|--------|-------------|
| `build_system` | marker files | pybuilder, setuptools, poetry, hatch, flit, maturin, pdm, pipenv, pip |
| `build_backend` | pyproject.toml | Specific backend name from `[build-system].requires` |
| `python_requires` | pyproject.toml / setup.cfg | Python version constraint |
| `venv_path` | filesystem / `$VIRTUAL_ENV` | Path to detected virtual environment |
| `venv_python` | pyvenv.cfg | Python interpreter path inside venv |
| `venv_version` | pyvenv.cfg | Python version string |
| `is_conda` | conda-meta/history | True if conda environment |
| `src_layout` | filesystem | True if `src/` contains Python packages |
| `pyrightconfig` | filesystem | True if `pyrightconfig.json` exists |
| `has_setup_py` | filesystem | True if `setup.py` exists |
| `has_poetry` | pyproject.toml | True if `[tool.poetry]` section exists |

## Pyright Configuration

Pyright reads configuration from (in priority order):
1. `pyrightconfig.json` in the project root
2. `[tool.pyright]` section in `pyproject.toml`

For src-layout projects, you may need to set `pythonPlatform` and `venvPath`
in your pyright configuration. The adapter automatically forwards detected
venv paths, but explicit configuration takes precedence.
