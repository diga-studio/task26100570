# 制品清单登记库（artifact_registry）

由上层发布流水线调用的 Python 库：批量登记进入 build 阶段的制品、查询批次结果、按项目和版本读回制品。元数据存 SQLite，制品内容存本地目录（按 SHA-256 内容寻址），无任何外部服务依赖。

## 使用

```python
from pathlib import Path
from artifact_registry import Registry, ArtifactSpec

registry = Registry("/var/lib/my-registry")  # 内含 registry.db 与 content/

specs = [
    ArtifactSpec("proj-a", "1.0.0", "<声明的sha256>", Path("/build/proj-a-1.0.0.tar.gz")),
    ArtifactSpec("proj-b", "2.1.0", "<声明的sha256>", Path("/build/proj-b-2.1.0.whl")),
]
result = registry.register_batch("batch-20261004-01", specs)
if result.success:
    for rec in result.artifacts:
        print(rec.project, rec.version, "复用" if rec.reused else "新增")
else:
    print("整批失败：", result.error)

# 查询批次结果（未知批次号返回 None）
again = registry.get_batch_result("batch-20261004-01")

# 读回制品（读回时做 SHA-256 核验，缺失/损坏抛 ArtifactIntegrityError）
data = registry.read_artifact("proj-a", "1.0.0")
```

## 语义

- 批次身份 = 清单中（项目， 版本， 声明摘要）的集合；清单顺序与源路径不属于身份。同批次号换清单抛 `BatchManifestMismatchError`；已成功批次的相同清单重试直接返回原结果，不依赖源文件仍存在。
- 同一（项目， 版本）只对应一种内容：摘要相同直接复用，不同则整批失败，不覆盖旧内容、不留下本批新增的可见制品。
- 一批新增制品全部可见或全部不可见：内容先复制进库（内容寻址），元数据在单个 SQLite 事务中提交。
- 登记时实际计算源文件 SHA-256，与声明不一致则整批失败。

## 运行测试

```sh
python3 -m unittest discover -s tests -v
```
