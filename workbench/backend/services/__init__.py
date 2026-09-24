"""业务服务层：项目 / 章节 / 文档树。

约定
----
- 事实源永远是磁盘（`projects/{书名}/` 下的 Markdown），SQLite 只是可重建的索引；
- 所有文件写入一律走 :func:`workbench.backend.services.fs_utils.atomic_write_text`（临时文件 + rename）；
- 目录定位一律在调用时读取 ``config.PROJECTS_DIR`` / ``config.RUNTIME_DIR``，
  便于测试用 monkeypatch 指向临时目录（绝不污染真实 ``projects/``）。
"""
