"""制品清单登记库：SQLite 元数据 + 本地内容目录。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from .errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    BatchManifestMismatchError,
)
from .models import ArtifactRecord, ArtifactSpec, BatchResult

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CHUNK_SIZE = 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    project TEXT NOT NULL,
    version TEXT NOT NULL,
    sha256  TEXT NOT NULL,
    PRIMARY KEY (project, version)
);
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    manifest TEXT NOT NULL,
    success  INTEGER NOT NULL,
    result   TEXT NOT NULL
);
"""


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_manifest(artifacts: list[ArtifactSpec]) -> str:
    """批次身份：项目、版本、声明摘要的有序集合，不含顺序与源路径。"""
    identity = sorted((a.project, a.version, a.sha256) for a in artifacts)
    return json.dumps(identity, separators=(",", ":"))


def _result_to_json(result: BatchResult) -> str:
    payload = {
        "batch_id": result.batch_id,
        "success": result.success,
        "error": result.error,
        "artifacts": [
            {
                "project": a.project,
                "version": a.version,
                "sha256": a.sha256,
                "reused": a.reused,
            }
            for a in result.artifacts
        ],
    }
    return json.dumps(payload, separators=(",", ":"))


def _result_from_json(raw: str) -> BatchResult:
    payload = json.loads(raw)
    return BatchResult(
        batch_id=payload["batch_id"],
        success=payload["success"],
        error=payload["error"],
        artifacts=tuple(
            ArtifactRecord(
                project=a["project"],
                version=a["version"],
                sha256=a["sha256"],
                reused=a["reused"],
            )
            for a in payload["artifacts"]
        ),
    )


class Registry:
    """制品登记库，由上层发布流水线调用。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._content_dir = self._root / "content"
        self._content_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._root / "registry.db")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Registry":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def register_batch(self, batch_id: str, artifacts: list[ArtifactSpec]) -> BatchResult:
        """批量登记一批制品（只处理进入 build 阶段的登记）。"""
        artifacts = [
            ArtifactSpec(a.project, a.version, a.sha256.lower(), a.source_path)
            for a in artifacts
        ]
        failure = self._validate_specs(batch_id, artifacts)
        if failure is not None:
            return self._finish_batch(batch_id, artifacts, failure)

        existing = self._load_batch_row(batch_id)
        manifest = _canonical_manifest(artifacts)
        if existing is not None:
            if existing["manifest"] != manifest:
                raise BatchManifestMismatchError(
                    f"批次号 {batch_id!r} 已绑定其他清单，拒绝登记"
                )
            if existing["success"]:
                # 已成功批次的相同清单重试：直接返回原结果，不依赖源文件。
                return _result_from_json(existing["result"])
            # 失败批次允许按相同清单重试。

        records: list[ArtifactRecord] = []
        new_contents: list[tuple[str, Path]] = []  # (sha256, source_path)
        for spec in artifacts:
            registered = self._lookup_artifact(spec.project, spec.version)
            if registered is not None:
                if registered != spec.sha256:
                    return self._finish_batch(
                        batch_id,
                        artifacts,
                        f"制品 {spec.project} {spec.version} 内容冲突："
                        f"已登记摘要 {registered}，本批声明 {spec.sha256}",
                    )
                records.append(
                    ArtifactRecord(spec.project, spec.version, spec.sha256, reused=True)
                )
                continue
            source = Path(spec.source_path)
            if not source.is_file():
                return self._finish_batch(
                    batch_id, artifacts, f"源文件不存在：{source}"
                )
            actual = _hash_file(source)
            if actual != spec.sha256:
                return self._finish_batch(
                    batch_id,
                    artifacts,
                    f"源文件 {source} 实际摘要 {actual} 与声明 {spec.sha256} 不一致",
                )
            records.append(
                ArtifactRecord(spec.project, spec.version, spec.sha256, reused=False)
            )
            new_contents.append((spec.sha256, source))

        # 先复制内容（内容寻址，未提交元数据前不可见），再单事务提交元数据，
        # 保证一批新增制品全部可见或全部不可见。
        for digest, source in new_contents:
            self._store_content(digest, source)

        result = BatchResult(batch_id=batch_id, success=True, artifacts=tuple(records))
        self._commit_success(batch_id, manifest, records, result)
        return result

    def get_batch_result(self, batch_id: str) -> BatchResult | None:
        """查询某批次的登记结果；批次号未知时返回 None。"""
        row = self._load_batch_row(batch_id)
        if row is None:
            return None
        return _result_from_json(row["result"])

    def read_artifact(self, project: str, version: str) -> bytes:
        """按项目和版本读回制品内容，读回时经过 SHA-256 核验。"""
        digest = self._lookup_artifact(project, version)
        if digest is None:
            raise ArtifactNotFoundError(f"未登记的制品：{project} {version}")
        content_path = self._content_dir / digest
        if not content_path.is_file():
            raise ArtifactIntegrityError(
                f"库内内容缺失：{project} {version}（摘要 {digest}）"
            )
        data = content_path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ArtifactIntegrityError(
                f"库内内容损坏：{project} {version}（摘要 {digest}）"
            )
        return data

    def _validate_specs(
        self, batch_id: str, artifacts: list[ArtifactSpec]
    ) -> str | None:
        if not batch_id:
            return "批次号不能为空"
        if not artifacts:
            return "清单不能为空"
        seen: set[tuple[str, str]] = set()
        for spec in artifacts:
            if not spec.project:
                return "项目名不能为空"
            if not _VERSION_RE.match(spec.version):
                return f"非法版本号：{spec.version!r}（应为 major.minor.patch）"
            if not _SHA256_RE.match(spec.sha256.lower()):
                return f"非法 SHA-256 摘要：{spec.sha256!r}"
            key = (spec.project, spec.version)
            if key in seen:
                return f"批内项目与版本组合重复：{spec.project} {spec.version}"
            seen.add(key)
        return None

    def _finish_batch(
        self, batch_id: str, artifacts: list[ArtifactSpec], error: str
    ) -> BatchResult:
        """记录失败结果（不产生任何可见制品），允许相同清单重试。"""
        result = BatchResult(batch_id=batch_id, success=False, error=error)
        manifest = _canonical_manifest(artifacts)
        with self._conn:
            self._conn.execute(
                "INSERT INTO batches (batch_id, manifest, success, result)"
                " VALUES (?, ?, 0, ?)"
                " ON CONFLICT(batch_id) DO UPDATE SET"
                "   manifest=excluded.manifest, result=excluded.result",
                (batch_id, manifest, _result_to_json(result)),
            )
        return result

    def _commit_success(
        self,
        batch_id: str,
        manifest: str,
        records: list[ArtifactRecord],
        result: BatchResult,
    ) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT INTO artifacts (project, version, sha256) VALUES (?, ?, ?)",
                [
                    (r.project, r.version, r.sha256)
                    for r in records
                    if not r.reused
                ],
            )
            self._conn.execute(
                "INSERT INTO batches (batch_id, manifest, success, result)"
                " VALUES (?, ?, 1, ?)"
                " ON CONFLICT(batch_id) DO UPDATE SET"
                "   manifest=excluded.manifest, success=1, result=excluded.result",
                (batch_id, manifest, _result_to_json(result)),
            )

    def _load_batch_row(self, batch_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT manifest, success, result FROM batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if row is None:
            return None
        return {"manifest": row[0], "success": bool(row[1]), "result": row[2]}

    def _lookup_artifact(self, project: str, version: str) -> str | None:
        row = self._conn.execute(
            "SELECT sha256 FROM artifacts WHERE project = ? AND version = ?",
            (project, version),
        ).fetchone()
        return row[0] if row else None

    def _store_content(self, digest: str, source: Path) -> None:
        target = self._content_dir / digest
        if target.exists():
            return  # 内容寻址：相同摘要的内容已存在
        tmp = self._content_dir / f".{digest}.tmp"
        with source.open("rb") as src, tmp.open("wb") as dst:
            while chunk := src.read(_CHUNK_SIZE):
                dst.write(chunk)
        tmp.replace(target)  # 同目录原子改名，避免半截内容可见
