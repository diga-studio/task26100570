"""制品清单登记库自动化测试。"""

import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artifact_registry import (
    ArtifactContentCorrupt,
    ArtifactNotFound,
    ArtifactRegistry,
    ArtifactSpec,
    BatchManifestMismatch,
)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RegistryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="registry-test-"))
        self.src_dir = self._tmp / "src"
        self.src_dir.mkdir()
        self.registry = ArtifactRegistry(self._tmp / "lib")

    def tearDown(self) -> None:
        self.registry.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def make_source(self, name: str, data: bytes) -> Path:
        path = self.src_dir / name
        path.write_bytes(data)
        return path

    def spec(self, project: str, version: str, path: Path) -> ArtifactSpec:
        return ArtifactSpec(
            project=project,
            version=version,
            sha256=sha256_of(path.read_bytes()),
            source_path=str(path),
        )


class MixedReuseAndNewTest(RegistryTestBase):
    """场景一：一批同时包含旧版本复用和新版本。"""

    def test_reuse_new_delete_sources_and_retry(self) -> None:
        old_data = b"old-version-content"
        new_data = b"new-version-content" * 4096

        old_path = self.make_source("old.bin", old_data)
        first = self.registry.register_batch(
            "batch-1", [self.spec("app", "1.0.0", old_path)]
        )
        self.assertTrue(first.success)

        new_path = self.make_source("new.bin", new_data)
        mixed = [
            self.spec("app", "1.0.0", old_path),  # 复用
            self.spec("app", "2.1.3", new_path),  # 新增
        ]
        second = self.registry.register_batch("batch-2", mixed)
        self.assertTrue(second.success)
        by_version = {a.version: a for a in second.artifacts}
        self.assertTrue(by_version["1.0.0"].reused)
        self.assertFalse(by_version["2.1.3"].reused)

        # 删除全部源文件后仍能逐字节一致地读回
        for path in self.src_dir.iterdir():
            path.unlink()
        self.assertEqual(self.registry.read_artifact("app", "1.0.0"), old_data)
        self.assertEqual(self.registry.read_artifact("app", "2.1.3"), new_data)

        # 调整清单顺序、换掉源路径再重试，返回原结果
        reordered = [
            ArtifactSpec("app", "2.1.3", mixed[1].sha256, "/gone/new.bin"),
            ArtifactSpec("app", "1.0.0", mixed[0].sha256, "/gone/old.bin"),
        ]
        retry = self.registry.register_batch("batch-2", reordered)
        self.assertEqual(retry, second)


class ConflictRollbackTest(RegistryTestBase):
    """场景二：批内一个版本内容冲突，整批新增不可见，旧内容不受影响。"""

    def test_conflict_rolls_back_whole_batch(self) -> None:
        original = b"original-content"
        original_path = self.make_source("orig.bin", original)
        first = self.registry.register_batch(
            "batch-1", [self.spec("app", "1.0.0", original_path)]
        )
        self.assertTrue(first.success)

        # 同一项目同一版本换了内容
        conflicting = b"different-content"
        conflict_path = self.make_source("conflict.bin", conflicting)
        fresh_data = b"brand-new-artifact"
        fresh_path = self.make_source("fresh.bin", fresh_data)
        batch = [
            ArtifactSpec("app", "1.0.0", sha256_of(conflicting), str(conflict_path)),
            self.spec("app", "9.9.9", fresh_path),
        ]
        failed = self.registry.register_batch("batch-2", batch)
        self.assertFalse(failed.success)
        self.assertIn("冲突", failed.detail)

        # 本批新增制品不可见
        with self.assertRaises(ArtifactNotFound):
            self.registry.read_artifact("app", "9.9.9")
        # 旧内容不受影响
        self.assertEqual(self.registry.read_artifact("app", "1.0.0"), original)

        # 相同清单重试失败批次，返回原失败结果，且不依赖源文件
        for path in self.src_dir.iterdir():
            path.unlink()
        retry = self.registry.register_batch("batch-2", list(reversed(batch)))
        self.assertEqual(retry, failed)


class BatchIdentityTest(RegistryTestBase):
    def test_same_batch_id_different_manifest_rejected(self) -> None:
        path = self.make_source("a.bin", b"data-a")
        self.registry.register_batch("batch-1", [self.spec("app", "1.0.0", path)])
        other = self.make_source("b.bin", b"data-b")
        with self.assertRaises(BatchManifestMismatch):
            self.registry.register_batch(
                "batch-1", [self.spec("app", "1.0.1", other)]
            )

    def test_declared_digest_mismatch_fails_batch(self) -> None:
        path = self.make_source("a.bin", b"real-data")
        fake_sha = sha256_of(b"not-the-real-data")
        result = self.registry.register_batch(
            "batch-1", [ArtifactSpec("app", "1.0.0", fake_sha, str(path))]
        )
        self.assertFalse(result.success)
        with self.assertRaises(ArtifactNotFound):
            self.registry.read_artifact("app", "1.0.0")

    def test_missing_source_fails_batch(self) -> None:
        result = self.registry.register_batch(
            "batch-1",
            [ArtifactSpec("app", "1.0.0", sha256_of(b"x"), "/no/such/file")],
        )
        self.assertFalse(result.success)

    def test_duplicate_project_version_in_batch_rejected(self) -> None:
        path = self.make_source("a.bin", b"data")
        spec = self.spec("app", "1.0.0", path)
        with self.assertRaises(ValueError):
            self.registry.register_batch("batch-1", [spec, spec])


class ReadVerificationTest(RegistryTestBase):
    def test_corrupt_content_detected_on_read(self) -> None:
        data = b"integrity-matters"
        path = self.make_source("a.bin", data)
        self.registry.register_batch("batch-1", [self.spec("app", "1.0.0", path)])
        content_file = self._tmp / "lib" / "content" / sha256_of(data)
        content_file.write_bytes(b"tampered")
        with self.assertRaises(ArtifactContentCorrupt):
            self.registry.read_artifact("app", "1.0.0")

    def test_missing_content_detected_on_read(self) -> None:
        data = b"will-be-lost"
        path = self.make_source("a.bin", data)
        self.registry.register_batch("batch-1", [self.spec("app", "1.0.0", path)])
        (self._tmp / "lib" / "content" / sha256_of(data)).unlink()
        with self.assertRaises(ArtifactContentCorrupt):
            self.registry.read_artifact("app", "1.0.0")

    def test_read_unregistered_raises_not_found(self) -> None:
        with self.assertRaises(ArtifactNotFound):
            self.registry.read_artifact("ghost", "0.0.1")


if __name__ == "__main__":
    unittest.main()
