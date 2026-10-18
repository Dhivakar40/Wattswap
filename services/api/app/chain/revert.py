"""Decode Solidity custom-error revert data using the contract ABIs."""

from dataclasses import dataclass, field
from typing import Any

from eth_abi import decode
from eth_utils import keccak
from eth_utils.abi import collapse_if_tuple
from hexbytes import HexBytes

ERROR_STRING_SELECTOR = "0x08c379a0"
PANIC_SELECTOR = "0x4e487b71"

# Reverts that can clear up by themselves (e.g. the chain clock has not reached the interval end).
TRANSIENT_ERRORS = frozenset({"ReadingNotComplete"})


@dataclass(frozen=True)
class RevertInfo:
    name: str
    args: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        if not self.args:
            return self.name
        inner = ", ".join(f"{k}={v}" for k, v in self.args.items())
        return f"{self.name}({inner})"

    @property
    def transient(self) -> bool:
        return self.name in TRANSIENT_ERRORS


def _fmt(value: object) -> object:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    return value


class RevertDecoder:
    def __init__(self, abis: list[list[dict[str, Any]]]) -> None:
        self._errors: dict[str, tuple[str, list[str], list[str]]] = {}
        for abi in abis:
            for item in abi:
                if item.get("type") != "error":
                    continue
                types = [collapse_if_tuple(i) for i in item.get("inputs", [])]
                names = [i["name"] for i in item.get("inputs", [])]
                sig = f"{item['name']}({','.join(types)})"
                self._errors["0x" + keccak(text=sig)[:4].hex()] = (item["name"], types, names)

    def decode(self, data: str | bytes | None) -> RevertInfo | None:
        if data is None:
            return None
        raw = HexBytes(data) if not isinstance(data, HexBytes) else data
        if len(raw) < 4:
            return None
        selector = "0x" + raw[:4].hex()
        body = bytes(raw[4:])
        try:
            if selector == ERROR_STRING_SELECTOR:
                (msg,) = decode(["string"], body)
                return RevertInfo("Error", {"message": msg})
            if selector == PANIC_SELECTOR:
                (code,) = decode(["uint256"], body)
                return RevertInfo("Panic", {"code": hex(code)})
            entry = self._errors.get(selector)
            if entry is None:
                return RevertInfo("UnknownError", {"selector": selector})
            name, types, names = entry
            values = decode(types, body) if types else ()
            return RevertInfo(name, {n: _fmt(v) for n, v in zip(names, values, strict=True)})
        except Exception:  # malformed revert data must never crash the pipeline
            return RevertInfo("UndecodableRevert", {"selector": selector})
