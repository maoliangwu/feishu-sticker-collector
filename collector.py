#!/usr/bin/env python3
"""飞书群表情收集机器人。

在群里 @机器人 发指令，机器人会翻群聊历史，把指定成员发过的图片 / 表情包
下载下来去重、打包成 zip，再以文件消息发回群里。

所有飞书 API 调用都通过 lark-cli（机器人身份）完成，复用 lark-cli 里已配置的
App ID / App Secret，本程序不接触密钥。只依赖 Python 标准库。

用法:
  python3 collector.py serve                       # 常驻运行，监听群消息
  python3 collector.py export --chat-id oc_xxx --user-id ou_xxx [--days 30 | --all]
                                                   # 本地一次性导出（调试用）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

LARK_CLI = os.environ.get("LARK_CLI") or shutil.which("lark-cli") or "lark-cli"
# lark-cli 只允许把文件写到 cwd / /tmp / ~/files 下，所以工作目录放在 /tmp
WORK_ROOT = Path(os.environ.get("COLLECTOR_WORK_DIR", "/tmp/feishu-sticker-collector"))
DEFAULT_DAYS = int(os.environ.get("COLLECTOR_DEFAULT_DAYS", "30"))
ALL_DAYS = 365 * 10  # “全部” 回溯的上限
# 飞书上传文件上限 30MB，留点余量
MAX_ZIP_BYTES = int(os.environ.get("COLLECTOR_MAX_ZIP_MB", "28")) * 1024 * 1024

HELP_TEXT = (
    "表情收集用法（先 @我）：\n"
    "• 收集 @某人 —— 打包 TA 最近 {d} 天在本群发的图片/表情\n"
    "• 收集 @某人 7天 —— 指定天数\n"
    "• 收集 @某人 全部 —— 全部历史\n"
    "• 收集 我 —— 收集你自己的\n"
    "• 帮助 —— 显示本说明"
).format(d=DEFAULT_DAYS)


def log(*args):
    print(time.strftime("[%H:%M:%S]"), *args, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- lark-cli


class LarkError(RuntimeError):
    pass


def lark(args: list[str], cwd: Path | None = None, check: bool = True) -> dict:
    """调用 lark-cli（机器人身份），返回解析后的 JSON。网络抖动时自动重试。"""
    cmd = [LARK_CLI, *args, "--as", "bot"]
    for attempt in range(5):
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        out = proc.stdout.strip()
        try:
            data = json.loads(out) if out else {}
        except json.JSONDecodeError:
            data = {"ok": False, "raw": out}
        err = data.get("error") or {}
        if not (isinstance(err, dict) and err.get("type") == "network"):
            break
        time.sleep(2 * (attempt + 1))
    if check and (proc.returncode != 0 or data.get("ok") is False):
        detail = data.get("error") or data.get("raw") or proc.stderr.strip() or out
        raise LarkError(f"lark-cli {' '.join(args[:3])} 失败: {detail}")
    return data


def bot_open_id() -> str:
    data = lark(["api", "GET", "/open-apis/bot/v3/info"]).get("data", {})
    return (data.get("bot") or data).get("open_id", "")


def list_chat_messages(chat_id: str, start_ts: int, end_ts: int):
    """按时间升序遍历群消息（原始 API，content 为原始 JSON）。"""
    token = ""
    while True:
        params = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "start_time": str(start_ts),
            "end_time": str(end_ts),
            "sort_type": "ByCreateTimeAsc",
            "page_size": 50,
        }
        if token:
            params["page_token"] = token
        data = lark(["api", "GET", "/open-apis/im/v1/messages",
                     "--params", json.dumps(params)]).get("data", {})
        yield from data.get("items") or []
        token = data.get("page_token") or ""
        if not data.get("has_more") or not token:
            return


def download_resource(message_id: str, key: str, out_name: str, cwd: Path) -> bool:
    """下载消息里的图片资源。表情包（sticker）官方接口可能不支持，失败返回 False。"""
    path = f"/open-apis/im/v1/messages/{message_id}/resources/{key}"
    try:
        lark(["api", "GET", path, "--params", json.dumps({"type": "image"}),
              "--output", out_name], cwd=cwd)
    except LarkError as e:
        log("下载失败", message_id, key, e)
        return False
    f = cwd / out_name
    return f.exists() and f.stat().st_size > 0


def send_text(chat_id: str, text: str, reply_to: str | None = None):
    if reply_to:
        lark(["im", "+messages-reply", "--message-id", reply_to, "--text", text], check=False)
    else:
        lark(["im", "+messages-send", "--chat-id", chat_id, "--text", text], check=False)


def send_file(chat_id: str, file_path: Path, reply_to: str | None = None):
    # --file 只接受相对 cwd 的路径
    args = (["im", "+messages-reply", "--message-id", reply_to] if reply_to
            else ["im", "+messages-send", "--chat-id", chat_id])
    lark([*args, "--file", file_path.name], cwd=file_path.parent)


# --------------------------------------------------------------------------- 收集逻辑


def iter_image_keys(msg: dict):
    """从一条消息里取出 (key, kind)。kind: image / sticker / post。"""
    mtype = msg.get("msg_type")
    try:
        content = json.loads((msg.get("body") or {}).get("content") or "{}")
    except json.JSONDecodeError:
        return
    if mtype == "image" and content.get("image_key"):
        yield content["image_key"], "image"
    elif mtype == "sticker" and content.get("file_key"):
        yield content["file_key"], "sticker"
    elif mtype == "post":
        stack = [content]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get("tag") == "img" and node.get("image_key"):
                    yield node["image_key"], "post"
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(reversed(node))


def sniff_ext(data: bytes) -> str:
    if data.startswith(b"GIF8"):
        return ".gif"
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


@dataclass
class Result:
    zips: list[Path] = field(default_factory=list)
    saved: int = 0
    duplicates: int = 0
    failed_stickers: int = 0
    failed_other: int = 0
    scanned: int = 0


def collect(chat_id: str, user_id: str, days: int, label: str = "") -> Result:
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400
    job_dir = Path(tempfile.mkdtemp(prefix="job-", dir=WORK_ROOT))
    img_dir = job_dir / "images"
    img_dir.mkdir()
    res = Result()
    seen_hashes: set[str] = set()

    for msg in list_chat_messages(chat_id, start_ts, end_ts):
        res.scanned += 1
        sender = msg.get("sender") or {}
        if msg.get("deleted") or sender.get("id") != user_id:
            continue
        ctime = time.strftime("%Y%m%d_%H%M%S",
                              time.localtime(int(msg.get("create_time", "0")) / 1000))
        for i, (key, kind) in enumerate(iter_image_keys(msg)):
            tmp_name = f"dl_{msg['message_id']}_{i}"
            if not download_resource(msg["message_id"], key, tmp_name, img_dir):
                if kind == "sticker":
                    res.failed_stickers += 1
                else:
                    res.failed_other += 1
                continue
            tmp = img_dir / tmp_name
            data = tmp.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen_hashes:
                res.duplicates += 1
                tmp.unlink()
                continue
            seen_hashes.add(digest)
            res.saved += 1
            tmp.rename(img_dir / f"{ctime}_{res.saved:04d}_{kind}{sniff_ext(data)}")

    if res.saved:
        res.zips = make_zips(sorted(img_dir.iterdir()), job_dir, label or user_id)
    return res


def make_zips(files: list[Path], out_dir: Path, label: str) -> list[Path]:
    """打包；超过上传上限时拆成多个 zip。"""
    safe = re.sub(r"[^\w一-鿿-]+", "_", label).strip("_") or "user"
    stamp = time.strftime("%Y%m%d")
    groups: list[list[Path]] = [[]]
    size = 0
    for f in files:
        s = f.stat().st_size
        if groups[-1] and size + s > MAX_ZIP_BYTES:
            groups.append([])
            size = 0
        groups[-1].append(f)
        size += s
    zips = []
    for idx, group in enumerate(groups, 1):
        suffix = f"_part{idx}" if len(groups) > 1 else ""
        zp = out_dir / f"{safe}_表情_{stamp}{suffix}.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED) as z:  # 图片已压缩，直接存储
            for f in group:
                z.write(f, f.name)
        zips.append(zp)
    return zips


def summary(res: Result, who: str, days: int) -> str:
    span = "全部历史" if days >= ALL_DAYS else f"最近 {days} 天"
    lines = [f"{who} {span}：扫描 {res.scanned} 条消息，导出 {res.saved} 张图片"]
    if res.duplicates:
        lines.append(f"去掉重复 {res.duplicates} 张")
    if res.failed_stickers:
        lines.append(f"{res.failed_stickers} 个表情包下载失败")
    if res.failed_other:
        lines.append(f"{res.failed_other} 张图片下载失败")
    return "，".join(lines) + "。"


# --------------------------------------------------------------------------- 机器人指令


@dataclass
class Job:
    chat_id: str
    message_id: str
    user_id: str
    user_name: str
    days: int


def parse_command(event: dict, bot_id: str) -> Job | str | None:
    """返回 Job、需要回复的文本，或 None（忽略）。"""
    mentions = event.get("mentions") or []
    is_group = event.get("chat_type") == "group"
    if is_group and not any(m.get("id") == bot_id for m in mentions):
        return None  # 群里只响应 @机器人 的消息
    text = event.get("content") or ""
    for m in mentions:  # 去掉 @ 文本，避免名字里的数字被当成天数
        for token in (m.get("key"), "@" + (m.get("name") or "")):
            if token and token != "@":
                text = text.replace(token, " ")
    text = text.strip()

    if not re.search(r"收集|导出|打包", text):
        return HELP_TEXT
    if not is_group:
        return "请在群里 @我 使用，我只能收集我所在群里的消息。"

    targets = [m for m in mentions if m.get("id") and m.get("id") != bot_id]
    if targets:
        uid, name = targets[0]["id"], targets[0].get("name") or "TA"
    elif "我" in text:
        uid, name = event.get("sender_id", ""), "你"
    else:
        return "要收集谁的？请 @ 对方，例如：@我 收集 @张三 30天"

    if "全部" in text:
        days = ALL_DAYS
    else:
        m = re.search(r"(\d+)\s*天", text)
        days = max(1, min(int(m.group(1)), ALL_DAYS)) if m else DEFAULT_DAYS
    return Job(event["chat_id"], event["message_id"], uid, name, days)


def run_job(job: Job):
    send_text(job.chat_id, f"收到，开始收集 {job.user_name} 的表情，请稍候…", job.message_id)
    try:
        res = collect(job.chat_id, job.user_id, job.days, job.user_name)
        if not res.zips:
            send_text(job.chat_id, summary(res, job.user_name, job.days) + " 没有可打包的图片。",
                      job.message_id)
            return
        for zp in res.zips:
            send_file(job.chat_id, zp, job.message_id)
        send_text(job.chat_id, summary(res, job.user_name, job.days), job.message_id)
    except Exception as e:  # noqa: BLE001 — 任何失败都要告诉群里
        log("任务失败", e)
        send_text(job.chat_id, f"收集失败：{e}", job.message_id)


POLL_SECONDS = float(os.environ.get("COLLECTOR_POLL_SECONDS", "5"))
POLL_STATE = WORK_ROOT / "poll_state.json"
FIRST_LOOKBACK_MS = 10 * 60 * 1000  # 首次启动时补处理最近 10 分钟的指令


class Dispatcher:
    """事件流和轮询两路来源共用：按 message_id 去重后执行指令。"""

    def __init__(self, bot_id: str):
        self.bot_id = bot_id
        self.jobs: queue.Queue[Job] = queue.Queue()
        self.handled: set[str] = set()
        self.lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        while True:
            job = self.jobs.get()
            log("开始任务", job)
            run_job(job)

    def dispatch(self, event: dict, source: str):
        mid = event.get("message_id")
        if not mid or event.get("sender_type") in ("bot", "app"):
            return
        with self.lock:
            if mid in self.handled:
                return
            self.handled.add(mid)
        cmd = parse_command(event, self.bot_id)
        if cmd is None:
            return
        log(f"[{source}] 收到指令", mid, (event.get("content") or "")[:60])
        if isinstance(cmd, Job):
            pending = self.jobs.qsize()
            self.jobs.put(cmd)
            if pending:
                send_text(cmd.chat_id, f"已排队，前面还有 {pending} 个任务。", mid)
        else:
            send_text(event.get("chat_id", ""), cmd, mid)


def event_loop(d: Dispatcher):
    """实时事件（需在开发者后台订阅 im.message.receive_v1 并选长连接）。"""
    backoff = 2
    while True:
        log("连接事件流 im.message.receive_v1 …")
        proc = subprocess.Popen(
            [LARK_CLI, "event", "consume", "im.message.receive_v1", "--as", "bot"],
            stdin=subprocess.PIPE,  # lark-cli 在 stdin 关闭时会退出，保持打开
            stdout=subprocess.PIPE, text=True)
        for line in proc.stdout:  # type: ignore[union-attr]
            backoff = 2
            try:
                d.dispatch(json.loads(line), "event")
            except json.JSONDecodeError:
                continue
        proc.wait()
        log(f"事件流退出（code {proc.returncode}），{backoff}s 后重连")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


def history_to_event(item: dict, chat_id: str) -> dict:
    """把历史消息转换成与事件相同的结构。"""
    text = ""
    if item.get("msg_type") == "text":
        try:
            text = json.loads(item["body"]["content"]).get("text", "")
        except (KeyError, json.JSONDecodeError):
            pass
    sender = item.get("sender") or {}
    return {
        "chat_id": chat_id, "chat_type": "group", "message_id": item.get("message_id"),
        "content": text, "mentions": item.get("mentions") or [],
        "sender_id": sender.get("id"), "sender_type": sender.get("sender_type"),
    }


def poll_loop(d: Dispatcher):
    """兜底：定时翻机器人所在群的新消息，找 @机器人 的指令。只需历史消息读取权限。"""
    try:
        state = json.loads(POLL_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    chats: list[str] = []
    last_chat_refresh = 0.0
    while True:
        try:
            if time.time() - last_chat_refresh > 60:
                data = lark(["im", "+chat-list", "--page-all"]).get("data", {})
                chats = [c["chat_id"] for c in data.get("chats") or []]
                last_chat_refresh = time.time()
            now_ms = int(time.time() * 1000)
            for chat_id in chats:
                since = int(state.get(chat_id, now_ms - FIRST_LOOKBACK_MS))
                newest = since
                for item in list_chat_messages(chat_id, since // 1000, now_ms // 1000 + 1):
                    ct = int(item.get("create_time", "0"))
                    if ct <= since:
                        continue
                    newest = max(newest, ct)
                    d.dispatch(history_to_event(item, chat_id), "poll")
                state[chat_id] = newest
            POLL_STATE.write_text(json.dumps(state))
        except Exception as e:  # noqa: BLE001
            log("轮询出错", e)
        time.sleep(POLL_SECONDS)


def serve():
    bot_id = bot_open_id()
    log("机器人 open_id:", bot_id)
    d = Dispatcher(bot_id)
    threading.Thread(target=event_loop, args=(d,), daemon=True).start()
    poll_loop(d)


# --------------------------------------------------------------------------- 入口


def main():
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    ap = argparse.ArgumentParser(description="飞书群表情收集机器人")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="常驻运行，监听群里 @机器人 的指令")
    ex = sub.add_parser("export", help="本地一次性导出")
    ex.add_argument("--chat-id", required=True)
    ex.add_argument("--user-id", required=True, help="目标成员 open_id (ou_xxx)")
    g = ex.add_mutually_exclusive_group()
    g.add_argument("--days", type=int, default=DEFAULT_DAYS)
    g.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if args.cmd == "serve":
        serve()
    else:
        days = ALL_DAYS if args.all else args.days
        res = collect(args.chat_id, args.user_id, days)
        print(summary(res, args.user_id, days))
        for zp in res.zips:
            print(zp)


if __name__ == "__main__":
    main()
