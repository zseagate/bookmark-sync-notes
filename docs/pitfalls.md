# 踩坑复盘：Edge 与 Firefox 书签双向增量合并

本文记录一次真实同步中遇到的全部问题与解决办法。环境：Linux Mint 22.3，Edge 156（Chromium 内核），Firefox（places.sqlite schema 当前版本），Python 3.12。

目标：两边书签**互补**——A 有 B 没有的补过去，B 有 A 没有的补回来，不产生重复，不破坏原文件夹结构。

---

## 坑 1：Firefox profile 根本不在 `~/.mozilla/firefox`

**现象**：`ls ~/.mozilla` 不存在，但 `/usr/bin/firefox` 明明装好了，而且正在运行。

**原因**：Linux 上 Firefox 遵循 XDG 规范，配置放在 `~/.config/mozilla/firefox/`；只有 macOS/Windows 才是 `~/.mozilla`、`%APPDATA%` 那套。Linux Mint 上还可能存在 Snap/Flatpak 变体，路径又不一样。

**解决**：

```bash
find ~ -maxdepth 6 -name "places.sqlite" 2>/dev/null
# /home/<user>/.config/mozilla/firefox/acu5ld8t.default-release/places.sqlite
# /home/<user>/.config/mozilla/firefox/lxdk0zca.default-release-1/places.sqlite
```

找到多处后，**必须确认哪个是当前在用的 profile**，否则会把书签写进一个你根本不打开的 profile：

```bash
# 1) profiles.ini / installs.ini 里 Install 段的 Default
cat ~/.config/mozilla/firefox/installs.ini

# 2) 看 .parentlock 的修改时间（正在运行的 profile 会被锁）
ls -l ~/.config/mozilla/firefox/*/.parentlock

# 3) 最直接：看正在运行的进程打开了哪个文件
ls -l /proc/$(pgrep -x firefox | head -1)/fd | grep places.sqlite
```

本次 `installs.ini` 指向 `lxdk0zca.default-release-1`，`/proc/<pid>/fd` 也确认它打开了该目录下的 `places.sqlite-wal`，两个证据一致才动手。另一个 profile 只是残留的 WAL 文件，容易误导。

---

## 坑 2：浏览器运行中改书签文件 = 白改

**现象**：改完文件，重启浏览器后改动"消失"，或者出现书签重复/错乱。

**原因**：Chromium 系（Edge/Chrome）在内存里维护书签树，定时/退出时整棵写回 `Bookmarks`；Firefox 的 `places.sqlite` 有连接池和 WAL，外部修改会被内存快照覆盖，甚至触发修复逻辑。

**解决**：动手前完全退出两个浏览器，并用**精确进程名**确认：

```bash
pgrep -x firefox
pgrep -x msedge
```

脚本里也加了这道闸：检测到进程就直接退出，`--dry-run` 才允许浏览器开着（只读）。

---

## 坑 3：`pkill -f 'msedge'` 把自己杀了

**现象**：

```text
$ pkill -f '/opt/microsoft/msedge/msedge'; pkill -f '/usr/lib/firefox/firefox'; ...
Killed by SIGTERM
```

命令执行到一半，整个 shell 被 SIGTERM 干掉。

**原因**：`pkill -f` 匹配的是**完整命令行**，而当前这条 `bash -c "...pkill -f '/opt/microsoft/msedge/msedge'..."` 的命令行里正好含有这个字符串，于是 pkill 先把自己（甚至父 shell）匹配上了。

**解决**：用 `-x` 精确匹配进程名，或者把模式写成不会匹配自身的正则：

```bash
pkill -x firefox
pkill -x msedge
# 或者
pkill -f '[m]sedge'
```

`[m]sedge` 是经典技巧：正则本身不含连续的 `msedge` 字面量，不会匹配到自己。

---

## 坑 4：sqlite3 命令行不存在

**现象**：`sqlite3: 未找到命令`。

**解决**：不用装，Python 标准库自带：

```python
import sqlite3
con = sqlite3.connect(path)
```

脚本内所有读写都用 Python 完成，零依赖。

---

## 坑 5：运行中的 `places.sqlite` 只读也报 `database is locked`

**现象**：Firefox 在跑时，以只读模式打开数据库也会抛 `sqlite3.OperationalError: database is locked`。

**原因**：数据库是 WAL 模式。只读连接也需要写 `-shm` 共享内存文件来做 WAL 索引；文件权限/并发状态下打不开就报锁。加 `?mode=ro&immutable=1` 能绕过，但 `immutable` 会让 SQLite 忽略 WAL 里的最新数据，读到旧快照。

**解决**：把 `places.sqlite`、`-wal`、`-shm` **三个文件一起复制**到临时目录，再打开副本：

```python
import os, shutil, sqlite3, tempfile

def open_firefox_db(db_path):
    tmp = tempfile.mkdtemp(prefix="places_copy_")
    dst = os.path.join(tmp, "places.sqlite")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            shutil.copy2(db_path + suffix, dst + suffix)
    return sqlite3.connect(dst), tmp
```

只复制主文件会丢掉 WAL 里最近的改动，书签数量对不上就是这么来的。**备份同理**，必须连 WAL/SHM 一起备，最好在浏览器关闭后再备份。

---

## 坑 6（核心）：`url_hash` 不是 CRC64

**现象**：想往 `moz_places` 插一行，`url_hash` 是 NOT NULL 且 Firefox 启动时会断言 `url_hash != 0`。网上大量文章说它是 URL 的 CRC64，于是试了 ECMA-182、CRC-64/XZ、Jones、ISO 等六种变体——**对库内 200 条已有记录，全部 0 匹配**。

**原因**：`url_hash` 是 Mozilla 自家的 `mfbt/HashFunctions.h` 里的 **golden-ratio 哈希**，并且针对 URL 做了 48 位打包。

**正确算法**（`toolkit/components/places/Helpers.cpp`）：

```python
MAX_CHARS_TO_HASH = 1500
GOLDEN = 0x9E3779B9

def _places_hash(s: str) -> int:
    h = 0
    for ch in s.encode("utf-8"):
        # h = golden * (rotl32(h, 5) ^ byte)
        h = (GOLDEN * (((h << 5) | (h >> 27)) ^ ch)) & 0xFFFFFFFF
    return h

def ff_url_hash(spec: str) -> int:
    scheme = spec.split(":", 1)[0] if ":" in spec else ""
    body = spec[:MAX_CHARS_TO_HASH]
    # 低 32 位：整串哈希；高 16 位：scheme 哈希
    return ((_places_hash(scheme) & 0xFFFF) << 32) + (_places_hash(body) & 0xFFFFFFFF)
```

**验证方式**（别信我，信数据）：拿库里已有的行回算，全部比对：

```python
rows = con.execute("select url, url_hash from moz_places where url_hash != 0")
ok = sum(1 for u, h in rows if ff_url_hash(u) == (h & 0xFFFFFFFFFFFF))
print(ok)   # 1433 / 1433
```

两个细节：

- 哈希只取 URL **前 1500 个字符**（`MAX_CHARS_TO_HASH`）。超长 URL（比如带大段 query 的）必须截断后再算，否则对不上——我们最初 1430/1433，差的 3 条全是超长 URL，截断后 1433/1433。
- 结果按 64 位整数存储，但实际只占 48 位，不会溢出符号位。

如果算错了会怎样？书签栏显示不受影响（`fk` 指向 `moz_places.id`），但地址栏补全、历史去重、访问时"已存在页面"的查找都会失效，最终表现为同一 URL 出现多条历史记录。属于"能用但会慢慢变脏"的隐性 bug。

---

## 坑 7：新版 `places.sqlite` 没有触发器了

**现象**：想当然以为插入 `moz_bookmarks` 后，`foreign_count`、`moz_origins`、`recalc_frecency` 会由触发器自动维护。

**原因**：查询 `sqlite_master where type='trigger'` 返回**空**。现代 Firefox 把原来靠触发器维护的逻辑移到了 C++/Rust 代码里，直接写库时这些字段必须自己填。

**解决**：插入时手工维护：

```sql
-- 1. 保证 origin 存在，并拿到 origin_id
INSERT INTO moz_origins (prefix, host, frecency, recalc_frecency)
VALUES ('https://', 'example.com', 0, 1);

-- 2. 插入 moz_places：foreign_count=1（被书签引用一次），recalc_frecency=1
INSERT INTO moz_places
  (url, title, rev_host, visit_count, hidden, typed, frecency,
   guid, foreign_count, url_hash, origin_id, recalc_frecency)
VALUES (?, ?, ?, 0, 0, 0, 0, ?, 1, ?, ?, 1);

-- 3. 插入书签节点：type=1，position 追加到末尾
INSERT INTO moz_bookmarks
  (type, fk, parent, position, title, dateAdded, lastModified,
   guid, syncStatus, syncChangeCounter)
VALUES (1, ?, ?, ?, ?, ?, ?, ?, 2, 0);
```

要点：

- `rev_host` 是主机名反转再加点：`www.example.com` → `moc.elpmaxe.www.`
- `foreign_count` 表示被书签引用的次数，配合 `foreign_count` 才能正常删除/去重
- `position` 取同父节点 `max(position)+1`，否则新书签会插到奇怪的位置
- 现有数据里本地书签的 `syncStatus=2, syncChangeCounter=0`，照抄即可
- GUID 是 **12 字符的 base64url**（9 随机字节），不是 UUID

---

## 坑 8：时间戳单位差 10 倍，差出 51 年

**现象**：Edge 里新书签的添加时间显示成 1975 年。

**原因**：两个浏览器的 epoch 和精度都不同：

| | 起点 | 单位 |
|---|---|---|
| Firefox `dateAdded` | Unix epoch | 微秒 |
| Chromium `date_added` | 1601-01-01 | 100 纳秒 |

**解决**：

```python
import time

# Firefox
ff_now = int(time.time() * 1_000_000)

# Chromium：注意是 1e7，不是 1e6
CHROMIUM_EPOCH_OFFSET = 116444736000000000   # 1601→1970 的 100ns 数
edge_now = str(int(time.time() * 10_000_000) + CHROMIUM_EPOCH_OFFSET)
```

写成 `time.time() * 1_000_000` 再加大偏移，时间就整体早了 51 年——因为当前时间那部分被少乘了 10 倍。这个 bug 不会报错，只会让书签管理器里的日期很怪。

---

## 坑 9：SQL 占位符与列数错位

**现象**：`sqlite3.ProgrammingError: Incorrect number of bindings supplied`，或更糟——不报错但字段互相串位（`guid` 写进了 `frecency`）。

**原因**：12 列的 INSERT 写了 11 个值，或者 `?` 的顺序和参数元组顺序不一致。

**解决**：INSERT 一律写全列名，值列表按列顺序排列，参数元组逐个对齐；复杂的插入**先在副本上跑**，再动真实库：

```python
con.execute("insert into moz_places (a,b,c,...) values (?,?,?,...)", (va, vb, vc, ...))
```

小技巧：把列名列表和值列表写成相邻的两行，肉眼比对；或者先 `--dry-run` 在 `/tmp` 的副本上执行完整流程。

---

## 坑 10：文件夹路径拼出 `//toolbar/...`，踩了两个连环雷

**现象**：

- Firefox 自带的 4 条默认书签没有被跳过（本该按 `/menu/Mozilla Firefox` 前缀过滤）；
- Edge 里凭空多出一个 `toolbar` 文件夹，3 条书签被塞了进去。

**原因**：拼路径时把根节点也算进去了，而根节点 `moz_bookmarks.title` 是**空字符串**：

```python
parts.append(titles[bid] or '')      # 根节点把 '' 也塞进来了
return '/' + '/'.join(reversed(parts))
# -> '//toolbar/AI工具与平台'
```

于是：

1. `full.startswith('/menu/Mozilla Firefox')` 永远不成立（实际是 `//menu/...`），默认书签没跳过；
2. 解析 `//toolbar/AI工具与平台` 时多切出一层，去 Edge 里找名为 `toolbar` 的文件夹，找不到就**创建了一个**。

**解决**：

```python
if titles[bid]:          # 跳过空标题（根节点）
    parts.append(titles[bid])
```

同时这也暴露了一个流程问题：**先写错了怎么办**。答案是——

1. 动手前已把 Edge `Bookmarks` 和 `places.sqlite`（含 WAL/SHM）备份到独立目录；
2. 从备份恢复 Edge 文件；
3. 修正脚本后**直接重跑**：因为按 URL 求差集，Firefox 侧已写入的 12 条不会再重复插入。

这就是"幂等 + 备份"的价值：出错成本是从备份恢复 + 重跑一次，而不是手工清理几十条书签。

---

## 坑 11：Chromium 的 `checksum` 过期要不要重算？

`Bookmarks` JSON 顶层有一个 `checksum` 字段。手工改完书签后它必然对不上。

结论：**不用管**。现代 Chromium/Edge 在加载时不会因为这个校验失败而清空书签，会在下次保存时自行重算。实测改完重启 Edge，书签正常。但**不要**去删 `roots` 里的任何根节点（`bookmark_bar`、`other`、`synced`、`workspaces_v2`），那会真的丢数据。

---

## 验证清单

同步完成后逐项确认：

```bash
# 1. 数据库完整性
python3 -c "import sqlite3;print(sqlite3.connect('places.sqlite').execute('PRAGMA integrity_check').fetchone()[0])"
# ok

# 2. 不允许 url_hash=0（Firefox 启动断言）
python3 -c "import sqlite3;print(sqlite3.connect('places.sqlite').execute('select count(*) from moz_places where url_hash=0').fetchone()[0])"
# 0

# 3. 再跑一次 dry-run，应该 0 变化（幂等）
python3 scripts/bookmark_sync.py ... --dry-run

# 4. 新书签落在正确文件夹（按父节点标题分组检查）
```

本次结果：Edge 220 → 223，Firefox 215 → 227，双向差集归零，只剩 4 条被主动跳过的 Firefox 默认书签。

---

## 一个未完全解释的现象

重启 Firefox 后，观察到它自带的 4 条默认书签（`menu/Mozilla Firefox` 文件夹）被**它自己清理掉了**，而用户书签完好。这不是同步脚本的删除操作（脚本没有任何删除逻辑，`moz_bookmarks_deleted` 也是空的），更像是 Firefox 自身的默认书签维护行为。

影响很小：默认书签可通过 `browser.bookmarks.restore_default_bookmarks` 恢复，用户数据没有任何丢失。记录在此，供后来者遇到时不必惊慌。

---

## 小结

1. 先确认**哪个 profile 在用**，再动手。
2. 关浏览器、连 WAL 一起备份——顺序不能反。
3. `url_hash` 用官方算法算，并用库内数据回验，别信"CRC64"的传言。
4. 直接写库要手工维护 `foreign_count`/`origin_id`/`frecency`。
5. 全程幂等 + dry-run + 副本演练，出事就回滚重跑。
