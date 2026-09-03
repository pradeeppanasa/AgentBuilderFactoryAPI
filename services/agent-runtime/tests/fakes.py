"""In-memory test doubles — no real AWS calls anywhere in this suite."""

from __future__ import annotations

from typing import Any


class FakeTable:
    def __init__(self, items: dict[tuple[Any, ...], dict[str, Any]] | None = None, key_names: tuple[str, ...] = ()) -> None:
        self.items = items or {}
        self.key_names = key_names
        self.put_calls: list[dict[str, Any]] = []
        self.query_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def _key_tuple(self, key: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(key[name] for name in self.key_names) if self.key_names else tuple(sorted(key.items()))

    def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:
        self.get_calls.append(Key)
        item = self.items.get(self._key_tuple(Key))
        return {"Item": item} if item is not None else {}

    def put_item(self, Item: dict[str, Any]) -> dict[str, Any]:
        self.put_calls.append(Item)
        key = self._key_tuple({name: Item[name] for name in self.key_names}) if self.key_names else tuple(sorted(Item.items()))
        self.items[key] = Item
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.query_calls.append(kwargs)
        return {"Items": list(self.items.values())}


class FakeDynamoDBResource:
    def __init__(self, tables: dict[str, FakeTable]) -> None:
        self._tables = tables

    def Table(self, name: str) -> FakeTable:
        return self._tables[name]


class FakeLambdaClient:
    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        function_name = kwargs["FunctionName"]

        class _Payload:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

        import json

        result = self._responses.get(function_name, {"ok": True})
        return {"Payload": _Payload(json.dumps(result).encode("utf-8")), "StatusCode": 200}


class FakeBedrockRuntimeClient:
    def __init__(self, action: str = "NONE", outputs: list[dict[str, Any]] | None = None, raise_error: bool = False) -> None:
        self.action = action
        self.outputs = outputs or []
        self.raise_error = raise_error
        self.calls: list[dict[str, Any]] = []

    def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.raise_error:
            raise RuntimeError("simulated Bedrock outage")
        return {"action": self.action, "outputs": self.outputs}


class FakeBedrockAgentRuntimeClient:
    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        self.results = results or []
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"retrievalResults": self.results}


class FakeSNSClient:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, **kwargs: Any) -> dict[str, Any]:
        self.published.append(kwargs)
        return {"MessageId": "fake-message-id"}
