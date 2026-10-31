"""Exceptions shared by the model-facing modules."""


class LLMError(Exception):
    """A model call failed. The message is safe to show to the user."""


class LLMConnectionError(LLMError):
    """The model server could not be reached at all."""


class LLMTimeout(LLMError):
    """The model took too long to answer."""


class LLMFormatError(LLMError):
    """The model answered, but not with the JSON we asked for."""
