"""登记库公开数据类型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class ArtifactSpec:
    """清单中的一件制品声明。"""

    project: str
    version: str  # major.minor.patch
    sha256: str   # 声明的内容摘要（十六进制小写）
    source_path: str  # 本地源文件路径，不属于批次身份


@dataclass(frozen=True)
class ArtifactOutcome:
    """批次成功后单件制品的登记结果。"""

    project: str
    version: str
    sha256: str
    reused: bool  # True 表示复用已登记内容，False 表示本批新增


@dataclass(frozen=True)
class BatchResult:
    """批次登记结果。success 为 False 时 detail 说明失败原因。"""

    batch_id: str
    success: bool
    artifacts: List[ArtifactOutcome]
    detail: str = ""
