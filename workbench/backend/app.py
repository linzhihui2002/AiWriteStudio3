"""FastAPI 应用：应用工厂 + 可直接运行的入口。

运行方式：
    python -m workbench.backend.app
"""

from __future__ import annotations

import socket
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from . import config, db
from .api.content import router as content_router
from .api.images import router as images_router
from .api.library import router as library_router
from .api.models import router as models_router
from .api.orchestration import router as orchestration_router
from .api.projects import router as projects_router
from .api.writing import router as writing_router
from .services.errors import ServiceError

VERSION = config.VERSION

# Vite 开发服务器
DEV_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
]


def check_port_available(host: str, port: int) -> None:
    """启动前探测端口：被占用则抛错（绝不静默顺延端口）。

    注意：不设置 SO_REUSEADDR —— 在 Windows 上该选项会允许绑定到已占用端口，
    从而使探测失效。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError as exc:
            raise RuntimeError(
                f"端口 {port} 已被占用，工作台拒绝启动（不会自动顺延端口）。"
                f"请释放该端口，或通过环境变量 {config.PORT_ENV_VAR} 指定其他端口。"
            ) from exc


def run_startup_selfcheck() -> dict:
    """启动自检：确认代码库不存在裸 `dsh` 调用（隔离红线 2）。

    只做检测与告警，不阻断启动（详见 ``workbench/cli/check_no_bare_dsh.py``）。
    """
    result: dict = {"bare_dsh_ok": True, "violations": []}
    try:
        import importlib.util

        checker_path = config.WORKBENCH_DIR / "cli" / "check_no_bare_dsh.py"
        spec = importlib.util.spec_from_file_location("check_no_bare_dsh", checker_path)
        if spec is None or spec.loader is None:
            return result
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        iter_files = getattr(module, "iter_source_files", None)
        check_file = getattr(module, "check_file", None)
        if callable(iter_files) and callable(check_file):
            violations = []
            for path in iter_files():
                violations.extend(str(item) for item in check_file(path))
            result["violations"] = violations
            result["bare_dsh_ok"] = not violations
    except Exception as exc:  # noqa: BLE001 - 自检失败不影响启动
        result["bare_dsh_ok"] = False
        result["error"] = str(exc)
    return result


def _mount_frontend(app: FastAPI) -> None:
    """把 `workbench/frontend/dist` 挂载为根路径静态站点（SPA 回退 index.html）。

    构建产物尚不存在时直接跳过，不影响后端启动。
    """
    dist = config.FRONTEND_DIST_DIR
    if not dist.is_dir():
        return

    dist_root = dist.resolve()
    index_file = dist / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        # API 路径不参与 SPA 回退，避免掩盖 404
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")

        candidate = (dist_root / full_path).resolve()
        if full_path and candidate.is_file() and dist_root in candidate.parents:
            return FileResponse(candidate)
        if index_file.is_file():
            return FileResponse(index_file)
        raise HTTPException(status_code=404, detail="前端构建产物缺少 index.html")


def _vendor_dsh_ready() -> bool:
    """vendor 内置 dsh 是否就绪（只读探测，不影响启动）。"""
    try:
        from .engine.dsh_paths import is_vendor_dsh_installed

        return is_vendor_dsh_installed()
    except Exception:  # noqa: BLE001
        return False


@asynccontextmanager
async def _lifespan(app: FastAPI):
    config.ensure_runtime_dirs()
    db.init_db()
    # Repair an older or interrupted provider projection before native workers
    # are started. The source of truth remains the workbench provider database.
    from .services import provider_service
    provider_service.project_to_dsh_home()
    app.state.selfcheck = run_startup_selfcheck()
    from .services import chat_run_service
    chat_run_service.recover_interrupted()
    yield
    chat_run_service.shutdown()


def create_app() -> FastAPI:
    """应用工厂。"""
    port = config.resolve_port()

    app = FastAPI(
        title="AI 小说创作工作台后端",
        version=VERSION,
        lifespan=_lifespan,
    )
    app.state.port = port

    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(ServiceError)
    async def service_error_handler(_request, exc: ServiceError) -> JSONResponse:
        """服务层异常 → 统一 JSON 错误响应（400/404/409）。"""
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(FileNotFoundError)
    async def file_not_found_handler(_request, exc: FileNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/api/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": VERSION,
            "port": app.state.port,
            "db_ready": db.is_ready(),
            "fts": db.fts_available(),
            "vendor_dsh": _vendor_dsh_ready(),
        }

    # 业务路由（项目 / 文档树 / 章节；模板 / 索引 / 快照 / 导入导出 / 冲突 / 设置）
    app.include_router(projects_router)
    app.include_router(library_router)
    app.include_router(models_router)
    app.include_router(writing_router)
    app.include_router(content_router)
    app.include_router(orchestration_router)
    app.include_router(images_router)

    # 静态托管必须最后注册（catch-all 路由）
    _mount_frontend(app)

    return app


def main() -> int:
    """命令行入口：校验配置 → 探测端口 → 初始化运行时 → 启动 uvicorn。"""
    try:
        port = config.resolve_port()
    except (ValueError, RuntimeError) as exc:
        print(f"[workbench] 配置错误：{exc}", file=sys.stderr)
        return 1

    try:
        check_port_available(config.HOST, port)
    except RuntimeError as exc:
        print(f"[workbench] 启动失败：{exc}", file=sys.stderr)
        return 1

    config.ensure_runtime_dirs()
    db.init_db()

    import uvicorn

    print(f"[workbench] 启动中：http://{config.HOST}:{port}")
    uvicorn.run(create_app(), host=config.HOST, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
