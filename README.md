# astrbot_plugin_bililive

监测 B 站 UP 主的**开播状态**与**空间动态**，有变化即推送到指定群。每个 UP 主可独立配置推送目标群；Web 面板、插件配置页、聊天命令三处改的是同一份数据，互相同步。

> **源头声明**：本项目 Fork 自 [BB0813/astrbot_plugin_bilibiliobs](https://github.com/BB0813/astrbot_plugin_bilibiliobs)（原作者 **BB0813**），在其代码基础上重写、修复而来，功劳归属源头项目。

> 更新日志见 [Releases](https://github.com/Lianzy-Baimiao/astrbot_plugin_bililive/releases)。

## 环境要求

- AstrBot ≥ 4.24.1（Web 面板依赖 Plugin Pages）

## 功能概览

**开播监测**

- 每个 UP 独立配置推送目标群，互不影响；从设计上避免同一次开播重复推送
- 关播通知、直播封面图（均可关）
- `@全体` 精确到「UP × 群」，各群独立开关

**动态推送**

- 支持投稿视频、图文/相簿、专栏、转发、音频、纯文字，按类型分别开关
- 用动态 id 数值游标判断新旧，重启不补发存量、被风控自动退避，不漏也不刷屏
- **置顶评论盯梢**：白名单 UP 发视频后盯它评论区，UP 自己的置顶评论出现后单独补推（附图），与视频推送解耦

**管理**

- Web 面板可视化管理：订阅矩阵、按群通知开关、运行状态与诊断
- 聊天命令与配置双向同步；退订支持序号，无需记 UID
- 管理员鉴权（`admin_ids`）、扫码登录续期 Cookie、Cookie 失效私聊提醒
- 数据落 AstrBot 规范目录，卸载随目录清理

## Web 面板

AstrBot WebUI → 插件 → B站开播监测 → 打开面板：

- **订阅矩阵**：切换「开播订阅 / 动态订阅」，一行一个 UP 主，行内直接挂要推的群标签；「新增 / 编辑」弹窗里勾选群（可按群名/群号搜索、一键刷新机器人所在的全部群），每个群可单独勾 `@全体`。动态订阅矩阵每行还带「盯置顶评论」开关。
- **按群通知开关**：开播 / 关播 / 动态三列，改即保存，等价于群里 `/开启通知`、`/关闭通知`。
- **运行状态**：HTTP 会话、监控任务、检查间隔（限流退避时显示实际间隔）、动态基线数、Cookie 是否配置、静音时段是否生效。
- **诊断与整理**：列出无法解析的配置行；标出「同一个群写成多种平台前缀」「前缀不是已加载平台实例」两类问题，并支持一键整理（改写到可用实例、合并重复写法）。

保存走插件原有的 `_save_subs` / `_save_dyn_subs`（序列化 + 落盘 + `groups.json` 剪枝），面板、配置页、聊天命令三处不会互相覆盖。

## 配置项

在 AstrBot WebUI 插件配置页设置：

| 配置项 | 说明 |
| --- | --- |
| `subscriptions` | 开播订阅列表，每行一个 UP，格式 `UID=目标[,目标...]`，目标可带 `@all` 后缀 |
| `default_platform` | 裸群号补全到哪个平台**实例 id**（留空自动探测；填适配器类型名如 `aiocqhttp` 会自动映射到同类型实例 id） |
| `check_interval` | 开播检查间隔（秒），生效钳制 30–600 |
| `max_monitors` | 最大监控 UP 数 |
| `enable_notifications` / `enable_end_notifications` | 开播 / 关播通知全局总开关 |
| `send_cover` | 推送是否附带直播封面图 |
| `quiet_hours` | 静音时段 `HH:MM-HH:MM`（可选，支持跨零点），该时段暂停检查与推送 |
| `admin_ids` | 管理员 QQ 号列表：命令鉴权、Cookie 失效提醒、扫码登录鉴权 |
| `bilibili_cookie` | B 站 Cookie（可选，提高 API 限额；动态推送建议配置） |
| `live_notify_template` / `end_notify_template` | 开播 / 关播通知模板 |
| `dynamic_subscriptions` | 动态订阅列表，写法与 `subscriptions` 一致 |
| `dynamic_check_interval` | 动态检查间隔（秒），钳制 30–600，默认 45；UP 多时建议 120 以上 |
| `dynamic_notify_template` | 动态通知模板，占位符 `{uname}` `{action}` `{title}` `{text}` `{url}` |
| `dyn_notify_video` / `_draw` / `_article` / `_word` / `_forward` / `_music` | 各类动态推送开关（默认开） |
| `dyn_notify_live` | 动态里的「正在直播」卡片（默认关，避免与开播监测重复） |
| `dyn_notify_other` | 归类外的动态（投票、播单等，默认关） |
| `comment_watch_uids` | 置顶评论盯梢 UP 白名单（UID 列表），留空=不启用 |
| `dyn_comment_watch_hours` | 视频发布后盯评论区多久（小时），钳制 1–72，默认 2 |
| `comment_notify_template` | 置顶评论通知模板，占位符 `{uname}` `{title}` `{text}` `{url}` |

### 订阅写法

每个「目标」有三种写法，短的优先：

```
111111111=777777777                       # ①裸群号，按 default_platform 补全
111111111=napcat:777777777                # ②平台id:群号（推荐，可多平台混用）
111111111=napcat:GroupMessage:777777777   # ③完整 unified_msg_origin
222222222=napcat:777777777@all            # 该群开播时 @全体成员
111111111=napcat:777777777,default_666:777777777   # 同一 UP 推到多个平台
# 以 # 开头的行是注释
```

- 冒号前是平台**实例 id**（在「配置 → 消息平台」里看，如 `napcat`），不是类型名 `aiocqhttp` / `qq_official`
- 目标后加 `@all` 表示该群 @全体；旧写法「行尾 ` | at_all`」仍兼容
- 同一 UID 可写多行，自动合并群列表；格式错误的行忽略并在日志与 `/开播监测状态` 提示
- `dynamic_subscriptions` 写法完全相同，只是推的是空间动态（`@all` 仅对视频/直播卡片类生效）

## 聊天命令

| 命令 | 说明 |
| --- | --- |
| `/订阅 <UID> [at_all]` | 把当前群加入该 UP 的开播推送目标 |
| `/退订 <UID或序号>` | 从当前群移除某 UP，序号来自 `/订阅列表` |
| `/订阅列表` | 列出本群订阅的 UP（带序号与直播状态） |
| `/检查直播 <UID>` | 手动查一次直播状态 |
| `/动态订阅 <UID> [at_all]` | 把当前群加入该 UP 的动态推送目标（自动记基线，不补发旧动态） |
| `/退订动态 <UID或序号>` | 从当前群移除该 UP 的动态订阅 |
| `/动态列表` | 列出本群订阅的动态 UP |
| `/检查动态 <UID>` | 手动拉一次该 UP 最新动态（展示前 3 条） |
| `/检查置顶评论 <BV号\|链接\|UID>` | 手动拉一次视频里 UP 自己的置顶评论并发到当前会话；给 UID 时取该 UP 最新视频 |
| `/开播监测状态` | 查看运行状态与生效配置 |
| `/开启通知` `/关闭通知` | 本群通知总开关（仅管理员；关闭后开播/关播/动态都不发） |
| `/开启关播通知` `/关闭关播通知` | 本群关播通知开关（仅管理员） |
| `/开启动态通知` `/关闭动态通知` | 本群动态推送开关（仅管理员） |

### 管理员

在配置 `admin_ids` 里填管理员 QQ 号（可多个）：控制本群通知开关、私聊扫码登录续期 Cookie、接收 Cookie 失效提醒。未配置时回退用 AstrBot 全局管理员判定。

私聊 Bot 发送 `更新cookie` / `b站登录` / `bilibili登录`，Bot 返回二维码，B 站客户端扫码后 Cookie 自动更新。

## 动态推送说明

`dynamic_subscriptions` 管的是 UP 主的空间动态，与开播监测各自独立，可只订其一。

- 推送内容：投稿视频（附封面）、图文/相簿（最多 3 张图）、专栏、转发（原动态概要拼进正文）、音频、纯文字
- `@all` 仅对视频、直播卡片类生效，图文/专栏不 @全体，避免刷屏
- 判新用动态 id **数值**游标（老动态 18 位、新动态 19 位，按字符串比会判错），游标只前进不回退
- 首次见到某 UP 只记基线、不补发存量；单轮最多补推 10 条
- 节流：单 UP 间隔 2 秒；被限流整机间隔翻倍（上限 900 秒）；单 UP 连续失败按 60s/120s… 退避（上限 20 分钟）
- 静音时段内同样暂停

### 关于 Cookie

动态接口（`x/polymer/web-dynamic/v1/feed/space`）对匿名调用较严格：

| 现象（日志） | 含义 | 处理 |
| --- | --- | --- |
| `code=-636` | 不接受匿名请求 | 配 `bilibili_cookie`（私聊 Bot 发 `b站登录` 扫码最快） |
| `code=-352` / `HTTP 412` | 签名/风控 | 插件自动刷新 WBI 重试；仍失败则退避，可调大 `dynamic_check_interval` |
| `code=-799` | 请求过于频繁 | 调大间隔 |

插件已内置 WBI 签名（`wbi.py`）与 buvid 访客指纹（每日一刷），这是匿名读取动态的前提；大 V、锁内容的 UP 仍需配 Cookie。

### 置顶评论盯梢

有些 UP 会在发完视频一段时间后，在**自己视频评论区**发一条并置顶，关键信息（常为图片）藏在里面。这条晚于视频、又不在动态流里，普通动态推送拿不到。

开启方式（二选一，改的是同一份 `comment_watch_uids`）：

- **面板勾选（推荐）**：动态订阅矩阵 → 目标 UP 行的「盯置顶评论」列打勾 → 保存
- **配置页填写**：把该 UP 的 UID 填进 `comment_watch_uids`

要求该 UP 已在 `dynamic_subscriptions` 里、本群开着动态通知。之后：

- 该 UP 投稿视频后，插件在 `dyn_comment_watch_hours`（默认 2）小时内持续检查其评论区
- 出现 **UP 自己（`mid==UID`）的置顶评论**即按 `comment_notify_template` 补推一条（附图，最多 3 张），推给该视频当时的目标群
- 同一条只推一次；UP 换置顶（评论 id 变化）会再补推；`@全体` 跟随该群视频的 `@all` 设置
- 只对白名单 UP 生效（评论接口风控较严）；用旧版评论接口 `x/v2/reply`（靠 buvid，无需签名），单视频间隔 2 秒、失败退避
- 盯梢清单持久化于 `state.json`；超窗口或移出白名单即清除
- 手动验证：`/检查置顶评论 BV1V9aq6CEeR`（或传 UID）立刻拉一次并发到当前会话

## 数据存储

- 订阅、全局开关、Cookie、模板 → `data/config/astrbot_plugin_bililive_config.json`
- 运行时状态 → `data/plugin_data/astrbot_plugin_bililive/state.json`（`live_status_cache` 直播状态、`dyn_last_ids` 动态基线、`dyn_comment_watch` 评论盯梢、`up_names` 昵称缓存）
- 按群通知开关 → `.../groups.json`；群名缓存 → `.../group_names.json`

均在 AstrBot 规范目录下，卸载随目录清理。

## 获取 UP 主 UID

打开 UP 主主页，`https://space.bilibili.com/123456` 中的 `123456` 即 UID。

## 本地测试

纯逻辑（WBI 签名、动态/评论解析、订阅串、id 游标、面板转换）都拆在无副作用模块里，**不需要装 astrbot** 即可跑：

```bash
python tests/test_subscription.py      # 订阅串解析/序列化/@all
python tests/test_dynamic_report.py    # WBI 签名、动态分类与提取、id 游标
python tests/test_comment_report.py    # BV→av 换算、置顶评论解析
python tests/test_main_smoke.py        # 桩掉 astrbot 拉起 main.py，跑游标/推送/评论盯梢
python tests/test_bililive_panel.py    # Web 面板接口与订阅矩阵转换
python tests/test_groups.py            # 群名解析/缓存
```

全绿会打印 `OK`，失败以非零退出码结束，便于挂 CI。

## 关于

- 源头项目（原作者 BB0813）：[BB0813/astrbot_plugin_bilibiliobs](https://github.com/BB0813/astrbot_plugin_bilibiliobs)
- 本项目：[Lianzy-Baimiao/astrbot_plugin_bililive](https://github.com/Lianzy-Baimiao/astrbot_plugin_bililive)
