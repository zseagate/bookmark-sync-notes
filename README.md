# bookmark-sync-notes

> Edge 与 Firefox 书签**双向增量合并**（互补，不覆盖）的完整实践：可直接使用的脚本 + 踩坑记录。

很多人的书签散落在两三个浏览器里：Edge 里存了一批 AI 工具，Firefox 里存了一批开发文档。常见的做法是"导出 HTML → 另一个浏览器导入"，但导入是**全量叠加**，跑几次就满屏重复。本项目做的是按 URL 求差集，只补对方没有的，重复执行也不会产生副本。

## 效果

一次真实同步（Linux Mint，Edge 156 / Firefox 某 Nightly 版）：

| | Edge | Firefox |
|---|---|---|
| 同步前 | 220 条 | 215 条 |
| 补入 | +3 条 | +12 条 |
| 同步后 | 223 条 | 227 条 |

补完之后两边只差 Firefox 自带的 4 条官方默认书签（按需选择跳过）。所有书签保留原有文件夹结构，不会全部堆到根目录。

## 快速开始

```bash
# 0. 完全退出 Edge 和 Firefox
# 1. 先看差异，不写文件
python3 scripts/bookmark_sync.py \
    --edge-bookmarks ~/.config/microsoft-edge/Default/Bookmarks \
    --firefox-db ~/.config/mozilla/firefox/<你的profile>/places.sqlite \
    --dry-run

# 2. 确认无误后执行（会自动备份到当前目录）
python3 scripts/bookmark_sync.py \
    --edge-bookmarks ~/.config/microsoft-edge/Default/Bookmarks \
    --firefox-db ~/.config/mozilla/firefox/<你的profile>/places.sqlite
```

可选参数：

- `--skip-firefox-folders "/menu/Mozilla Firefox"`：不同步 Firefox 默认书签（默认开启）
- `--force`：浏览器仍在运行时强行写入（不推荐，仅用于副本演练）
- `--edge-bar-name 收藏夹栏`：仅用于提示，不影响逻辑

脚本没有第三方依赖，Python 3.8+ 即可。默认会在当前目录生成 `bookmark_sync_backup_<时间戳>/`，用完确认没问题再删。

## 工作原理

```
读取 Edge Bookmarks(JSON)          读取 Firefox places.sqlite
        │                                   │
        └────────── 按 URL 求差集 ───────────┘
                        │
        ┌───────────────┴───────────────┐
        ▼                               ▼
  写入 Firefox                    写入 Edge
  (moz_places / moz_bookmarks     (Bookmarks JSON 节点，
   / moz_origins，手算 url_hash)    UUID + Chromium 时间戳)
```

三个关键点：

1. **幂等**：每轮都重新读取两边现有 URL 集合，只处理差集，跑多少遍结果都一样。
2. **保留文件夹**：Edge 的 `收藏夹栏/AI工具与平台` 对应 Firefox 工具栏下的同名文件夹，缺失时才创建。
3. **安全**：检测浏览器进程 → 拒绝写入；写入前备份；Edge JSON 原子替换（临时文件 + rename）。

## 踩坑清单

真正花时间的不是"合并"，而是下面这些坑，详细复盘见 [docs/pitfalls.md](docs/pitfalls.md)：

| # | 坑 | 一句话结论 |
|---|---|---|
| 1 | Firefox profile 位置 | Linux 上在 `~/.config/mozilla/firefox/`，不是 `~/.mozilla/`；多 profile 要先确认哪个在用 |
| 2 | 浏览器运行中写文件 | 会被内存状态覆盖，必须先完全退出 |
| 3 | `pkill -f msedge` | 会把自己这条命令也杀掉（命令行里含匹配串） |
| 4 | 运行中的 places.sqlite | 只读也会 `database is locked`，把 sqlite + wal + shm 一起复制出来读 |
| 5 | **url_hash 不是 CRC64** | 是 mfbt 的 golden-ratio 哈希，48 位打包，只取前 1500 字符 |
| 6 | 新版 places.sqlite 没有触发器 | `foreign_count`、`moz_origins/origin_id`、`recalc_frecency` 都要手工维护 |
| 7 | 时间戳单位 | Firefox 用微秒，Chromium 用"1601 年起的 100ns"，差 10 倍就是差 51 年 |
| 8 | SQL 占位符错位 | 列数、参数顺序必须逐一对齐，先在副本上演练 |
| 9 | 文件夹路径拼接 | 根节点标题为空会拼出 `//toolbar/...`，导致跳过多余文件夹误判 |
| 10 | 备份时机 | 备份要在浏览器关闭之后做，否则 WAL 里可能还有未落盘的数据 |

其中第 5 条是整件事的核心难点，网上不少文章说 `url_hash` 是 CRC64，实测**六种 CRC64 变体全部 0 匹配**。正确的公式与验证方法都写在 [docs/pitfalls.md](docs/pitfalls.md) 里，并用库内 1433 条已有记录验证到 1433/1433。

## 目录结构

```
.
├── README.md
├── LICENSE
├── scripts/
│   └── bookmark_sync.py      # 通用同步脚本（双向、幂等、自动备份）
└── docs/
    └── pitfalls.md           # 10 个坑的详细复盘与代码
```

## 安全提醒

- **一定要先备份**。脚本会备份，但如果你手动改库，请自己复制 `places.sqlite`（连同 `-wal`、`-shm`）和 Edge 的 `Bookmarks`。
- 直接写 `places.sqlite` 属于"非官方支持"的操作，Firefox 版本升级后表结构可能变化；脚本只针对当前 schema，升级后请先用副本验证。
- 同步前请确认目标 profile，避免写进不用的 profile。
- 如果 Firefox 开了同步（Sync），建议先关闭同步再操作，避免服务端状态把本地改动覆盖。

## 验证方法

同步结束后建议跑一遍：

```bash
# Firefox 库完整性
python3 -c "import sqlite3;print(sqlite3.connect('PLACES').execute('PRAGMA integrity_check').fetchone()[0])"

# 不允许存在 url_hash 为 0 的行（Firefox 启动时会断言）
python3 -c "import sqlite3;print(sqlite3.connect('PLACES').execute('select count(*) from moz_places where url_hash=0').fetchone()[0])"

# 再跑一次 --dry-run，应该显示 0 变化
```

## License

MIT
