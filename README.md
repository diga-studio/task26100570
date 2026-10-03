# task26100570

制品清单登记库：发布流水线在制品进入 build 阶段时批量登记。元数据存 SQLite，制品内容按 SHA-256 寻址存本地目录，纯标准库实现，无外部服务依赖。

## 使用

```python
from artifact_registry import ArtifactRegistry, ArtifactSpec

registry = ArtifactRegistry("/path/to/registry")  # 自动创建 metadata.sqlite3 与 content/

result = registry.register_batch("batch-42", [
    ArtifactSpec(project="app", version="1.2.0",
                 sha256="<声明的64位小写十六进制摘要>",
                 source_path="/build/work/app-1.2.0.tar"),
    # ...一批多件
])
if result.success:
    for a in result.artifacts:
        print(a.project, a.version, "复用" if a.reused else "新增")
else:
    print("整批失败:", result.detail)

data = registry.read_artifact("app", "1.2.0")  # 读回时做 SHA-256 核验
registry.close()
```

## 语义

- 批次身份 = 批内（项目, 版本, 声明摘要）的集合；清单顺序与源路径不影响身份。
- 同一批次号重复提交相同清单（含失败批次）返回原结果，不依赖源文件仍存在；换清单抛 `BatchManifestMismatch`。
- 同一项目同一版本只对应一种内容：摘要相同直接复用，不同则整批失败，旧内容不受影响，本批新增不可见。
- 新增内容先复制进库（临时文件 + 原子改名），元数据单事务提交，一批制品要么全部可见要么全部不可见。
- 登记后删除源文件仍可读回；读回必过 SHA-256 核验，内容缺失或损坏抛 `ArtifactContentCorrupt`。

## 测试

```sh
python3 -m unittest discover -s tests -v
```
