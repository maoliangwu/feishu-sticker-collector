# 飞书群表情收集机器人（表情大师）

在群里 @机器人，让它把某个成员发过的图片 / 表情包打包成 zip 发回群里。

```
@表情大师 收集 @张三          # 最近 30 天
@表情大师 收集 @张三 7天
@表情大师 收集 @张三 全部
@表情大师 收集 我
@表情大师 帮助
```

所有飞书 API 调用都通过 `lark-cli`（机器人身份），复用已配置的 App ID / Secret；只依赖 Python 标准库。

## 一次性配置（飞书开发者后台）

1. **应用能力** → 启用「机器人」。
2. **权限管理** → 开通以下权限：
   - `im:message.group_at_msg:readonly`：接收群里 @机器人 的消息
   - `im:message.p2p_msg:readonly`：接收私聊消息（lark-cli 事件订阅要求）
   - `im:message.group_msg`：获取群组中所有消息（用于翻历史，属于敏感权限，可能需要管理员审批）
   - `im:message:readonly`：读取消息
   - `im:message:send_as_bot`：以机器人身份发消息
   - `im:resource`：下载消息里的图片，上传 zip 文件
3. **事件与回调** → 订阅方式选「使用长连接接收事件」，添加事件 `im.message.receive_v1`。
4. **创建版本并发布**（权限和事件要发布后才生效）。
5. 把机器人拉进目标群。

## 运行

```bash
python3 collector.py serve
```

要在本地调试导出（不经过群指令）：

```bash
python3 collector.py export --chat-id oc_xxx --user-id ou_xxx --days 30
```

`serve` 同时用两种方式接收指令：长连接实时事件（需完成上面第 3 步），以及每 5 秒翻一次群历史的轮询兜底（只需要读取群消息的权限）。两路按消息 ID 去重，同一条指令只会执行一次。

可选环境变量：`COLLECTOR_POLL_SECONDS`（轮询间隔，默认 5）、`COLLECTOR_DEFAULT_DAYS`（默认 30）、`COLLECTOR_MAX_ZIP_MB`（单个 zip 上限，默认 28，超过自动分卷）、`COLLECTOR_WORK_DIR`（默认 `/tmp/feishu-sticker-collector`）、`LARK_CLI`。

## 收集范围与限制

- 收集三类：图片消息、表情包消息（sticker）、富文本里内嵌的图片。按内容去重。
- 表情包（sticker）已实测可以下载（得到 PNG）。个别下载失败的会在结果里单独计数。
- 机器人只能读取它入群之后、且在权限允许范围内的群消息。
- 任务串行执行，同时有多个请求时会排队。
