"""登记库异常类型。"""


class RegistryError(Exception):
    """登记库所有错误的基类。"""


class BatchManifestMismatchError(RegistryError):
    """同一批次号提交了与已登记清单不同的清单。"""


class BatchRegistrationError(RegistryError):
    """批次登记失败（内容冲突、摘要不匹配、源文件缺失等）。"""


class ArtifactNotFoundError(RegistryError):
    """请求的项目/版本未登记。"""


class ArtifactIntegrityError(RegistryError):
    """库内制品内容缺失或损坏（SHA-256 核验失败）。"""
