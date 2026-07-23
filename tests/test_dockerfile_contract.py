from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_backend_image_includes_assurance_package():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY app ./app" in dockerfile
    assert "COPY assurance ./assurance" in dockerfile
    assert dockerfile.index("COPY app ./app") < dockerfile.index("COPY assurance ./assurance")
