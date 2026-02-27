from typing import Protocol, Optional, AsyncIterator
from abc import abstractmethod

class LLMClient(Protocol):
    """Protocol for LLM clients.

    Required method:  generate()
    Optional method:  astream() — async generator that yields str chunks.
                      Clients that implement it enable streaming execution.
                      Clients that don't will fall back to generate().
    """

    @abstractmethod
    async def generate(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        """Generates text from a prompt."""
        ...

class LLMError(Exception):
    """Base class for LLM errors."""
    pass

class LLMTimeoutError(LLMError):
    """Raised when an LLM request times out."""
    pass

class LLMContextWindowError(LLMError):
    """Raised when the prompt exceeds the LLM context window."""
    pass
