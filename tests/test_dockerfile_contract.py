from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _instructions(dockerfile_text: str) -> list[str]:
    lines = []
    for raw_line in dockerfile_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def test_backend_image_includes_assurance_package():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    instructions = _instructions(dockerfile)

    assert "COPY app ./app" in instructions
    assert "COPY assurance/change ./assurance/change" in instructions
    assert instructions.index("COPY app ./app") < instructions.index(
        "COPY assurance/change ./assurance/change"
    )


def test_backend_image_does_not_include_non_runtime_assurance_trees():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    instructions = _instructions(dockerfile)

    assert not any(line.startswith("COPY assurance ./assurance") for line in instructions)
    assert not any("assurance/phase0" in line for line in instructions)
    assert not any("assurance/reference" in line for line in instructions)
