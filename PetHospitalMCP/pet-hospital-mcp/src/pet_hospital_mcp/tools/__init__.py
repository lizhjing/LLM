"""MCP 工具集合。

后续阶段新增工具时，只需在本包下增加新模块（复用 ``rest_client``、
``logging_config``、``errors`` 的约定），并在 server 装配处注册即可。
"""

from __future__ import annotations
