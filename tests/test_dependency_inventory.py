from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def _normalize_name(value: str) -> str:
    return value.lower().replace("_", "-")


def test_dependency_license_inventory_covers_and_binds_the_lockfile() -> None:
    lock_bytes = (ROOT / "uv.lock").read_bytes()
    lock = tomllib.loads(lock_bytes.decode("utf-8"))
    inventory = json.loads((ROOT / "docs/licensing/dependency_inventory.json").read_text(encoding="utf-8"))

    locked_names = {_normalize_name(package["name"]) for package in lock["package"]}
    inventory_names = {_normalize_name(package["name"]) for package in inventory["python_packages"]}

    assert locked_names == inventory_names | {"minillm"}
    assert inventory["locked_project_count"] == 1
    assert inventory["locked_third_party_count"] == len(inventory_names)
    assert (
        inventory["inventory_coverage"]
        == f"{len(inventory_names)}/{len(inventory_names)} locked third-party package names"
    )
    assert inventory["lock_sha256"] == hashlib.sha256(lock_bytes).hexdigest()
    assert all(package["license"] for package in inventory["python_packages"])


def test_downloaded_model_dependencies_are_not_claimed_as_distributed() -> None:
    inventory = json.loads((ROOT / "docs/licensing/dependency_inventory.json").read_text(encoding="utf-8"))

    assert inventory["model_dependencies"]
    assert all(model["distributed_with_repository"] is False for model in inventory["model_dependencies"])
    assert all(model["license"] and model["source"] for model in inventory["model_dependencies"])
