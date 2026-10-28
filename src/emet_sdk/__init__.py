"""Emet SDK: sends your AI agent's turns to Emet from the OpenTelemetry GenAI spans it already emits."""

from emet_sdk._configure import configure
from emet_sdk._turn import TurnInfo, turn

__all__ = ["TurnInfo", "configure", "turn"]
__version__ = "0.1.0.dev0"
