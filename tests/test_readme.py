from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# PyPI serves these as the project page, where a relative link has nothing to resolve against
RELATIVE_LINK = re.compile(r"\]\((?!https?://|#)([^)]+)\)")


@pytest.mark.parametrize("readme", ["README.md", "cli/README.md"])
def test_the_published_readmes_have_no_relative_links(readme: str) -> None:
    assert RELATIVE_LINK.findall((ROOT / readme).read_text(encoding="utf-8")) == []
