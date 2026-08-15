"""Distribution metadata exposed through the public package."""

from importlib.metadata import version

import agentbus


def test_public_version_uses_distribution_name() -> None:
    assert agentbus.__version__ == version("agentbus-graph")
