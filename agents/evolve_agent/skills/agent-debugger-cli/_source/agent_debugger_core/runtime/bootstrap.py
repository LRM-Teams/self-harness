from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


class BootstrapError(Exception):
    pass


def _evolve_agent_dir(candidate: Path) -> Path | None:
    for relative in (Path("agents/evolve_agent"), Path("evolve_agent")):
        evolve_dir = candidate / relative
        if (evolve_dir / "tools" / "__init__.py").exists():
            return evolve_dir
    return None


def _contains_tools_pkg(candidate: Path) -> bool:
    return _evolve_agent_dir(candidate) is not None


def ensure_tools_importable(*, _skip_self_search: bool = False) -> str:
    """Locate AHE root and prepend its evolve_agent/ dir to sys.path so
    import tools.file_tools etc. resolve. Returns the added path.

    Resolution order: AHE_HOME env var > walk up from this file > cwd.
    """
    ahe_home = os.environ.get("AHE_HOME")
    if ahe_home and _contains_tools_pkg(Path(ahe_home)):
        added = str(_evolve_agent_dir(Path(ahe_home)))
        if added not in sys.path:
            sys.path.insert(0, added)
        return added

    if not _skip_self_search:
        here = Path(__file__).resolve()
        for parent in here.parents:
            if _contains_tools_pkg(parent):
                # Skip if this file lives inside that candidate's evolve_agent
                # tree (i.e. we are a dev install inside the repo itself).
                evolve_dir = _evolve_agent_dir(parent)
                if evolve_dir is None:
                    continue
                try:
                    here.relative_to(evolve_dir)
                    # We ARE inside evolve_agent - fall through to cwd.
                    break
                except ValueError:
                    pass
                added = str(evolve_dir)
                if added not in sys.path:
                    sys.path.insert(0, added)
                return added

    cwd = Path.cwd()
    if _contains_tools_pkg(cwd):
        added = str(_evolve_agent_dir(cwd))
        if added not in sys.path:
            sys.path.insert(0, added)
        return added

    raise BootstrapError(
        "Cannot locate evolve_agent/tools. Set AHE_HOME env var or run `adb` "
        "from the AHE repo root."
    )
