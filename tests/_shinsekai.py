"""Load this checkout against an installed Shinsekai SDK and world plugin."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys


STORY_CODE = Path(__file__).resolve().parents[1]
PROJECT = Path(os.environ.get("SHINSEKAI_TEST_ROOT") or STORY_CODE.parents[1]).expanduser().resolve()
WORLD_CODE = PROJECT / "plugins" / "nanami_world"

if not (PROJECT / "sdk" / "register.py").is_file() or not (WORLD_CODE / "plugin.py").is_file():
    raise RuntimeError("Set SHINSEKAI_TEST_ROOT to a Shinsekai installation with the nanami_world plugin.")

sys.path.insert(0, str(PROJECT))
import plugins

spec = importlib.util.spec_from_file_location(
    "plugins.nanami_story", STORY_CODE / "__init__.py", submodule_search_locations=[str(STORY_CODE)],
)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Could not load the nanami_story checkout at {STORY_CODE}.")

module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
plugins.nanami_story = module
spec.loader.exec_module(module)
