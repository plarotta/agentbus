"""agentbus.chat — interactive CLI mode for agentbus.

Entry points:
  * ``run_chat(config)`` — async coroutine, use with asyncio.run()
  * ``ChatSession`` — full session class for embedding
  * ``ChatConfig`` — configuration dataclass
  * ``load_config`` — load from agentbus.yaml
  * ``first_run_wizard`` — interactive first-run setup
"""

from agentbus.chat._config import ChatConfig, first_run_wizard, load_config
from agentbus.chat._runner import ChatSession, run_chat

__all__ = [
    "ChatConfig",
    "ChatSession",
    "first_run_wizard",
    "load_config",
    "run_chat",
]
