"""写作门禁集合（gates）。

三个门禁均由 MIT 项目引入并改造，来源与许可证声明见各模块文件头，
引入登记见 ``docs/third-party-notices.md``：

* :mod:`workbench.backend.gates.density_gate` —— 去 AI 味密度红线
  （来源：huahuakandaima__human-novel-writer）
* :mod:`workbench.backend.gates.prose_gate` —— 中文成稿硬禁令与模型化形状
  （来源：huahuakandaima__human-novel-writer）
* :mod:`workbench.backend.gates.language_gate` —— 语言门（HTML 阻断 + 外语泄漏）
  （来源：qin1473692580-ux__oh-story-claudecode）

统一调用接口：``run(text, ...) -> 结果对象``，结果对象均有 ``passed`` 与
``report()``。CLI 入口保留在各模块内。
"""

from __future__ import annotations

from workbench.backend.gates import density_gate, language_gate, prose_gate

__all__ = ["density_gate", "language_gate", "prose_gate"]
