# 项目知识库

**Generated:** 2026-02-17
**Commit:** 9655023
**Branch:** main

## 语言

思考和回复应始终使用中文。

## 概览

MoviePilot V2 插件仓库，当前包含 `qbsmartqueue` 插件 —— 基于下载体积动态管理 qBittorrent 队列。

## 结构

```
./
├── package.v2.json                      # V2 插件市场注册定义
└── plugins.v2/
    └── qbsmartqueue/
        ├── __init__.py                  # 插件主体（~700 行）
        └── README.md                    # 插件使用说明
```

## 查找指南

| 任务             | 位置                                                                  | 备注                                  |
| ---------------- | --------------------------------------------------------------------- | ------------------------------------- |
| 插件核心调度逻辑 | `plugins.v2/qbsmartqueue/__init__.py` → `_manage_single_downloader()` | 8 步流程                              |
| 配置表单         | 同文件 → `get_form()`                                                 | Vuetify 组件 JSON                     |
| 插件注册信息     | `package.v2.json`                                                     | name / description / version / labels |

## 代码地图

| 符号                          | 类型     | 位置              | 作用                                 |
| ----------------------------- | -------- | ----------------- | ------------------------------------ |
| `QbSmartQueue`                | class    | `__init__.py:24`  | 插件主类，继承 `_PluginBase`         |
| `service_infos`               | property | `__init__.py:144` | 获取已配置的 qBittorrent 下载器实例  |
| `manage_queue()`              | method   | `__init__.py:199` | 入口：遍历所有下载器执行调度         |
| `_manage_single_downloader()` | method   | `__init__.py:214` | 核心：磁盘检查→分类→溢出→放行→防死锁 |
| `on_download_added()`         | method   | `__init__.py:189` | 事件驱动：`DownloadAdded` 触发       |
| `get_form()`                  | method   | `__init__.py:442` | 配置表单定义                         |

## 约定

### 文案规范

- 中西文之间**必须加空格**：`管理 qBittorrent 队列` ✓，`管理qBittorrent队列` ✗
- 数字与单位之间加空格：`35 GB` ✓，`35GB` ✗
- qBittorrent 使用完整拼写，不缩写为 QB / qb

### V2 插件规范

- 插件代码放在 `plugins.v2/` 目录下
- 使用 `DownloaderHelper` 服务帮助类，不直接实例化下载器
- 插件注册放在根目录 `package.v2.json`
- 下载器选择通过 `DownloaderHelper().get_configs()` 动态获取
- 下载目录通过 `DirectoryHelper().get_local_download_dirs()` 动态获取
- 表单使用 `VCronField` 替代纯文本 cron 输入

### 版本与日志规范（强制）

- 每次修改任意插件（`plugins.v2/<plugin>/`）代码或行为时，必须同步更新该插件 `__init__.py` 中的 `plugin_version`
- 每次发布插件改动时，必须同步更新 `package.v2.json` 对应条目的 `version` 与 `history`
- `history` 必须新增当前版本的变更说明，不允许只改代码不记日志
- 提交前必须检查 `plugin_version` 与 `package.v2.json` 的 `version` 完全一致

### qBittorrent 种子状态

- 活跃下载：`downloading`, `stalledDL`, `metaDL`, `checkingDL`, `forcedDL`, `allocating`
- 待调度（暂停）：`pausedDL`, `stoppedDL`, `queuedDL`
- 非下载状态一律忽略

### 磁盘空间检测

- 通过 `SystemUtils.free_space(Path)` → `psutil.disk_usage` 检测挂载点级别剩余空间
- 按种子 `save_path` 匹配监控目录，仅暂停对应目录的种子，不全局暂停

## 反模式

- **禁止**：直接操作下载器实例（V1 模式），必须走 `DownloaderHelper`
- **禁止**：全局暂停所有种子 —— 磁盘空间不足时只暂停受影响目录的种子
- **禁止**：缩写 qBittorrent 为 QB/qb（用户可见文案中）

## 参考文档

- [V2 插件开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/V2_Plugin_Development.md) — 插件结构、基类方法、表单组件、事件系统等

## 依赖

| 依赖               | 来源            | 用途                     |
| ------------------ | --------------- | ------------------------ |
| `_PluginBase`      | MoviePilot 核心 | 插件基类                 |
| `DownloaderHelper` | MoviePilot 核心 | V2 下载器服务发现        |
| `DirectoryHelper`  | MoviePilot 核心 | 目录管理（获取下载路径） |
| `SystemUtils`      | MoviePilot 核心 | 磁盘空间检测             |
| `APScheduler`      | 第三方          | 定时任务调度             |

## 命令

```bash
# 本地无法运行/测试 —— 依赖 MoviePilot 运行时环境
# AST 语法检查
python -c "import ast; ast.parse(open('plugins.v2/qbsmartqueue/__init__.py').read()); print('OK')"
```

## 注意事项

- LSP 会报大量 `Import could not be resolved` 错误 —— 正常，因为 `app.*` 模块来自 MoviePilot 核心，本地不存在
- 插件只支持 qBittorrent，因为依赖 qB 原生种子状态值和 `save_path` 字段（MP 统一抽象层没有这些）
- `package.v2.json` 的 key（`QbSmartQueue`）必须与插件类名一致
