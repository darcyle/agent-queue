from __future__ import annotations

import json
import uuid

from .base import ChatProvider
from .tool_conversion import anthropic_tools_to_openai
from .types import ChatResponse, TextBlock, ToolUseBlock


class OllamaChatProvider(ChatProvider):
    """Chat provider using Ollama's OpenAI-compatible endpoint."""

    def __init__(self, model: str = "qwen2.5:32b-instruct-q3_K_M", base_url: str = "http://localhost:11434/v1"):
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(base_url=base_url, api_key="ollama")
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    async def create_message(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse:
        openai_messages = self._convert_messages(messages, system)

        kwargs: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": openai_messages,
        }
        if tools:
            kwargs["tools"] = anthropic_tools_to_openai(tools)

        resp = await self._client.chat.completions.create(**kwargs)

        choice = resp.choices[0]
        content: list[TextBlock | ToolUseBlock] = []

        if choice.message.content:
            content.append(TextBlock(text=choice.message.content))

        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                args = tc.function.arguments
                if isinstance(args, str):
                    args = json.loads(args)
                content.append(ToolUseBlock(
                    id=tc.id or str(uuid.uuid4())[:8],
                    name=tc.function.name,
                    input=args,
                ))

        return ChatResponse(content=content)

    @staticmethod
    def _convert_messages(messages: list[dict], system: str) -> list[dict]:
        """Convert Anthropic-format messages to OpenAI format."""
        result: list[dict] = [{"role": "system", "content": system}]

        for msg in messages:
            role = msg["role"]
            content = msg["content"]

            if role == "user" and isinstance(content, list):
                # Could be tool_result blocks
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        result.append({
                            "role": "tool",
                            "tool_call_id": item["tool_use_id"],
                            "content": item.get("content", ""),
                        })
                    elif isinstance(item, dict) and item.get("type") == "text":
                        result.append({"role": "user", "content": item["text"]})
                    else:
                        result.append({"role": "user", "content": str(item)})

            elif role == "assistant" and isinstance(content, list):
                # Could be tool_use blocks (our normalized ToolUseBlock dataclasses)
                text_parts = []
                tool_calls = []
                for item in content:
                    if hasattr(item, "text"):
                        text_parts.append(item.text)
                    elif hasattr(item, "name") and hasattr(item, "input"):
                        tool_calls.append({
                            "id": item.id,
                            "type": "function",
                            "function": {
                                "name": item.name,
                                "arguments": json.dumps(item.input),
                            },
                        })

                assistant_msg: dict = {"role": "assistant"}
                if text_parts:
                    assistant_msg["content"] = "\n".join(text_parts)
                else:
                    assistant_msg["content"] = None
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                result.append(assistant_msg)

            else:
                result.append({"role": role, "content": content})

        return result
