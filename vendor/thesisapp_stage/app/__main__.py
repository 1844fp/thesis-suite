"""``python -m app`` 入口，等价于 ``python -m app.cli``。

存在的原因：``app\\cli.py`` 里有相对导入，直接 ``python app\\cli.py`` 会失败。
让 `-m` 成为唯一被文档化的调用方式，可以避免使用者踩这个坑。
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
