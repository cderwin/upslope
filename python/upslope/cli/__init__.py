from pathlib import Path


# upslope/python/upslope/cli/__init__.py -> repo root
PROJECT_BASE = Path(__file__).resolve().parents[3]
DEFAULT_WRF_CONFIG = PROJECT_BASE / "data/wrf_codes.yaml"
DEFAULT_GEOREGISTRATION_CONFIG = PROJECT_BASE / "data/georegistration/annotations.yaml"
