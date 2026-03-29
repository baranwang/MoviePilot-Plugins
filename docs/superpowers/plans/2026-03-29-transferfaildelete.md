# TransferFailDelete 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `TransferFailDelete` 插件，监听 MoviePilot v2 的 `TransferFailed` 事件，自动删除整理失败的源文件。

**Architecture:** 纯事件驱动，无定时任务。监听 `EventType.TransferFailed`，使用 `StorageChain().delete_media_file(fileitem)` 删除文件（与 UI「删除转移记录和源文件」逻辑一致），并清理下载记录、发送通知。

**Tech Stack:** Python, MoviePilot v2 插件基类 (`_PluginBase`), `StorageChain`, `DownloadHistoryOper`, `eventmanager`

---

## 文件结构

| 文件 | 操作 | 职责 |
|---|---|---|
| `plugins.v2/transferfaildelete/__init__.py` | 新建 | 插件主体：事件监听、文件删除、通知 |
| `package.v2.json` | 修改 | 注册新插件条目 |

---

### Task 1: 创建插件主体 `__init__.py`

**Files:**
- Create: `plugins.v2/transferfaildelete/__init__.py`

- [ ] **Step 1: 创建插件目录并写入完整插件代码**

```python
# plugins.v2/transferfaildelete/__init__.py
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.chain.storage import StorageChain
from app.core.event import eventmanager, Event
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType


class TransferFailDelete(_PluginBase):
    # 插件名称
    plugin_name = "整理失败源文件清理"
    # 插件描述
    plugin_desc = "媒体整理失败时自动删除源文件，与手动「删除转移记录和源文件」逻辑一致。"
    # 插件图标
    plugin_icon = "Eraser.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "baranwang"
    # 作者主页
    author_url = "https://github.com/baranwang"
    # 插件配置项 ID 前缀
    plugin_config_prefix = "transferfaildelete_"
    # 加载顺序
    plugin_order = 99
    # 可使用的用户级别
    auth_level = 2

    # 配置属性
    _enabled: bool = False
    _notify: bool = True

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", True)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "发送通知",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "warning",
                                            "variant": "tonal",
                                            "text": (
                                                "启用后，媒体整理失败时将自动删除源文件（不可恢复）。\n\n"
                                                "整理失败历史记录不会被删除，仍可在整理历史中查看。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
        }

    def get_page(self) -> List[dict]:
        return []

    @eventmanager.register(EventType.TransferFailed)
    def on_transfer_failed(self, event: Event):
        """
        整理失败时删除源文件
        """
        if not self._enabled:
            return

        event_data = event.event_data or {}
        fileitem = event_data.get("fileitem")
        if not fileitem:
            logger.warning("整理失败事件缺少 fileitem，跳过源文件清理")
            return

        transferinfo = event_data.get("transferinfo")
        download_hash = event_data.get("download_hash")
        fail_reason = transferinfo.message if transferinfo else "未知"

        logger.info(f"整理失败，准备删除源文件：{fileitem.path}，原因：{fail_reason}")

        result = StorageChain().delete_media_file(fileitem)

        if result:
            logger.info(f"源文件已删除：{fileitem.path}")

            # 清理下载记录
            if download_hash:
                try:
                    DownloadHistoryOper().delete_file_by_fullpath(
                        Path(fileitem.path).as_posix()
                    )
                    eventmanager.send_event(
                        EventType.DownloadFileDeleted,
                        {"src": fileitem.path, "hash": download_hash},
                    )
                except Exception as e:
                    logger.error(f"清理下载记录失败：{e}")

            if self._notify:
                self.post_message(
                    mtype=NotificationType.Manual,
                    title="整理失败源文件已删除",
                    text=f"文件：{fileitem.path}\n原因：{fail_reason}",
                )
        else:
            logger.error(f"源文件删除失败：{fileitem.path}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Manual,
                    title="整理失败源文件删除失败",
                    text=f"文件：{fileitem.path}\n请手动处理",
                )

    def stop_service(self):
        pass
```

- [ ] **Step 2: 语法检查（AST + 导入检查）**

先做 AST 解析（检查语法）：
```bash
python -c "import ast; ast.parse(open('plugins.v2/transferfaildelete/__init__.py').read()); print('AST OK')"
```

期望输出：`AST OK`

如果报语法错误则修复后再继续。

- [ ] **Step 3: 提交**

```bash
git add plugins.v2/transferfaildelete/__init__.py
git commit -m "feat(transferfaildelete): 新增整理失败源文件清理插件 v1.0.0"
```

---

### Task 2: 注册到 `package.v2.json`

**Files:**
- Modify: `package.v2.json`

- [ ] **Step 1: 在 `package.v2.json` 末尾的 `}` 前添加新条目**

在现有最后一个条目（`RssDownload`）之后添加：

```json
  "TransferFailDelete": {
    "name": "整理失败源文件清理",
    "description": "媒体整理失败时自动删除源文件，与手动「删除转移记录和源文件」逻辑一致",
    "labels": "整理,清理,源文件",
    "version": "1.0.0",
    "icon": "Eraser.png",
    "author": "baranwang",
    "level": 2,
    "history": {
      "v1.0.0": "初始版本：监听 TransferFailed 事件，整理失败时自动删除源文件"
    }
  }
```

注意：`package.v2.json` 是标准 JSON，确保最后一个已有条目结尾加逗号，新条目不加逗号。

- [ ] **Step 2: 验证 JSON 合法性**

```bash
python -c "import json; json.load(open('package.v2.json')); print('OK')"
```

期望输出：`OK`

- [ ] **Step 3: 验证新条目的 version 与插件代码一致**

```bash
python -c "
import json, ast, re
pkg = json.load(open('package.v2.json'))
ver_pkg = pkg['TransferFailDelete']['version']
src = open('plugins.v2/transferfaildelete/__init__.py').read()
tree = ast.parse(src)
ver_code = None
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef) and node.name == 'TransferFailDelete':
        for item in node.body:
            if isinstance(item, ast.Assign):
                for t in item.targets:
                    if isinstance(t, ast.Name) and t.id == 'plugin_version':
                        ver_code = ast.literal_eval(item.value)
print(f'package.v2.json: {ver_pkg}')
print(f'plugin_version:  {ver_code}')
print('OK' if ver_pkg == ver_code else 'MISMATCH')
"
```

期望输出：两个版本均为 `1.0.0`，最后一行为 `OK`。

- [ ] **Step 4: 提交**

```bash
git add package.v2.json
git commit -m "feat(transferfaildelete): 注册插件到 package.v2.json"
```
