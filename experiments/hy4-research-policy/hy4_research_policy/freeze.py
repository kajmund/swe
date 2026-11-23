"""Load the frozen Gym snapshot and check its published digests."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def package_root() -> Path:
    return PACKAGE_ROOT


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_protocol() -> dict[str, Any]:
    protocol = load_json(PACKAGE_ROOT / "config" / "protocol.json")
    if protocol.get("schema_version") != "hy4-research-policy-v1":
        raise ValueError(f"unsupported protocol {protocol.get('schema_version')!r}")
    return protocol


def sha256_file(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def sha256_corpus_dir(path: Path) -> str:
    """Match Research Gym's directory digest: name, NUL, bytes, NUL."""

    files = sorted(path.glob("*.jsonl"))
    if not files:
        raise ValueError(f"corpus directory contains no JSONL files: {path}")
    digest = sha256()
    for file_path in files:
        digest.update(file_path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _manifest_hash(directory: Path) -> str:
    manifest = directory / "MANIFEST.sha256"
    if not manifest.is_file():
        raise ValueError(f"missing manifest: {manifest}")
    return manifest.read_text(encoding="utf-8").split()[0]


def load_questions(protocol: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    protocol = protocol or load_protocol()
    path = PACKAGE_ROOT / protocol["frozen"]["questions"]
    actual = sha256_file(path)
    expected = protocol["frozen"]["questions_sha256"]
    published = _manifest_hash(path.parent)
    if actual != expected or actual != published:
        raise ValueError("frozen questions hash mismatch")
    payload = load_json(path)
    questions = payload["questions"]
    ids = [item["question_id"] for item in questions]
    if ids != protocol["frozen"]["question_ids"]:
        raise ValueError("frozen question ids differ from the protocol")
    return questions


def load_corpus(protocol: dict[str, Any] | None = None) -> dict[str, str]:
    protocol = protocol or load_protocol()
    directory = PACKAGE_ROOT / protocol["frozen"]["corpus_dir"]
    actual = sha256_corpus_dir(directory)
    expected = protocol["frozen"]["corpus_directory_sha256"]
    published = _manifest_hash(directory)
    if actual != expected or actual != published:
        raise ValueError("frozen corpus hash mismatch")
    documents: dict[str, str] = {}
    for file_path in sorted(directory.glob("*.jsonl")):
        for line in file_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            documents[str(row["document_id"])] = str(row["content"])
    if not documents:
        raise ValueError("frozen corpus is empty")
    return documents


def question_by_id(questions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["question_id"]: item for item in questions}
