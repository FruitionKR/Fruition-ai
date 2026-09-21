"""Fail when the converter runtime carries GPU-only torch dependencies.

Two modes:
- no argument: inspect the installed environment (used in the Dockerfile build).
- ``--report PATH``: inspect a ``pip install --report`` JSON (used in CI dry-run).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

GPU_PREFIXES = ("nvidia-", "cuda-", "triton")


def gpu_packages(names: list[str]) -> list[str]:
    return sorted(n for n in names if n.lower().startswith(GPU_PREFIXES))


def check_versions(installed: dict[str, str]) -> list[str]:
    errors = []
    for name in ("torch", "torchvision"):
        version = installed.get(name)
        if version is None:
            errors.append(f"{name} is not installed")
        elif not version.endswith("+cpu"):
            errors.append(f"{name}=={version} is not a +cpu wheel")
    gpu = gpu_packages(list(installed))
    if gpu:
        errors.append("GPU packages present: " + ", ".join(gpu))
    return errors


def runtime_errors() -> list[str]:
    """Import the CPU runtime the converter relies on and exercise torchvision ops."""
    import torch

    errors = []
    if torch.version.cuda is not None:
        errors.append(f"torch.version.cuda is {torch.version.cuda!r}, expected None")
    if torch.cuda.is_available():
        errors.append("torch reports CUDA availability in a CPU-only image")
    try:
        import torchvision
        from torchvision.ops import nms, roi_align

        boxes = torch.tensor([[0.0, 0.0, 10.0, 10.0], [1.0, 1.0, 11.0, 11.0]])
        kept = nms(boxes, torch.tensor([0.9, 0.8]), iou_threshold=0.5)
        if kept.tolist() != [0]:
            errors.append(f"torchvision nms returned {kept.tolist()}")
        roi_align(torch.zeros(1, 1, 4, 4), torch.tensor([[0.0, 0.0, 0.0, 2.0, 2.0]]), 2)
        torchvision.io  # noqa: B018  image codecs must link against CPU libs
        import docling.document_converter  # noqa: F401
        import pix2tex.cli  # noqa: F401
    except Exception as exc:  # noqa: BLE001  any import/op failure fails the build
        errors.append(f"CPU runtime smoke failed: {exc!r}")
    return errors


def installed_from_report(path: Path) -> dict[str, str]:
    report = json.loads(path.read_text(encoding="utf-8"))
    return {
        item["metadata"]["name"].lower(): item["metadata"]["version"]
        for item in report["install"]
    }


def installed_from_environment() -> dict[str, str]:
    import importlib.metadata as metadata

    return {
        dist.metadata["Name"].lower(): dist.version for dist in metadata.distributions()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="pip --report JSON path")
    args = parser.parse_args(argv)

    if args.report is not None:
        installed = installed_from_report(args.report)
    else:
        installed = installed_from_environment()
        errors = runtime_errors()
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
    errors = check_versions(installed)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print(f"cpu-only ok: torch {installed['torch']}, torchvision {installed['torchvision']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
