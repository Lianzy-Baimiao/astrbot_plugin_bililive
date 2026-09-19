# B站UP主开播监测插件 (astrbot_plugin_bililive)

监测指定B站UP主的开播状态，开播/关播时向你指定的群推送通知。**每个UP主可以各自配置推送到哪些群**，配置界面和聊天命令双向管理，改一处两边同步。

> 🔱 **Fork 说明**：本项目基于原项目 [BB0813/astrbot_plugin_bilibiliobs](https://github.com/BB0813/astrbot_plugin_bilibiliobs) 修改而来。
> 原项目的 v2.0.0 重写版修掉了旧版的几个硬伤：配置界面看不到订阅、数据乱放导致卸载不清、同一个群开播被多平台重复推送、退订必须记 UID。
> 本仓库在其基础上继续开发（v2.1.0）：新增按群通知开关、`admin_ids` 管理员鉴权、静音时段、封面图开关、`@all` 精确到群等功能，并修复了扫码登录入口不可达、私聊判定失效等多个问题，详见文末更新日志。

## 主要特性

- 🎯 **每UP各自配群**：UP-A 发甲群乙群、UP-B 只发丙群，互不影响
- 🖥️ **配置界面可视化管理**：直接在 WebUI 插件配置里增删「监控UID + 目标群」
- 💬 **聊天命令同步**：群里 `/订阅`、`/退订`、`/订阅列表` 与配置界面共用同一份数据
- 🔢 **退订支持序号**：`/订阅列表` 带序号，`/退订 2` 即可，不用记 UID
- 🚫 **不再重复推送**：目标群显式列出、裸群号只补全到单一平台，从设计上杜绝一次开播发两条
- 🏷️ **@全体按群控制**：每个目标可单独加 `@all`，A 群 @全体不影响 B 群
- 🔕 **通知按群开关**：群管理可用命令只关本群通知，不影响其他群；另有全局静音时段
- 🛡️ **管理员鉴权**：通知开关、扫码登录受 `admin_ids` 控制，Cookie 失效自动私聊提醒管理员
- 📁 **数据落规范目录**：`data/plugin_data/astrbot_plugin_bililive/`，卸载时能被 AstrBot 一并清理
- 🖼️ **封面图通知（可关）** / 🔔 **关播通知** / 📝 **自定义模板（写错不炸推送）**
- 🍪 **Cookie 支持 + 扫码登录续期**：管理员私聊扫码自动更新 Cookie

## 配置项

在 AstrBot WebUI 的插件配置页设置：

| 配置项 | 说明 |
| --- | --- |
| `subscriptions` | **订阅列表**，每行一个UP，格式 `UID=目标[,目标...]`，目标可带 `@all` 后缀 |
| `default_platform` | 裸群号补全到哪个平台**实例id**（留空自动探测第一个平台） |
| `check_interval` | 检查间隔（秒），实际生效钳制在 30-600 |
| `max_monitors` | 最大监控 UP 数 |
| `enable_notifications` | 开播通知全局总开关（单群开关用群里命令控制） |
| `enable_end_notifications` | 关播通知全局总开关 |
| `send_cover` | 推送是否附带直播封面图 |
| `quiet_hours` | 静音时段 `HH:MM-HH:MM`（可选，支持跨零点），该时段暂停检查与推送 |
| `admin_ids` | 管理员QQ号列表：通知开关命令鉴权、Cookie失效提醒、扫码登录鉴权 |
| `bilibili_cookie` | B站 Cookie（可选，提高 API 限额） |
| `live_notify_template` / `end_notify_template` | 开播/关播通知模板 |

### subscriptions 写法

每个「目标」有三种写法，短的优先：

```
111111111=777777777                # ①裸群号，按 default_platform 补全
111111111=napcat:777777777         # ②平台id:群号（推荐，可多平台混用）
111111111=napcat:GroupMessage:777777777   # ③完整 unified_msg_origin
222222222=napcat:777777777@all               # 只对该群开播@全体成员
111111111=napcat:777777777,default_666666666:777777777   # 同一UP推到多个平台
# 以 # 开头的行是注释
```

- 冒号前是平台的**实例 id**（在 AstrBot「配置 → 消息平台」里看，如 `napcat`、`default_666666666`），**不是**类型名 `aiocqhttp`/`qq_official`
- 目标后加 `@all` 表示该群开播时@全体成员；旧写法「行尾 ` | at_all`」（整行所有群@全体）仍兼容
- 同一UID可写多行，会自动合并群列表
- 格式错误的行会被忽略，在日志和 `/开播监测状态` 里提示
- 保存后会自动统一成 `平台id:群号` 简写

## 聊天命令

| 命令 | 说明 |
| --- | --- |
| `/订阅 <UID> [at_all]` | 把**当前群**加入该UP的推送目标（免手填群号） |
| `/退订 <UID或序号>` | 从当前群移除某UP，序号来自 `/订阅列表` |
| `/订阅列表` | 列出本群订阅的UP（带序号和直播状态） |
| `/检查直播 <UID>` | 手动查一次直播状态 |
| `/开播监测状态` | 查看运行状态与生效配置（通知开关、默认平台、坏行警告等） |
| `/开启通知` `/关闭通知` | **本群**通知开关（仅管理员，不影响其他群） |
| `/开启关播通知` `/关闭关播通知` | **本群**关播通知开关（仅管理员） |

### 管理员

在插件配置 `admin_ids` 里填管理员QQ号（可多个）。管理员可以：

- 在群里用 `/开启通知` `/关闭通知` 等命令控制**本群**通知（其他群不受影响）
- 私聊 Bot 扫码登录续期 Cookie
- Cookie 失效时会收到私聊提醒

未配置 `admin_ids` 时，命令回退用 AstrBot 全局管理员判定；Cookie 失效提醒退回本次运行期间记录过的管理员私聊。

### 管理员扫码登录（私聊Bot）

私聊 Bot 发送 `更新cookie` / `b站登录` / `bilibili登录` / `更新b站cookie`，Bot 返回二维码，B站客户端扫码后 Cookie 自动更新。管理员首次私聊会自动记录身份；Cookie 失效时会私聊提醒。

## 如何获取 UP 主 UID

打开 UP 主 B站主页，URL 里 `https://space.bilibili.com/123456` 中的 `123456` 就是 UID。

## 数据存储

- 订阅、全局开关、Cookie、模板 → 插件配置 `data/config/astrbot_plugin_bililive_config.json`（WebUI 直接可改）
- 直播状态缓存 → `data/plugin_data/astrbot_plugin_bililive/state.json`
- 按群通知开关 → `data/plugin_data/astrbot_plugin_bililive/groups.json`（群里命令写入，卸载随目录清理）

两者都在 AstrBot 规范目录下，卸载插件时会被清理干净。

## 关于

- **本仓库（Fork 修改版）**: [Lianzy-Baimiao/astrbot_plugin_bililive](https://github.com/Lianzy-Baimiao/astrbot_plugin_bililive)
- **原项目**: [BB0813/astrbot_plugin_bilibiliobs](https://github.com/BB0813/astrbot_plugin_bilibiliobs)（作者 BB0813）
- **当前版本**: 2.1.0

### 2.1.0 更新（Fork 修改版）

- 修复：扫码登录入口不可达——原 `@filter.command("")` 全拦写法永远无法命中（空命令名 + 指令过滤器要求唤醒前缀），改用官方 `@filter.event_message_type(PRIVATE_MESSAGE)` 过滤器，登录命令命中后 `stop_event` 消费
- 修复：插件初始化改用官方 `initialize()` 生命周期钩子（原在 `__init__` 里手动 `asyncio.create_task`）
- 修复：删除不存在的 `context.get_platform_insts()` 调用，平台探测只走官方 `platform_manager.platform_insts`
- 修复：扫码登录的私聊判定改为按消息类型判断（旧实现要求平台 id 里含 `qq_`，napcat/aiocqhttp 等平台的私聊无法触发登录）
- 修复：Cookie 失效提醒按 Cookie 去重，重试轮询期间不再刷屏；直播标题的 HTML 实体（`&quot;` 等）转义后推送
- 通知开关改为按群生效（`/关闭通知` 只关本群），并增加管理员鉴权（`admin_ids`）
- `@all` 精确到群：目标级 `napcat:777777777@all`，旧「行尾 ` | at_all`」写法兼容
- 新增静音时段 `quiet_hours`、封面图开关 `send_cover`
- Cookie 失效提醒不再依赖运行期记录，配置 `admin_ids` 即可跨重启生效；扫码登录加鉴权
- 通知模板容错：占位符写错原样保留、花括号不配对按原文发送，不再静默失败
- `check_interval` 钳制到 30-600 秒；`/开播监测状态` 展示生效配置与无法解析的配置行
