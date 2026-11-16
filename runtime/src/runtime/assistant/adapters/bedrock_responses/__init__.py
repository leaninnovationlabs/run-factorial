"""Chat backend that calls Amazon Bedrock Runtime's `/openai/v1/responses` itself."""

from runtime.assistant.adapters.bedrock_responses.backend import BedrockResponsesBackend
from runtime.assistant.adapters.bedrock_responses.wire import PINNED_API

__all__ = ["PINNED_API", "BedrockResponsesBackend"]
