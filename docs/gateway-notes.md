# 网关行为基线（实测）

## RemoteDisconnected 是正常现象

- iLink 网关会周期性掐断长轮询的空闲连接，日志里出现
  `poll error: RemoteDisconnected('Remote end closed connection without response')`
  是常态（实测约每 2–4 分钟一次），**不是故障，不影响消息收发**：daemon 2 秒后重试即恢复。
- 判读方法：报错后重试间隔始终回到 2 秒、没有持续指数退避，说明每次报错之间都有成功的轮询（成功轮询不写日志，只在有消息时记 inbox）。

## 判健康只看三样

1. `state/credentials.json` 存在；
2. daemon 进程存活（`systemctl is-active wechat-bridge`）；
3. `state/flags/session_expired` 不存在。

**不要因为日志里刷屏的断连报错去重启 daemon**——重启改变不了网关行为，纯属徒劳。

## 真正需要处理的两类事件

- **登录过期**：接口返回错误码 -14，daemon 落 `state/flags/session_expired` 并干净退出。
  外部调度器（如每分钟巡检的 cron）检测到该标记后应通知用户重新运行 `login.py` 扫码，
  且只通知一次（可用一个 notified 标记防重复）。注意 -14 偶尔也可能是限流误报，
  若频繁出现可先等几分钟再试。
- **进程不在**：被杀或机器重启后未拉起，按 systemd 巡检流程恢复即可；
  游标与 context_token 均已落盘，恢复后不丢消息。

## 发送方向

- 发送走独立接口，与长轮询断连无关；`outbox/` 为空即无积压。
- CDN 上传偶发断连时任务会留在 outbox 队列自动重试，不会重复投递文本
  （媒体任务重试会先重新上传再发送）。
