#!/usr/bin/env python3
"""从飞书桌面客户端（macOS）的本地缓存里备份表情包 / 图片。

只读取缓存，不修改飞书的任何文件；不调用任何接口。

用法:
  python3 backup_local_cache.py                         # 备份表情包到 ~/Desktop/飞书表情备份
  python3 backup_local_cache.py --out ~/Downloads/stk --zip
  python3 backup_local_cache.py --include-images        # 连聊天图片一起备份
  python3 backup_local_cache.py --list                  # 只统计，不复制
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import time
import zipfile
from pathlib import Path

DEFAULT_ROOT = Path.home() / "Library/Application Support/LarkShell/sdk_storage"


def sniff_ext(head: bytes) -> str | None:
    if head.startswith(b"GIF8"):
        return ".gif"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head.startswith(b"\xff\xd8"):
        return ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None  # 非图片（或客户端加密的文件）跳过


def file_hash(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description="备份飞书本地缓存里的表情包")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="飞书 sdk_storage 目录")
    ap.add_argument("--out", type=Path, default=Path.home() / "Desktop/飞书表情备份")
    ap.add_argument("--include-images", action="store_true", help="同时备份聊天图片缓存")
    ap.add_argument("--zip", action="store_true", help="完成后打成 zip")
    ap.add_argument("--list", action="store_true", help="只统计，不复制")
    args = ap.parse_args()

    kinds = ["stickers"] + (["images"] if args.include_images else [])
    sources = [d for kind in kinds for d in sorted(args.root.glob(f"*/resources/{kind}")) if d.is_dir()]
    if not sources:
        raise SystemExit(f"没找到缓存目录：{args.root}/*/resources/stickers（飞书客户端是否安装/登录过？）")

    seen: set[str] = set()
    stats = {"copied": 0, "dup": 0, "skipped": 0}
    for src in sources:
        account, kind = src.parts[-3], src.name
        files = [p for p in src.rglob("*") if p.is_file()]
        print(f"{account[:8]}…/{kind}: {len(files)} 个文件")
        if args.list:
            continue
        dest = args.out / f"{account[:8]}_{kind}"
        for p in files:
            with p.open("rb") as f:
                ext = sniff_ext(f.read(12))
            if not ext:
                stats["skipped"] += 1
                continue
            digest = file_hash(p)
            if digest in seen:
                stats["dup"] += 1
                continue
            seen.add(digest)
            dest.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(p.stat().st_mtime))
            target = dest / f"{stamp}_{digest[:10]}{ext}"
            shutil.copy2(p, target)
            stats["copied"] += 1

    if args.list:
        return
    print(f"完成：复制 {stats['copied']} 个，重复 {stats['dup']} 个，非图片跳过 {stats['skipped']} 个")
    print(f"输出目录：{args.out}")
    if args.zip and stats["copied"]:
        zp = args.out.with_suffix(".zip")
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED) as z:
            for f in sorted(args.out.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(args.out))
        print(f"压缩包：{zp}")


if __name__ == "__main__":
    main()
