"""服务层异常：统一携带 HTTP 状态码，供 API 层映射为响应。"""

from __future__ import annotations


class ServiceError(Exception):
    """服务层异常基类。"""

    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ProjectNotFoundError(ServiceError):
    status_code = 404


class ProjectExistsError(ServiceError):
    status_code = 409


class InvalidNameError(ServiceError):
    status_code = 400


class NodeNotFoundError(ServiceError):
    status_code = 404


class NodeExistsError(ServiceError):
    status_code = 409


class InvalidChapterNameError(ServiceError):
    status_code = 400


class InvalidOperationError(ServiceError):
    status_code = 400
