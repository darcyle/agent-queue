# Chat Providers — Specification

## 1. Overview

The `src/chat_providers/` package provides a thin, uniform abstraction over LLM providers used for chat interactions. It is consumed exclusively by `ChatAgent` (`src/chat_agent.py`) to drive the Discord-facing conversational interface.

The abstraction serves two purposes:

1. Allow `ChatAgent` to call a single `create_message` method regardless of which backend is configured, receiving a normalized response object it can inspect without knowing provider-specific details.
2. Keep provider-specific credential detection, format conversion, and SDK calls isolated from the rest of the codebase.

The package contains six modules:

| Module | Responsibility |
|---|---|
| `types.py` | Normalized response dataclasses (`TextBlock`, `ToolUseBlock`, `ChatResponse`) |
| `base.py` | Abstract base class `ChatProvider` |
| `anthropic.py` | Anthropic-backed implementation (direct API, Vertex AI, Bedrock, OAuth) |
| `ollama.py` | Ollama-backed implementation (OpenAI-compatible `/v1` endpoint) |
| `tool_conversion.py` | Utility to convert Anthropic tool schemas to OpenAI function-calling format |
| `__init__.py` | Public surface and `create_chat_provider` factory |

---

## Source Files

- `src/chat_providers/base.py`
- `src/chat_providers/types.py`
- `src/chat_providers/tool_conversion.py`
- `src/chat_providers/__init__.py`
- `src/chat_providers/anthropic.py`
- `src/chat_providers/ollama.py`

---

## 2. Response Types

Defined in `src/chat_providers/types.py`. All types are plain dataclasses with no external dependencies.

### TextBlock

Represents a plain-text segment within a model response.

```python
@dataclass
class TextBlock:
    text: str
```

### ToolUseBlock

Represents a single tool invocation requested by the model.

```python
@dataclass
class ToolUseBlock:
    id: str      # Unique identifier for this tool call (used to match tool results)
    name: str    # Name of the tool being invoked
    input: dict  # Parsed arguments for the tool call
```

### ChatResponse

The single return type from every `create_message` call. Contains an ordered list of content blocks that may be any mix of `TextBlock` and `ToolUseBlock`.

```python
@dataclass
class ChatResponse:
    content: list[TextBlock | ToolUseBlock]
```

**Helper properties:**

| Property | Type | Description |
|---|---|---|
| `text_parts` | `list[str]` | Extracts the `.text` value from every `TextBlock` in `content`, in order. |
| `tool_uses` | `list[ToolUseBlock]` | Filters `content` to only `ToolUseBlock` entries. |
| `has_tool_use` | `bool` | `True` when at least one `ToolUseBlock` is present in `content`. |

All three properties are computed on access with no caching; `content` is the single source of truth.

---

## 3. ChatProvider Interface

Defined in `src/chat_providers/base.py`.

```python
class ChatProvider(ABC):
    @abstractmethod
    async def create_message(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse: ...

    @property
    @abstractmethod
    def model_name(self) -> str: ...
```

All parameters to `create_message` are keyword-only.

**Parameter conventions:**

| Parameter | Description |
|---|---|
| `messages` | Conversation history in Anthropic message format. Each entry is a dict with at least `"role"` and `"content"` keys. Content may be a plain string or a list of typed content blocks (e.g. `tool_result`). |
| `system` | The system prompt string passed directly to the model. |
| `tools` | Optional list of tool definitions in Anthropic format (`name`, `description`, `input_schema`). Providers that do not use Anthropic's native format are responsible for converting this before passing to their SDK. |
| `max_tokens` | Maximum tokens to generate. Defaults to `1024`. |

**`model_name` property:**

Returns the string model identifier that will appear in any logging or metadata. Set during `__init__` and never mutated afterward.

---

## 4. Factory

Defined in `src/chat_providers/__init__.py`.

```python
def create_chat_provider(config: ChatProviderConfig) -> ChatProvider | None:
```

Accepts a `ChatProviderConfig` dataclass (from `src/config.py`) and returns an initialized provider, or `None` if initialization fails.

**Routing logic:**

1. If `config.provider == "ollama"`, construct and return an `OllamaChatProvider`. This never returns `None`; it always succeeds because Ollama needs no credentials.
   - Default model: `"qwen2.5:32b-instruct-q3_K_M"`
   - Default base URL: `"http://localhost:11434/v1"`
   - Both defaults are overridden by `config.model` and `config.base_url` when set.

2. For any other `config.provider` value (including the default, `"anthropic"`), construct an `AnthropicChatProvider`. If `provider.is_configured` is `False` (no credentials were found during `__init__`), return `None`.

`create_chat_provider` returning `None` signals to `ChatAgent` that no LLM-backed chat interface is available; `ChatAgent` must handle this gracefully.

**Public exports from `__init__.py`:**

```
ChatProvider, ChatResponse, TextBlock, ToolUseBlock, create_chat_provider
```

---

## 5. Anthropic Provider

Defined in `src/chat_providers/anthropic.py`. Class: `AnthropicChatProvider`.

### Credential Detection Priority

`__init__` probes four credential sources in strict priority order, stopping at the first that succeeds. The source that is selected determines the SDK client class that is instantiated.

**Priority 1 — Vertex AI**

Checked when either `GOOGLE_CLOUD_PROJECT` or `ANTHROPIC_VERTEX_PROJECT_ID` is set in the environment.

- Client class: `anthropic.AsyncAnthropicVertex`
- Region is resolved from (in order): `GOOGLE_CLOUD_LOCATION`, `CLOUD_ML_REGION`, fallback `"us-east5"`.
- Default model when `config.model` is empty: `"claude-sonnet-4@20250514"` (note the `@` versioning convention used by Vertex).

**Priority 2 — Bedrock**

Checked when either `AWS_REGION` or `AWS_DEFAULT_REGION` is set.

- Client class: `anthropic.AsyncAnthropicBedrock` (no additional constructor arguments; uses standard AWS credential chain).
- Default model when `config.model` is empty: `"claude-sonnet-4-20250514"`.

**Priority 3 — API Key**

Checked when `ANTHROPIC_API_KEY` is set.

- Client class: `anthropic.AsyncAnthropic(api_key=...)`
- Default model when `config.model` is empty: `"claude-sonnet-4-20250514"`.

**Priority 4 — OAuth Token**

Checked last by calling `_load_claude_oauth_token()`.

- Client class: `anthropic.AsyncAnthropic(auth_token=...)`
- Default model when `config.model` is empty: `"claude-sonnet-4-20250514"`.

If none of the four sources yield a client, `self._client` remains `None`.

### OAuth Token Loading

Module-level function `_load_claude_oauth_token() -> str | None`.

Searches for a credentials file in `~/.claude/` under two candidate names, tried in order:

1. `.credentials.json`
2. `credentials.json`

For the first file that exists, it reads and JSON-parses the content, then navigates:

```
root -> "claudeAiOauth" -> "accessToken"
```

If an `accessToken` is found, it also checks `"expiresAt"` (a millisecond Unix timestamp). If `expiresAt` is present and less than the current time in milliseconds, a warning is printed to stdout but the token is still returned and used. Read errors are caught and printed as warnings; the function returns `None` in that case.

Returns the access token string, or `None` if no valid token was found.

### is_configured Flag

```python
@property
def is_configured(self) -> bool:
    return self._client is not None
```

`False` means none of the four credential sources succeeded. The factory uses this to decide whether to return `None`.

### Message Creation

```python
async def create_message(
    self,
    *,
    messages: list[dict],
    system: str,
    tools: list[dict] | None = None,
    max_tokens: int = 1024,
) -> ChatResponse:
```

Delegates directly to `self._client.messages.create(...)` with the following kwargs:

- `model`: `self._model`
- `max_tokens`: as passed
- `system`: as passed
- `messages`: as passed (already in Anthropic format; no conversion needed)
- `tools`: included in kwargs only when the `tools` argument is not `None` and non-empty

The raw SDK response `resp.content` is a list of block objects. Each block is inspected by its `.type` attribute:

- `"text"` -> `TextBlock(text=block.text)`
- `"tool_use"` -> `ToolUseBlock(id=block.id, name=block.name, input=block.input)`
- Any other block type is silently dropped.

Returns a `ChatResponse` with the assembled `content` list.

---

## 6. Ollama Provider

Defined in `src/chat_providers/ollama.py`. Class: `OllamaChatProvider`.

### Initialization

```python
def __init__(
    self,
    model: str = "qwen2.5:32b-instruct-q3_K_M",
    base_url: str = "http://localhost:11434/v1",
):
```

Constructs an `openai.AsyncOpenAI` client pointed at the given `base_url`. Ollama's API does not require authentication, so the `api_key` is set to the literal string `"ollama"` to satisfy the SDK's required-parameter check.

### Message Creation

```python
async def create_message(
    self,
    *,
    messages: list[dict],
    system: str,
    tools: list[dict] | None = None,
    max_tokens: int = 1024,
) -> ChatResponse:
```

Steps:

1. Convert the incoming `messages` + `system` to OpenAI format via `_convert_messages`.
2. Build kwargs: `model`, `max_tokens`, `messages`.
3. If `tools` is provided, convert via `anthropic_tools_to_openai` and add as `tools` kwarg.
4. Call `self._client.chat.completions.create(**kwargs)`.
5. Inspect `resp.choices[0]`:
   - If `choice.message.content` is non-empty, append a `TextBlock`.
   - If `choice.message.tool_calls` is non-empty, iterate and append one `ToolUseBlock` per tool call. The `arguments` field of each function call may arrive as a JSON string; if so it is parsed with `json.loads`. The tool call `id` is used as-is; if absent or falsy a random 8-character UUID prefix is generated via `uuid.uuid4()`.
6. Return `ChatResponse(content=content)`.

### Message Format Conversion

Static method `_convert_messages(messages, system) -> list[dict]`.

Converts Anthropic-format conversation history to OpenAI chat format.

**Output always starts with:**

```python
{"role": "system", "content": system}
```

**Per-message conversion rules:**

| Incoming role | Incoming content type | Output |
|---|---|---|
| `"user"` | `list` | Iterate items. Each `dict` with `type == "tool_result"` becomes a `{"role": "tool", "tool_call_id": item["tool_use_id"], "content": item.get("content", "")}` message. Each `dict` with `type == "text"` becomes a `{"role": "user", "content": item["text"]}` message. Any other item is stringified and emitted as `{"role": "user", "content": str(item)}`. |
| `"assistant"` | `list` | Items are split into text parts (objects with a `.text` attribute) and tool calls (objects with `.name` and `.input` attributes). A single assistant message is assembled: `content` is the newline-joined text parts, or `None` if there are none. `tool_calls` is added only when tool call objects were found, serialized as OpenAI function call dicts with `arguments` JSON-encoded from `.input`. |
| any role | non-list | Pass through as `{"role": role, "content": content}` unchanged. |

Note: list-type `"user"` content containing `tool_result` blocks produces multiple separate messages in the output list, one per block, rather than a single batched message.

---

## 7. Tool Conversion Utility

Defined in `src/chat_providers/tool_conversion.py`.

```python
def anthropic_tools_to_openai(tools: list[dict]) -> list[dict]:
```

Converts a list of tool definitions from Anthropic format to OpenAI function-calling format.

**Anthropic input format (per tool):**

```json
{
  "name": "tool_name",
  "description": "What the tool does",
  "input_schema": {
    "type": "object",
    "properties": { ... },
    "required": [...]
  }
}
```

**OpenAI output format (per tool):**

```json
{
  "type": "function",
  "function": {
    "name": "tool_name",
    "description": "What the tool does",
    "parameters": {
      "type": "object",
      "properties": { ... },
      "required": [...]
    }
  }
}
```

The mapping is:
- `input_schema` -> `parameters` (the dict is used as-is, not deep-copied)
- `description` defaults to `""` when absent
- `input_schema` defaults to `{"type": "object", "properties": {}}` when absent

This function is called by `OllamaChatProvider.create_message` before passing tools to the OpenAI SDK. It is not used by `AnthropicChatProvider`, which passes the original Anthropic-format tool definitions directly to the Anthropic SDK.
