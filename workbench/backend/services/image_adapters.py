"""图片模型适配器注册表：把「参数 → API 请求 → 结果解析」按模型族隔离。

扩展方式
--------
接入新模型族时：新建 ``BaseImageAdapter`` 子类并在 :data:`ADAPTERS` 列表
**头部**插入（顺序即匹配优先级），services / api / 前端零改动。

v1 内置 GPTImageAdapter（OpenAI 兼容 images API，gpt-image 族前缀匹配，
同时作为未知模型的兜底——该 API 形态是事实标准）。

尺寸计算（按比例）
------------------
短边基准 ``{"1K": 1024, "2K": 1440, "4K": 2048}``；长边 = 短边 × 长短比
（16 对齐）；横比输出 ``"{L}x{S}"``，竖比 ``"{S}x{L}"``，1:1 输出正方形。
对齐参考站实测：2K·2:3 → 1440x2160，1K·2:3 → 1024x1536。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import gcd

from .errors import InvalidOperationError

# ─────────────────────────── 尺寸计算 ───────────────────────────

BASE_RESOLUTIONS: dict[str, int] = {"1K": 1024, "2K": 1440, "4K": 2048}
RATIOS: tuple[str, ...] = ("1:1", "3:2", "2:3", "16:9", "9:16", "4:3", "3:4", "21:9")

_ALIGN = 16  # 边长对齐粒度


def _round_align(value: float, align: int = _ALIGN) -> int:
    return max(align, int(round(value / align)) * align)


def parse_ratio(ratio: str) -> tuple[int, int]:
    """解析 "w:h" 为约分后的整数对；非法输入抛错。"""
    text = (ratio or "").strip().lower()
    if ":" not in text:
        raise InvalidOperationError(f"比例格式应为 w:h，当前：{ratio!r}")
    left, _, right = text.partition(":")
    try:
        w, h = int(left.strip()), int(right.strip())
    except ValueError as exc:
        raise InvalidOperationError(f"比例必须为整数比，当前：{ratio!r}") from exc
    if w <= 0 or h <= 0:
        raise InvalidOperationError(f"比例必须为正数，当前：{ratio!r}")
    divisor = gcd(w, h)
    return w // divisor, h // divisor


def calc_size(base: str, ratio: str) -> str:
    """按 基准分辨率 × 比例 计算最终尺寸串（"WxH"）。"""
    if base not in BASE_RESOLUTIONS:
        raise InvalidOperationError(
            f"基准分辨率仅支持 {sorted(BASE_RESOLUTIONS)}，当前：{base!r}"
        )
    short = BASE_RESOLUTIONS[base]
    w, h = parse_ratio(ratio)
    if w == h:
        return f"{short}x{short}"
    big, small = max(w, h), min(w, h)
    long_side = _round_align(short * big / small)
    return f"{long_side}x{short}" if w > h else f"{short}x{long_side}"


def parse_size(size: str) -> tuple[int, int]:
    """解析 "WxH" 为整数对；非法输入抛错。"""
    text = (size or "").strip().lower()
    if "x" not in text:
        raise InvalidOperationError(f"尺寸格式应为 WxH，当前：{size!r}")
    left, _, right = text.partition("x")
    try:
        width, height = int(left.strip()), int(right.strip())
    except ValueError as exc:
        raise InvalidOperationError(f"尺寸必须为整数像素，当前：{size!r}") from exc
    if width <= 0 or height <= 0:
        raise InvalidOperationError(f"尺寸必须为正数，当前：{size!r}")
    return width, height


# ─────────────────────────── 请求结构 ───────────────────────────


@dataclass
class ImageGenRequest:
    """一次生图请求（文生图或垫图编辑，由 input_images 是否为空区分）。"""

    prompt: str
    model_id: str
    size: str = "auto"                 # "1440x2160" / "auto"
    quality: str = "auto"              # auto|low|medium|high
    output_format: str = "png"         # png|jpeg|webp
    background: str = "auto"           # auto|transparent|opaque
    moderation: str = "auto"           # auto|low
    n: int = 1
    input_images: list[tuple[str, bytes]] = field(default_factory=list)  # (filename, bytes)

    @property
    def is_edit(self) -> bool:
        return bool(self.input_images)


# ─────────────────────────── 适配器基类 ───────────────────────────


class BaseImageAdapter:
    """图片模型适配器协议：能力声明 + 请求组装 + 响应解析。"""

    key: str = "base"

    @staticmethod
    def matches(model_id: str) -> bool:  # pragma: no cover - 接口约定
        raise NotImplementedError

    def capabilities(self) -> dict:
        raise NotImplementedError  # pragma: no cover - 接口约定

    def validate(self, req: ImageGenRequest) -> None:
        """按能力声明校验请求参数（超范围直接拒绝，由调用方给用户提示）。"""
        caps = self.capabilities()
        if req.is_edit and not caps.get("supports_edit"):
            raise InvalidOperationError(f"模型 {req.model_id} 不支持垫图/图片编辑")
        if req.quality not in caps.get("qualities", []):
            raise InvalidOperationError(
                f"质量 {req.quality!r} 不受支持，可选：{caps.get('qualities')}"
            )
        if req.output_format not in caps.get("formats", []):
            raise InvalidOperationError(
                f"格式 {req.output_format!r} 不受支持，可选：{caps.get('formats')}"
            )
        if req.size != "auto":
            width, height = parse_size(req.size)
            min_side = int(caps.get("min_side", 64))
            max_side = int(caps.get("max_side", 4096))
            if not (min_side <= width <= max_side and min_side <= height <= max_side):
                raise InvalidOperationError(
                    f"尺寸 {width}x{height} 超出模型范围（边长 {min_side}-{max_side}）"
                )
        if req.n > int(caps.get("max_n", 1)):
            raise InvalidOperationError(
                f"数量 {req.n} 超出模型单次上限 {caps.get('max_n')}"
            )

    def build_request(
        self, base_url: str, req: ImageGenRequest
    ) -> tuple[str, dict | None, list[dict] | None]:
        """组装 HTTP 请求：返回 (url, json_payload, multipart_parts)。

        json_payload 与 multipart_parts 二选一；multipart 每项形如
        ``{"name": "image[]", "filename": ..., "content": bytes, "content_type": ...}``，
        另含 ``"data": {...}`` 字段表示表单文本字段。
        """
        raise NotImplementedError  # pragma: no cover - 接口约定

    def parse_response(self, data: dict) -> list[dict]:
        """解析响应：返回 ``[{"b64": ...} | {"url": ...}, ...]``。"""
        raise NotImplementedError  # pragma: no cover - 接口约定


# ─────────────────────────── gpt-image 族 ───────────────────────────


class GPTImageAdapter(BaseImageAdapter):
    """OpenAI 兼容 images API（gpt-image 族；未知模型的兜底适配器）。

    - 文生图：``POST {base_url}/images/generations``（JSON）；
    - 垫图/编辑：``POST {base_url}/images/edits``（multipart，image[] 支持多张）；
    - 响应：``data[]``，gpt-image 族恒为 ``b64_json``，兼容 ``url``（由服务层下载）。
    """

    key = "gpt-image"

    @staticmethod
    def matches(model_id: str) -> bool:
        return model_id.lower().startswith("gpt-image")

    def capabilities(self) -> dict:
        return {
            "adapter_key": self.key,
            "size_mode": "arbitrary",
            "base_resolutions": sorted(BASE_RESOLUTIONS),
            "ratios": list(RATIOS),
            "qualities": ["auto", "low", "medium", "high"],
            "formats": ["png", "jpeg", "webp"],
            "background": True,
            "moderations": ["auto", "low"],
            "max_n": 4,
            "supports_edit": True,
            "min_side": 256,
            "max_side": 4096,
            # UI「透明背景」布尔 → API 枚举（未来模型可覆盖）
            "background_map": {"false": "auto", "true": "transparent"},
        }

    def build_request(
        self, base_url: str, req: ImageGenRequest
    ) -> tuple[str, dict | None, list[dict] | None]:
        root = base_url.rstrip("/")
        if req.is_edit:
            url = f"{root}/images/edits"
            parts: list[dict] = []
            for index, (filename, content) in enumerate(req.input_images):
                parts.append(
                    {
                        "name": "image[]",
                        "filename": filename or f"reference-{index + 1}.png",
                        "content": content,
                        "content_type": "application/octet-stream",
                    }
                )
            parts.append(
                {
                    "name": "__data__",
                    "data": {
                        "model": req.model_id,
                        "prompt": req.prompt,
                        "n": str(req.n),
                        "size": req.size,
                        "quality": req.quality,
                        "output_format": req.output_format,
                        "background": req.background,
                    },
                }
            )
            return url, None, parts

        payload: dict = {
            "model": req.model_id,
            "prompt": req.prompt,
            "n": req.n,
            "size": req.size,
            "quality": req.quality,
            "output_format": req.output_format,
            "background": req.background,
        }
        if req.moderation and req.moderation != "auto":
            payload["moderation"] = req.moderation
        else:
            payload["moderation"] = "auto"
        return f"{root}/images/generations", payload, None

    def parse_response(self, data: dict) -> list[dict]:
        items = data.get("data") or []
        results: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            b64 = item.get("b64_json")
            url = item.get("url")
            if b64:
                results.append({"b64": str(b64)})
            elif url:
                results.append({"url": str(url)})
        return results


# ─────────────────────────── 注册表 ───────────────────────────

# 顺序即匹配优先级：接入新模型族时插到列表头部。
ADAPTERS: list[type[BaseImageAdapter]] = [GPTImageAdapter]

# 兜底：OpenAI 兼容 images API 是事实标准，未知模型沿用 GPTImageAdapter 行为。
_FALLBACK_ADAPTER = GPTImageAdapter


def resolve_adapter(model_id: str) -> BaseImageAdapter:
    """按 model_id 匹配适配器；无命中时回落到 OpenAI 兼容兜底。"""
    for adapter_cls in ADAPTERS:
        if adapter_cls.matches(model_id):
            return adapter_cls()
    return _FALLBACK_ADAPTER()


__all__ = [
    "ADAPTERS",
    "BASE_RESOLUTIONS",
    "BaseImageAdapter",
    "GPTImageAdapter",
    "ImageGenRequest",
    "RATIOS",
    "calc_size",
    "parse_ratio",
    "parse_size",
    "resolve_adapter",
]
