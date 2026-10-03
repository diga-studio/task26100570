"""登记库公开数据类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ArtifactSpec:
    """批次清单中的一件制品声明。

    source_path 仅用于登记时读取内容，不属于批次身份。
    """

    project: str
    version: str  # major.minor.patch
    sha256: str  # 声明的内容摘要（十六进制小写）
    source_path: Path


@dataclass(frozen=True)
class ArtifactRecord:
    """批次结果中单件制品的登记结果。"""

    project: str
    version: str
    sha256: str
    reused: bool  # True 表示复用已登记内容，False 表示本批新增


@dataclass(frozen=True)
class BatchResult:
    """一次批次登记的结果。"""

    batch_id: str
    success: bool
    artifacts: tuple[ArtifactRecord, ...] = field(default_factory=tuple)
    error: str | None = None
