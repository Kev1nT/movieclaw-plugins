# 种子做种定时清理（`seed-scheduler`）

给下载器里的种子设「做种多久」：到点自动删任务（可连数据文件一起删）。
不依赖 MovieClaw 入库——只把 MovieClaw 当下载器用也行。

## 怎么判断该删

1. **做种时长以 qBittorrent 自己的记录为准**：累计做种秒数、完成时刻、添加时刻三者取最长。
   qB 重启或重新校验会把累计秒数归零，取最长才不会把熬了半个月的老种子判成刚下的。
   三个信号都缺失时才用「插件第一次看到它」的时间兜底。
2. **按状态分类**：正在做种 / 文件已丢失 / 仍在下载 / 出错 / 校验中 / 其他状态，
   每一类在设置里单独开关，默认只处理前两类。
3. **安全线**（默认全开）会挡住删除，并把「到期但没删」发成通知：
   - 站点 H&R 考核的种子至少做种到 `hr_min_hours`；
   - 刷流（分享率自动提升）引擎登记的种子由它自己汰换，本插件不碰；
   - 最近 30 分钟有人在网页上打开过这部片；
   - MovieClaw 订阅刚投递、还没拿到文件的种子；
   - 打开「只清理媒体库里已有这份文件的种子」后，没入库的一律不删。
4. 手动下载、一直没入库的种子不删，超过「做种时长 + 若干天」时提醒你处理。

## 设置

| 字段 | 标题 | 默认 |
|---|---|---|
| `seed_hours` | 单种做种时长（小时） | 72 |
| `overdue_days` | 没入库的种子再等几天后提醒（天） | 3 |
| `delete_files` | 删除时连数据文件一起删 | 开 |
| `dry_run` | 只记录不删除（演练） | 关 |
| `library_required` | 只清理媒体库里已有这份文件的种子 | 关 |
| `keep_waiting_subscriptions` | 订阅还等着用的种子先保留 | 开 |
| `protect_unwatched` | 库里的还没看完就先保留 | 关 |
| `protect_recent_open` | 最近打开过的先保留 | 开 |
| `respect_hr` | 带 H&R 考核的种子多保一段 | 开 |
| `hr_min_hours` | H&R 种子最短做种时长（小时） | 72 |
| `respect_boost` | 不碰刷流引擎的种子 | 开 |
| `handle_seeding` | 处理·正在做种 | 开 |
| `handle_missing_files` | 处理·文件已丢失 | 开 |
| `handle_downloading` | 处理·仍在下载 | 关 |
| `handle_errored` | 处理·出错 | 关 |
| `handle_checking` | 处理·校验中 | 关 |
| `handle_unknown` | 处理·其他未知状态 | 关 |
| `qb_url` | qBittorrent WebUI 地址 | 空 |
| `qb_username` | qBittorrent 用户名 | 空 |
| `qb_password` | qBittorrent 密码（加密保存） | 空 |

`qb_url` 留空时只用任务中心的口径，只看得见还在跑的任务；填上 WebUI 地址才能扫到下载器里的全部种子。

## 命令

| 命令 | 作用 |
|---|---|
| `mclaw plugins seed-scheduler status` | 设置、累计统计与最近的处理记录 |
| `mclaw plugins seed-scheduler preview` | 只读预演：哪些种子会到期、各自为什么删 / 为什么留 |
| `mclaw plugins seed-scheduler queue` | 每个种子还要做种多久、什么时候清理（按时间排好） |
| `mclaw plugins seed-scheduler scan` | 立刻扫描一次（`dry_run` 不传时按设置里的演练开关） |

## 定时与后台

- 定时任务「做种定时清理扫描」，周期在「设置 → 定时任务」里改。
- 后台任务 `seed-scheduler:sweep` 做实际删除，进度在「活动 → 任务」里看。
- 常驻监听四个事件：下载完成、入库完成、条目详情被打开、开始播放。

## 事件与通知

- `seed-scheduler.seed-removed`：种子已到期删除，可带「已做种 N 小时、是否连文件一起删」。
- `seed-scheduler.seed-overdue`：到期但被安全线挡着没删，交给你处理。

两条都配了被动通知，发给管理员。

## 权限与契约

- 宿主操作：`dl.list`、`dl.tasks`、`dl.file-sources.list`、`library.items.relations`、`library.list`、
  `library.items.get`、`dl.torrent.delete`、`library.scan.start`
- 契约：`host-ops`、`plugin-data`、`plugin-events`、`job-handlers`、`plugin-jobs`、`scheduled-tasks`、
  `download.completed`、`library.ingest.completed`、`library.item.viewed`、`playback.started`
- 敏感事件：`playback.*`、`library.item.viewed`
- `dl.torrent.delete` 是破坏性操作，默认在演练模式下不触发。

## 说明

站点 H&R 的具体考核时长 MovieClaw 拿不到，所以 `hr_min_hours` 只是一个兜底值（常见站点是 72 小时
加分享率 1.0 以上），实际以站点要求为准。拿不准就先开「只记录不删除」跑几轮。
