"""Test-only dependency adapter for deterministic Change Assurance fixtures."""
from __future__ import annotations

from typing import Any

from assurance.change.checker import DistributionInspection, ImportInspection


class FixtureDependencyEnvironment:
    def __init__(self, fixture: dict[str, Any]):
        self.fixture = fixture

    def inventory(self) -> dict[str, str]:
        return dict(self.fixture.get("inventory", {}))

    def distributions_for_import(self, module: str) -> list[str]:
        root = module.split(".", 1)[0]
        return list(self.fixture.get("import_map", {}).get(root, []))

    def inspect_import(self, module: str, symbol: str | None) -> ImportInspection:
        key = f"{module}:{symbol or ''}"
        resolves = bool(self.fixture.get("imports", {}).get(key, False))
        return ImportInspection(
            resolves,
            "fixture import resolved" if resolves else "fixture import missing",
        )

    def inspect_distribution(self, distribution: str) -> DistributionInspection:
        value = self.fixture.get("distributions", {}).get(distribution)
        if value is None:
            return DistributionInspection(False, None, None, "fixture distribution missing")
        return DistributionInspection(
            bool(value["installed"]),
            value.get("version"),
            value.get("license"),
            "fixture distribution inspected",
        )
