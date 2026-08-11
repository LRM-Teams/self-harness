"""E2B environment namespace dedicated to the independent Pi experiment."""

from __future__ import annotations

import re
from typing import Any

from harbor.environments.e2b import E2BEnvironment


class PiE2BEnvironment(E2BEnvironment):
    """Use prefixed E2B aliases so Pi images never replace NexAU templates."""

    def __init__(
        self,
        *args: Any,
        template_prefix: str = "ahe-pi-v1",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", template_prefix).strip("-")
        safe_name = self.environment_name.replace(".", "-")
        self._template_name = f"{safe_prefix}-{safe_name}"
