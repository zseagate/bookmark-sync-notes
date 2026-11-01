#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Edge / Firefox 书签双向增量合并（互补，不覆盖）。

用法：
    python3 bookmark_sync.py \
        --edge-bookmarks ~/.config/microsoft-edge/Default/Bookmarks \
        --firefox-db ~/.config/mozilla/firefox/<profile>/places.sqlite \
        [--edge-bar-name 收藏夹栏] [--dry-run] [--force]

安全措施：
  * 默认检测浏览器进程，运行中直接退出（避免写入被覆盖）
  * 写入前自动备份两边文件到 ./bookmark_sync_backup_<时间戳>/
  * 按 URL 求差集，天然幂等，可重复执行
"""

import argparse
import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from datetime import datetime

# ---------------------------------------------------------------- 常量

# Firefox 默认书签（menu/Mozilla Firefox），默认不同步到 Edge
DEFAULT_SKIP_PREFIXES = ("/menu/Mozilla Firefox",)

# Chromium date_added: 1601-01-01 起的 100ns 数
CHROMIUM_EPOCH_OFFSET = 116444736000000000
CHROMIUM_TICKS_PER_SEC = 10_000_000

# Firefox url_hash 只取 URL 前 1500 字符
MAX_CHARS_TO_HASH = 1500


# ---------------------------------------------------------------- Firefox 哈希
# 注意：这不是 CRC64！是 mfbt/HashFunctions.h 里的 golden-ratio 哈希。
# h = 0x9E3779B9 * (rotl32(h, 5) ^ byte)
# 结果打包成 48 位：低 32 位 = 整串哈希，高 16 位 = scheme 哈希。
def _places_hash(s: str) -> int:
    h = 0
    for ch in s.encode("utf-8"):
        h = (0x9E3779B9 * (((h << 5) | (h >> 27)) ^ ch)) & 0xFFFFFFFF
    return h


def ff_url_hash(spec: str) -> int:
    scheme = spec.split(":", 1)[0] if ":" in spec else ""
    body = spec[:MAX_CHARS_TO_HASH]
    return ((_places_hash(scheme) & 0xFFFF) << 32) + (_places_hash(body) & 0xFFFFFFFF)


def ff_guid() -> str:
    """Firefox 的 12 字符 GUID（9 字节 base64url）。"""
    return base64.urlsafe_b64encode(os.urandom(9)).decode().rstrip("=")


# ---------------------------------------------------------------- 读取

def read_edge(path):
    """返回 (完整 JSON, {url: (根键, [文件夹名...], 标题)})。"""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    out = {}

    def walk(node, root, path_parts):
        for child in node.get("children", []):
            ctype = child.get("type")
            if ctype == "url":
                out[child["url"]] = (root, tuple(path_parts), child.get("name", ""))
            elif ctype == "folder":
                walk(child, root, path_parts + [child.get("name", "")])

    for root in ("bookmark_bar", "other", "synced"):
        node = doc["roots"].get(root)
        if node:
            walk(node, root, [])
    return doc, out


def read_firefox(con):
    """返回 {url: (根名, [文件夹名...], 标题)}。"""
    parent = dict(con.execute("select id, parent from moz_bookmarks"))
    titles = dict(con.execute("select id, title from moz_bookmarks"))

    def path_of(bid):
        parts, seen = [], set()
        while bid and bid in parent and bid not in seen:
            seen.add(bid)
            bid = parent[bid]
            if bid == 0 or bid not in titles:
                break
            if titles[bid]:  # 根节点 title 为空，跳过，否则会变成 '//toolbar'
                parts.append(titles[bid])
        return list(reversed(parts))

    out = {}
    for bid, url, title in con.execute(
        """select b.id, p.url, b.title
             from moz_bookmarks b join moz_places p on b.fk = p.id
            where b.type = 1 and p.url not like 'place:%'"""
    ):
        parts = path_of(bid)
        root = parts[0] if parts else ""
        out[url] = (root, parts[1:], title or "")
    return out


def open_firefox_db(db_path):
    """Firefox 运行时 WAL 会锁库：复制到临时目录再读。"""
    tmp = tempfile.mkdtemp(prefix="places_copy_")
    dst = os.path.join(tmp, "places.sqlite")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            shutil.copy2(db_path + suffix, dst + suffix)
    return sqlite3.connect(dst), tmp


# ---------------------------------------------------------------- 写入 Firefox

def ff_root_id(con, root_name):
    """toolbar=3 / menu=2 / unfiled=5（按 guid 查找更稳）。"""
    guid = {"toolbar": "toolbar_____", "menu": "menu________", "unfiled": "unfiled_____"}[root_name]
    row = con.execute("select id from moz_bookmarks where guid=?", (guid,)).fetchone()
    if not row:
        raise RuntimeError(f"找不到 Firefox 根节点 {root_name}")
    return row[0]


def ff_ensure_folder(con, name, parent_id):
    row = con.execute(
        "select id from moz_bookmarks where type=2 and parent=? and title=?",
        (parent_id, name),
    ).fetchone()
    if row:
        return row[0]
    now = int(time.time() * 1_000_000)
    pos = con.execute(
        "select coalesce(max(position), -1) + 1 from moz_bookmarks where parent=?",
        (parent_id,),
    ).fetchone()[0]
    cur = con.execute(
        """insert into moz_bookmarks
             (type, parent, position, title, dateAdded, lastModified, guid,
              syncStatus, syncChangeCounter)
           values (2, ?, ?, ?, ?, ?, ?, 2, 0)""",
        (parent_id, pos, name, now, now, ff_guid()),
    )
    return cur.lastrowid


def ff_ensure_origin(con, url):
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    if not host:
        return None
    prefix = (p.scheme or "http") + "://"
    row = con.execute(
        "select id from moz_origins where host=? and prefix=?", (host, prefix)
    ).fetchone()
    if row:
        return row[0]
    cur = con.execute(
        """insert into moz_origins (prefix, host, frecency, recalc_frecency)
           values (?, ?, 0, 1)""",
        (prefix, host),
    )
    return cur.lastrowid


def ff_rev_host(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return (host + ".")[::-1] if host else ""


def ff_add_bookmark(con, url, title, parent_id):
    now = int(time.time() * 1_000_000)
    origin_id = ff_ensure_origin(con, url)

    row = con.execute(
        "select id from moz_places where url_hash=? and url=?",
        (ff_url_hash(url), url),
    ).fetchone()
    if row:
        place_id = row[0]
        con.execute(
            "update moz_places set foreign_count = foreign_count + 1, "
            "title = coalesce(title, ?) where id = ?",
            (title, place_id),
        )
    else:
        cur = con.execute(
            """insert into moz_places
                 (url, title, rev_host, visit_count, hidden, typed, frecency,
                  guid, foreign_count, url_hash, origin_id, recalc_frecency)
               values (?, ?, ?, 0, 0, 0, 0, ?, 1, ?, ?, 1)""",
            (url, title, ff_rev_host(url), ff_guid(), ff_url_hash(url), origin_id),
        )
        place_id = cur.lastrowid

    pos = con.execute(
        "select coalesce(max(position), -1) + 1 from moz_bookmarks where parent=?",
        (parent_id,),
    ).fetchone()[0]
    con.execute(
        """insert into moz_bookmarks
             (type, fk, parent, position, title, dateAdded, lastModified, guid,
              syncStatus, syncChangeCounter)
           values (1, ?, ?, ?, ?, ?, ?, ?, 2, 0)""",
        (place_id, parent_id, pos, title, now, now, ff_guid()),
    )


# ---------------------------------------------------------------- 写入 Edge

def edge_next_id(doc):
    ids = []

    def walk(node):
        if str(node.get("id", "")).isdigit():
            ids.append(int(node["id"]))
        for child in node.get("children", []):
            walk(child)

    for root in ("bookmark_bar", "other", "synced"):
        if doc["roots"].get(root):
            walk(doc["roots"][root])
    return max(ids, default=0) + 1


def edge_date_added():
    return str(int(time.time() * CHROMIUM_TICKS_PER_SEC) + CHROMIUM_EPOCH_OFFSET)


def edge_ensure_folder(doc, root, names):
    node = doc["roots"][root]
    for name in names:
        child = next(
            (c for c in node.get("children", [])
             if c.get("type") == "folder" and c.get("name") == name),
            None,
        )
        if child is None:
            child = {
                "children": [],
                "date_added": edge_date_added(),
                "date_modified": edge_date_added(),
                "guid": str(uuid.uuid4()),
                "id": str(edge_next_id(doc)),
                "name": name,
                "source": "unknown",
                "type": "folder",
            }
            node.setdefault("children", []).append(child)
        node = child
    return node


def edge_add_bookmark(doc, root, names, title, url):
    folder = edge_ensure_folder(doc, root, names)
    folder.setdefault("children", []).append(
        {
            "date_added": edge_date_added(),
            "date_last_used": "0",
            "guid": str(uuid.uuid4()),
            "id": str(edge_next_id(doc)),
            "name": title or url,
            "source": "unknown",
            "type": "url",
            "url": url,
        }
    )


# ---------------------------------------------------------------- 主流程

def browser_running():
    running = []
    for name, pattern in (("Firefox", "firefox"), ("Edge", "msedge")):
        try:
            out = subprocess.run(
                ["pgrep", "-x", pattern], capture_output=True, text=True
            ).stdout.strip()
            if out:
                running.append(name)
        except FileNotFoundError:
            pass
    return running


def backup(paths):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.abspath(f"bookmark_sync_backup_{stamp}")
    os.makedirs(dest, exist_ok=True)
    for p in paths:
        if os.path.exists(p):
            shutil.copy2(p, dest)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(p + suffix):
                shutil.copy2(p + suffix, dest)
    return dest


EDGE_TO_FF_ROOT = {"bookmark_bar": "toolbar", "other": "menu"}


def main():
    ap = argparse.ArgumentParser(description="Edge / Firefox 书签双向增量合并")
    ap.add_argument("--edge-bookmarks", required=True, help="Edge 的 Bookmarks JSON 路径")
    ap.add_argument("--firefox-db", required=True, help="Firefox profile 下的 places.sqlite")
    ap.add_argument("--edge-bar-name", default="收藏夹栏",
                    help="Edge 书签栏在 Firefox 中对应的根名（仅用于提示）")
    ap.add_argument("--skip-firefox-folders", nargs="*",
                    default=list(DEFAULT_SKIP_PREFIXES),
                    help="Firefox 中不同步到 Edge 的目录前缀，默认跳过 Firefox 默认书签")
    ap.add_argument("--dry-run", action="store_true", help="只打印差异，不写入")
    ap.add_argument("--force", action="store_true", help="浏览器运行中也强行写入（不推荐）")
    args = ap.parse_args()

    running = browser_running()
    if running and not args.force and not args.dry_run:
        print(f"[!] 检测到运行中的浏览器：{', '.join(running)}")
        print("    请先完全退出，避免写入被内存中的状态覆盖；或加 --force（风险自负）。")
        return 2

    edge_doc, edge = read_edge(args.edge_bookmarks)
    con, tmpdir = open_firefox_db(args.firefox_db)
    try:
        ff = read_firefox(con)
        con.close()

        def ff_parent(con, root_name, names):
            parent = ff_root_id(con, root_name)
            for name in names:
                parent = ff_ensure_folder(con, name, parent)
            return parent

        to_ff, to_edge, skipped = [], [], []

        # Edge -> Firefox
        for url, (root, names, title) in sorted(edge.items()):
            if url in ff:
                continue
            ff_root = EDGE_TO_FF_ROOT.get(root)
            if ff_root is None:
                skipped.append((url, f"未知 Edge 根 {root}"))
                continue
            to_ff.append((url, title, ff_root, names))

        # Firefox -> Edge
        for url, (root, names, title) in sorted(ff.items()):
            if url in edge:
                continue
            full = "/" + "/".join([root] + names)
            if any(full.startswith(p) for p in args.skip_firefox_folders):
                skipped.append((url, "Firefox 默认书签，跳过"))
                continue
            edge_root = "bookmark_bar" if root == "toolbar" else "other"
            to_edge.append((url, title, edge_root, names))

        print(f"Edge 独有（补进 Firefox）：{len(to_ff)}")
        for url, title, _, _ in to_ff:
            print(f"  + {title} | {url}")
        print(f"Firefox 独有（补进 Edge）：{len(to_edge)}")
        for url, title, _, _ in to_edge:
            print(f"  + {title} | {url}")
        if skipped:
            print(f"跳过：{len(skipped)}")
            for url, why in skipped:
                print(f"  - {why} | {url}")

        if args.dry_run or not (to_ff or to_edge):
            print("dry-run / 无变化，不写入。")
            return 0

        dest = backup([args.edge_bookmarks, args.firefox_db])
        print(f"已备份到 {dest}")

        con = sqlite3.connect(args.firefox_db)
        try:
            with con:
                for url, title, ff_root, names in to_ff:
                    ff_add_bookmark(con, url, title, ff_parent(con, ff_root, names))
        finally:
            con.close()

        for url, title, edge_root, names in to_edge:
            edge_add_bookmark(edge_doc, edge_root, names, title, url)

        tmp_out = args.edge_bookmarks + ".tmp"
        with open(tmp_out, "w", encoding="utf-8") as f:
            json.dump(edge_doc, f, ensure_ascii=False, indent=3)
            f.write("\n")
        os.replace(tmp_out, args.edge_bookmarks)

        print(f"完成：Firefox +{len(to_ff)}，Edge +{len(to_edge)}")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
