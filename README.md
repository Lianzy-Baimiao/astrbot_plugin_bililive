# B站UP主开播监测插件 (astrbot_plugin_bililive)

监测指定B站UP主的开播状态，开播/关播时向你指定的群推送通知。**每个UP主可以各自配置推送到哪些群**，配置界面和聊天命令双向管理，改一处两边同步。

> 这是 `astrbot_plugin_bilibiliobs` 的重写版（v2.0.0），修掉了旧版的几个硬伤：配置界面看不到订阅、数据乱放导致卸载不清、同一个群开播被多平台重复推送、退订必须记 UID。

## 主要特性

- 🎯 **每UP各自配群**：UP-A 发甲群乙群、UP-B 只发丙群，互不影响
- 🖥️ **配置界面可视化管理**：直接在 WebUI 插件配置里增删「监控UID + 目标群」
- 💬 **聊天命令同步**：群里 `/订阅`、`/退订`、`/订阅列表` 与配置界面共用同一份数据
- 🔢 **退订支持序号**：`/订阅列表` 带序号，`/退订 2` 即可，不用记 UID
- 🚫 **不再重复推送**：目标群显式列出、裸群号只补全到单一平台，从设计上杜绝一次开播发两条
- 📁 **数据落规范目录**：`data/plugin_data/astrbot_plugin_bililive/`，卸载时能被 AstrBot 一并清理
- 🖼️ **封面图通知** / 📢 **@全体成员** / 🔔 **关播通知** / 📝 **自定义模板**
- 🍪 **Cookie 支持 + 扫码登录续期**：管理员私聊扫码自动更新 Cookie

## 配置项

在 AstrBot WebUI 的插件配置页设置：

| 配置项 | 说明 |
| --- | --- |
| `subscriptions` | **订阅列表**，每行一个UP，格式 `UID=目标[,目标...][ \| at_all]` |
| `default_platform` | 裸群号补全到哪个平台**实例id**（留空自动探测第一个平台） |
| `check_interval` | 检查间隔（秒），建议 30-300 |
| `max_monitors` | 最大监控 UP 数 |
| `enable_notifications` | 开播通知总开关 |
| `enable_end_notifications` | 关播通知开关 |
| `bilibili_cookie` | B站 Cookie（可选，提高 API 限额） |
| `live_notify_template` / `end_notify_template` | 开播/关播通知模板 |

### subscriptions 写法

每个「目标」有三种写法，短的优先：

```
111111111=777777777                # ①裸群号，按 default_platform 补全
111111111=napcat:777777777         # ②平台id:群号（推荐，可多平台混用）
111111111=napcat:GroupMessage:777777777   # ③完整 unified_msg_origin
222222222=napcat:777777777 | at_all          # 开播时@全体成员
111111111=napcat:777777777,default_666666666:777777777   # 同一UP推到多个平台
```

- 冒号前是平台的**实例 id**（在 AstrBot「配置 → 消息平台」里看，如 `napcat`、`default_666666666`），**不是**类型名 `aiocqhttp`/`qq_official`
- 末尾加 ` | at_all` 表示该UP开播时@全体成员
- 同一UID可写多行，会自动合并群列表
- 保存后会自动统一成 `平台id:群号` 简写

## 聊天命令

| 命令 | 说明 |
| --- | --- |
| `/订阅 <UID> [at_all]` | 把**当前群**加入该UP的推送目标（免手填群号） |
| `/退订 <UID或序号>` | 从当前群移除某UP，序号来自 `/订阅列表` |
| `/订阅列表` | 列出本群订阅的UP（带序号和直播状态） |
| `/检查直播 <UID>` | 手动查一次直播状态 |
| `/开播监测状态` | 查看插件运行状态、数据目录 |
| `/开启通知` `/关闭通知` | 通知总开关 |
| `/开启关播通知` `/关闭关播通知` | 关播通知开关 |

### 管理员扫码登录（私聊Bot）

私聊 Bot 发送 `更新cookie` / `b站登录` / `bilibili登录` / `更新b站cookie`，Bot 返回二维码，B站客户端扫码后 Cookie 自动更新。管理员首次私聊会自动记录身份；Cookie 失效时会私聊提醒。

## 如何获取 UP 主 UID

打开 UP 主 B站主页，URL 里 `https://space.bilibili.com/123456` 中的 `123456` 就是 UID。

## 数据存储

- 订阅、开关、Cookie、模板 → 插件配置 `data/config/astrbot_plugin_bililive_config.json`（WebUI 直接可改）
- 直播状态缓存 → `data/plugin_data/astrbot_plugin_bililive/state.json`

两者都在 AstrBot 规范目录下，卸载插件时会被清理干净。

## 版本

- **版本**: 2.0.0
- **作者**: BB0813
- **仓库**: https://github.com/BB0813/astrbot_plugin_bilibiliobs
