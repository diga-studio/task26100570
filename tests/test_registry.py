"""登记库自动化测试。"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from artifact_registry import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactSpec,
    BatchManifestMismatchError,
    Registry,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RegistryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="registry-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.src_dir = self._tmp / "src"
        self.src_dir.mkdir()
        self.registry = Registry(self._tmp / "repo")
        self.addCleanup(self.registry.close)

    def make_source(self, name: str, content: bytes) -> tuple[Path, str]:
        path = self.src_dir / name
        path.write_bytes(content)
        return path, _sha256(content)

    def spec(self, project: str, version: str, name: str, content: bytes) -> ArtifactSpec:
        path, digest = self.make_source(name, content)
        return ArtifactSpec(project, version, digest, path)


class TestMixedReuseAndNew(RegistryTestCase):
    def test_reuse_then_delete_sources_then_read_back_and_retry(self) -> None:
        # 第一批：登记 proj-a 1.0.0
        old = self.spec("proj-a", "1.0.0", "a-1.0.0.bin", b"alpha-content-1.0.0")
        first = self.registry.register_batch("batch-1", [old])
        self.assertTrue(first.success)

        # 第二批：复用 proj-a 1.0.0 + 新增 proj-a 1.1.0 与 proj-b 2.0.0
        reused = ArtifactSpec("proj-a", "1.0.0", old.sha256, self.src_dir / "gone.bin")
        new_a_content = b"alpha-content-1.1.0"
        new_b_content = b"beta-content-2.0.0"
        new_a = self.spec("proj-a", "1.1.0", "a-1.1.0.bin", new_a_content)
        new_b = self.spec("proj-b", "2.0.0", "b-2.0.0.bin", new_b_content)
        result = self.registry.register_batch("batch-2", [reused, new_a, new_b])
        self.assertTrue(result.success)
        by_version = {(r.project, r.version): r for r in result.artifacts}
        self.assertTrue(by_version[("proj-a", "1.0.0")].reused)
        self.assertFalse(by_version[("proj-a", "1.1.0")].reused)
        self.assertFalse(by_version[("proj-b", "2.0.0")].reused)

        # 删除全部源文件后，仍能逐字节一致地读回
        shutil.rmtree(self.src_dir)
        self.assertEqual(self.registry.read_artifact("proj-a", "1.0.0"), b"alpha-content-1.0.0")
        self.assertEqual(self.registry.read_artifact("proj-a", "1.1.0"), new_a_content)
        self.assertEqual(self.registry.read_artifact("proj-b", "2.0.0"), new_b_content)

        # 调整清单顺序、换掉源路径，用相同批次号重试：返回原结果
        reordered = [
            ArtifactSpec("proj-b", "2.0.0", new_b.sha256, Path("/nonexistent/b")),
            ArtifactSpec("proj-a", "1.1.0", new_a.sha256, Path("/nonexistent/a")),
            ArtifactSpec("proj-a", "1.0.0", old.sha256, Path("/nonexistent/old")),
        ]
        retry = self.registry.register_batch("batch-2", reordered)
        self.assertEqual(retry, result)

        # 查询批次结果与直接登记返回一致
        self.assertEqual(self.registry.get_batch_result("batch-2"), result)


class TestConflictRollback(RegistryTestCase):
    def test_conflict_makes_whole_batch_invisible(self) -> None:
        original = self.spec("proj-a", "1.0.0", "a.bin", b"original-content")
        self.assertTrue(self.registry.register_batch("batch-1", [original]).success)

        # 新批：proj-a 1.0.0 声明了不同摘要（内容冲突）+ 两件全新制品
        conflict_path, conflict_digest = self.make_source("a-evil.bin", b"tampered-content")
        conflict = ArtifactSpec("proj-a", "1.0.0", conflict_digest, conflict_path)
        fresh_a = self.spec("proj-a", "2.0.0", "a-2.bin", b"new-alpha")
        fresh_b = self.spec("proj-b", "1.0.0", "b-1.bin", b"new-beta")
        result = self.registry.register_batch("batch-2", [conflict, fresh_a, fresh_b])
        self.assertFalse(result.success)
        self.assertIn("冲突", result.error or "")

        # 整批新增内容均不可见
        with self.assertRaises(ArtifactNotFoundError):
            self.registry.read_artifact("proj-a", "2.0.0")
        with self.assertRaises(ArtifactNotFoundError):
            self.registry.read_artifact("proj-b", "1.0.0")
        # 旧内容不受影响
        self.assertEqual(self.registry.read_artifact("proj-a", "1.0.0"), b"original-content")
        # 失败结果可查询
        self.assertEqual(self.registry.get_batch_result("batch-2"), result)


class TestBatchIdentity(RegistryTestCase):
    def test_same_batch_id_with_different_manifest_rejected(self) -> None:
        one = self.spec("proj-a", "1.0.0", "a.bin", b"content-a")
        self.assertTrue(self.registry.register_batch("batch-1", [one]).success)

        other = self.spec("proj-a", "1.0.1", "a2.bin", b"content-a2")
        with self.assertRaises(BatchManifestMismatchError):
            self.registry.register_batch("batch-1", [one, other])

    def test_unknown_batch_result_is_none(self) -> None:
        self.assertIsNone(self.registry.get_batch_result("no-such-batch"))

    def test_failed_batch_can_be_retried_with_same_manifest(self) -> None:
        digest = _sha256(b"content-a")
        missing = ArtifactSpec("proj-a", "1.0.0", digest, self.src_dir / "missing.bin")
        failed = self.registry.register_batch("batch-1", [missing])
        self.assertFalse(failed.success)

        (self.src_dir / "missing.bin").write_bytes(b"content-a")
        fixed = ArtifactSpec("proj-a", "1.0.0", digest, self.src_dir / "missing.bin")
        retry = self.registry.register_batch("batch-1", [fixed])
        self.assertTrue(retry.success)
        self.assertEqual(self.registry.read_artifact("proj-a", "1.0.0"), b"content-a")


class TestValidation(RegistryTestCase):
    def test_declared_digest_mismatch_fails_batch(self) -> None:
        path, _ = self.make_source("a.bin", b"real-content")
        spec = ArtifactSpec("proj-a", "1.0.0", _sha256(b"declared-other"), path)
        result = self.registry.register_batch("batch-1", [spec])
        self.assertFalse(result.success)
        with self.assertRaises(ArtifactNotFoundError):
            self.registry.read_artifact("proj-a", "1.0.0")

    def test_duplicate_project_version_in_batch_fails(self) -> None:
        one = self.spec("proj-a", "1.0.0", "a.bin", b"content-a")
        two = self.spec("proj-a", "1.0.0", "b.bin", b"content-b")
        result = self.registry.register_batch("batch-1", [one, two])
        self.assertFalse(result.success)

    def test_invalid_version_fails(self) -> None:
        spec = ArtifactSpec("proj-a", "1.0", _sha256(b"x"), self.src_dir / "x.bin")
        result = self.registry.register_batch("batch-1", [spec])
        self.assertFalse(result.success)


class TestReadIntegrity(RegistryTestCase):
    def test_corrupted_content_is_not_a_successful_read(self) -> None:
        spec = self.spec("proj-a", "1.0.0", "a.bin", b"content-a")
        self.assertTrue(self.registry.register_batch("batch-1", [spec]).success)
        content_file = self._tmp / "repo" / "content" / spec.sha256
        content_file.write_bytes(b"corrupted")
        with self.assertRaises(ArtifactIntegrityError):
            self.registry.read_artifact("proj-a", "1.0.0")

    def test_missing_content_is_not_a_successful_read(self) -> None:
        spec = self.spec("proj-a", "1.0.0", "a.bin", b"content-a")
        self.assertTrue(self.registry.register_batch("batch-1", [spec]).success)
        (self._tmp / "repo" / "content" / spec.sha256).unlink()
        with self.assertRaises(ArtifactIntegrityError):
            self.registry.read_artifact("proj-a", "1.0.0")


if __name__ == "__main__":
    unittest.main()
