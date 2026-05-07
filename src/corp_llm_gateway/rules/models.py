from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    pattern: str
    replacement: str


@dataclass(frozen=True)
class Rules:
    rules: tuple[Rule, ...]
