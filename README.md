# wechat-bridge

**把微信接到你的个人 AI 助手。**

微信消息经腾讯官方 iLink Bot 通道（ClawBot）进入本机 `wechat-bridge`，转成 JSON 落盘到 `inbox/`；你的 Agent 读取、理解、组织回复后写入 `outbox/`，daemon 发回微信。只出站长轮询，不对外开任何端口。

```
+--------+   iLink getupdates   +--------+   inbox/*.json   +--------+
|        | -------------------> |        | ---------------> |        |
| WeChat |                      | daemon |                  | Agent  |
|        | <------------------- |        | <--------------- |        |
+--------+   iLink sendmessage  +--------+   outbox/*.json  +--------+
```

daemon 只做传输（收发、加解密、队列），不理解消息内容——理解与回复交给 Agent 侧的分拣流程（cron / hook / 你自己的程序），两边用文件队列解耦，daemon 重启不丢消息（游标与 context 均落盘）。

## 功能

- **双向收发**：文本、图片、视频、文件。收到的媒体自动从微信 CDN 下载并解密落盘；语音消息附带微信转写文本
- **媒体加解密**：微信 CDN 的 AES-128-ECB + PKCS7，`aes_key` 四种格式全解析，解密失败自动多策略兜底
- **正在输入指示器**：处理消息时可刷新微信原生「对方正在输入…」（getconfig / sendtyping，票据缓存）
- **在线状态**：daemon 启停向网关上报 notifystart / notifystop
- **白名单配对**：`config.json` 的 `allowed_users` 留空时，首个发信人自动配对锁定，之后只处理白名单消息
- **去重与分片**：入站 60 秒窗口去重；出站超 2000 字按段落自动分片
- **登录过期自检**：网关返回 -14 时落 `state/flags/session_expired` 标记并干净退出，外部调度器据此通知你重新扫码
- **纯 Python 标准库传输层**（urllib，天然走系统代理环境变量），仅媒体加解密与二维码渲染需要第三方包

## 快速开始

```bash
pip install -r requirements.txt   # pycryptodome + qrcode[pil]

python3 login.py                  # 生成二维码，微信扫码确认登录（码约 2 分钟过期，自动刷新）
# 登录成功后凭证写入 state/credentials.json（0600）

# 先用微信给 Bot 发一条消息（官方限制：用户先发，才有 context_token 可回复）
python3 daemon.py                 # 前台运行；或用 systemd 常驻（见下）
```

发一条回复（或让你的 Agent 写）：

```bash
echo '{"to_user_id": "<对方的 ilink user id>", "text": "你好"}' > outbox/hello.json
# 带媒体：再加 "media_path": "/abs/path/pic.jpg"（text 作配文，先发配文再发媒体，上限 100MB）
```

## 目录与契约

```
daemon.py        常驻服务：长轮询收消息 -> inbox/；outbox/ -> sendmessage
ilink.py         iLink 协议层：登录、收发、typing、getuploadurl、消息拼装
wxcrypto.py      CDN 媒体 AES 加解密与 aes_key 解析
wxmedia.py       CDN 媒体下载解密 / 加密上传
login.py         扫码登录，凭证落 state/credentials.json
typing_ping.py   单次刷新「正在输入」指示器（供分拣流程调用）
```

- 入站 `inbox/<ts>-<id>.json`：`{id, from_user_id, kind, text, ts}`；带媒体时多一个 `media_paths`（已解密落盘的本地文件绝对路径，存在 `media/<日期>/` 下）
- 出站 `outbox/<任意名>.json`：`{to_user_id, text}`，可选 `media_path`
- `state/` 是运行时状态（凭证、游标、context_token、过期标记），已在 `.gitignore` 中排除，**永远不要提交**

## systemd 常驻（可选）

参考 `wechat-bridge.service.example`，把路径改成你的部署目录后：

```bash
sudo cp wechat-bridge.service.example /etc/systemd/system/wechat-bridge.service
sudo systemctl daemon-reload && sudo systemctl enable --now wechat-bridge
```

## 限制（微信官方）

- 仅一对一私聊，不支持群聊；机器人号进不了群
- 助手不能主动发起会话：用户必须先发消息（context_token 机制）
- 连续约 10 条不回复会被限流，用户回一句重置
- 登录态约 30 天过期（错误码 -14），过期后重新扫码
- 长轮询连接被网关周期性断开（RemoteDisconnected）是**正常现象**，daemon 自动重试；判健康只看三样：凭证存在、进程存活、无 session_expired 标记。详见 `docs/gateway-notes.md`

## 踩坑记录

入站图片 `image_item.aeskey` **不是纯 hex 字符串**，直接 `bytes.fromhex` 会失败——它可能是 base64 包裹的 hex 或原始字节，必须走完整的格式解析（见 `wxcrypto.parse_aes_key`），且图片条目自带的 key 优先于 media 引用里的 `aes_key`。视频下载必须用 `encrypt_query_param` 拼的 CDN 地址，`full_url` 的数据解不开。

## 安全

- 凭证只存本地 `state/credentials.json`（0600），代码与日志里不出现 token 明文
- `.gitignore` 已排除 `state/`、`logs/`、`media/`、各队列目录与 `config.json`——首次提交前请再人工确认一遍暂存区

## 致谢

- iLink 协议参考 [wechatbot-sdk](https://pypi.org/project/wechatbot-sdk/)（MIT）
- 媒体收发的分层与部分实现思路参考 [Liu-zhongxian/wechat-bridge](https://github.com/Liu-zhongxian/wechat-bridge)（MIT）与 lawrencewzen/wx-bot（Go）

## License

MIT
