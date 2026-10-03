"""登记库异常类型定义。"""


class RegistryError(Exception):
    """登记库所有异常的基类。"""


class BatchManifestMismatch(RegistryError):
    """批次号已存在，但本次清单与已登记清单不一致。"""


class BatchRegistrationFailed(RegistryError):
    """批次登记失败（内容冲突、摘要不符、源文件缺失等），整批未生效。"""


class ArtifactNotFound(RegistryError):
    """请求的项目与版本组合未登记。"""


class ArtifactContentCorrupt(RegistryError):
    """库内制品内容缺失或摘要核验失败。"""
