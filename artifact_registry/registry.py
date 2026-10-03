"""制品清单登记库核心实现。

元数据存 SQLite，制品内容按 SHA-256 寻址存本地目录。
只处理制品进入 build 阶段的登记。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterable, List, Tuple

from .errors import (
    ArtifactContentCorrupt,
    ArtifactNotFound,
    BatchManifestMismatch,
    BatchRegistrationFailed,
)
from .models import ArtifactOutcome, ArtifactSpec, BatchResult

_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
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
    batch_id      TEXT PRIMARY KEY,
    manifest_hash TEXT NOT NULL,
    success       INTEGER NOT NULL,
    detail        TEXT NOT NULL,
    result_json   TEXT NOT NULL
);
"""


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_hash(items: List[ArtifactSpec]) -> str:
    """批次身份：项目、版本、声明摘要的集合；顺序与源路径不参与。"""
    canonical = sorted((a.project, a.version, a.sha256) for a in items)
    payload = json.dumps(canonical, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ArtifactRegistry:
    """制品登记库。root 下自动创建 metadata.sqlite3 与 content/ 目录。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._content_dir = self._root / "content"
        self._content_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(
            self._root / "metadata.sqlite3", isolation_level=None
        )
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # 批量登记
    # ------------------------------------------------------------------
    def register_batch(
        self, batch_id: str, artifacts: Iterable[ArtifactSpec]
    ) -> BatchResult:
        specs = list(artifacts)
        self._validate_specs(batch_id, specs)
        manifest = _manifest_hash(specs)

        row = self._db.execute(
            "SELECT manifest_hash, success, detail, result_json "
            "FROM batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if row is not None:
            if row[0] != manifest:
                raise BatchManifestMismatch(
                    f"批次号 {batch_id!r} 已绑定其他清单，拒绝登记"
                )
            return self._stored_result(batch_id, row)

        try:
            outcomes = self._register_new_batch(batch_id, specs)
            result = BatchResult(
                batch_id=batch_id, success=True, artifacts=outcomes
            )
            self._record_batch(batch_id, manifest, result)
            return result
        except BatchRegistrationFailed as exc:
            result = BatchResult(
                batch_id=batch_id, success=False, artifacts=[], detail=str(exc)
            )
            self._record_batch(batch_id, manifest, result)
            return result

    # ------------------------------------------------------------------
    # 读取制品
    # ------------------------------------------------------------------
    def read_artifact(self, project: str, version: str) -> bytes:
        row = self._db.execute(
            "SELECT sha256 FROM artifacts WHERE project = ? AND version = ?",
            (project, version),
        ).fetchone()
        if row is None:
            raise ArtifactNotFound(f"未登记制品 {project}@{version}")
        sha256 = row[0]
        content_path = self._content_dir / sha256
        if not content_path.is_file():
            raise ArtifactContentCorrupt(
                f"制品 {project}@{version} 的库内内容缺失"
            )
        data = content_path.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ArtifactContentCorrupt(
                f"制品 {project}@{version} 的库内内容摘要核验失败"
            )
        return data

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "ArtifactRegistry":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _validate_specs(self, batch_id: str, specs: List[ArtifactSpec]) -> None:
        if not batch_id:
            raise ValueError("批次号不能为空")
        if not specs:
            raise ValueError("清单不能为空")
        seen: set[Tuple[str, str]] = set()
        for spec in specs:
            if not spec.project:
                raise ValueError("项目名不能为空")
            if not _VERSION_RE.match(spec.version):
                raise ValueError(f"非法版本号: {spec.version!r}")
            if not _SHA256_RE.match(spec.sha256):
                raise ValueError(f"非法 SHA-256 摘要: {spec.sha256!r}")
            key = (spec.project, spec.version)
            if key in seen:
                raise ValueError(
                    f"批内项目与版本组合重复: {spec.project}@{spec.version}"
                )
            seen.add(key)

    def _register_new_batch(
        self, batch_id: str, specs: List[ArtifactSpec]
    ) -> List[ArtifactOutcome]:
        # 先算好每件制品的处置方式，全部确认可行后才落库。
        plan: List[Tuple[ArtifactSpec, str, bool]] = []  # (spec, sha, reused)
        for spec in specs:
            row = self._db.execute(
                "SELECT sha256 FROM artifacts WHERE project = ? AND version = ?",
                (spec.project, spec.version),
            ).fetchone()
            if row is not None:
                if row[0] != spec.sha256:
                    raise BatchRegistrationFailed(
                        f"内容冲突: {spec.project}@{spec.version} 已登记摘要 "
                        f"{row[0]}，本批声明 {spec.sha256}"
                    )
                plan.append((spec, spec.sha256, True))
                continue
            source = Path(spec.source_path)
            if not source.is_file():
                raise BatchRegistrationFailed(
                    f"源文件缺失: {spec.source_path}"
                )
            actual = _hash_file(source)
            if actual != spec.sha256:
                raise BatchRegistrationFailed(
                    f"声明摘要与实际不符: {spec.source_path} "
                    f"声明 {spec.sha256}，实际 {actual}"
                )
            plan.append((spec, actual, False))

        # 先把新增内容复制进库（临时文件 + 原子改名），再单事务写入元数据，
        # 保证一批新增制品要么全部可见，要么全部不可见。
        for spec, sha256, reused in plan:
            if not reused:
                self._store_content(Path(spec.source_path), sha256)

        outcomes = [
            ArtifactOutcome(
                project=spec.project,
                version=spec.version,
                sha256=sha256,
                reused=reused,
            )
            for spec, sha256, reused in plan
        ]
        return outcomes

    def _store_content(self, source: Path, sha256: str) -> None:
        target = self._content_dir / sha256
        if target.exists():
            return  # 内容寻址，相同内容已在库中
        fd, tmp_name = tempfile.mkstemp(
            dir=self._content_dir, prefix=".tmp-"
        )
        try:
            with os.fdopen(fd, "wb") as tmp:
                with open(source, "rb") as src:
                    shutil.copyfileobj(src, tmp, _CHUNK_SIZE)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _record_batch(
        self, batch_id: str, manifest: str, result: BatchResult
    ) -> None:
        payload = json.dumps(
            [
                {
                    "project": a.project,
                    "version": a.version,
                    "sha256": a.sha256,
                    "reused": a.reused,
                }
                for a in result.artifacts
            ]
        )
        # 单事务：批次结果与制品可见性一致。
        # isolation_level=None 下需显式开启事务。
        self._db.execute("BEGIN IMMEDIATE")
        try:
            if result.success:
                self._db.executemany(
                    "INSERT INTO artifacts (project, version, sha256) "
                    "VALUES (?, ?, ?)",
                    [
                        (a.project, a.version, a.sha256)
                        for a in result.artifacts
                        if not a.reused
                    ],
                )
            self._db.execute(
                "INSERT INTO batches "
                "(batch_id, manifest_hash, success, detail, result_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    batch_id,
                    manifest,
                    1 if result.success else 0,
                    result.detail,
                    payload,
                ),
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    @staticmethod
    def _stored_result(batch_id: str, row: Tuple) -> BatchResult:
        _, success, detail, result_json = row
        outcomes = [
            ArtifactOutcome(
                project=item["project"],
                version=item["version"],
                sha256=item["sha256"],
                reused=item["reused"],
            )
            for item in json.loads(result_json)
        ]
        return BatchResult(
            batch_id=batch_id,
            success=bool(success),
            artifacts=outcomes,
            detail=detail,
        )
