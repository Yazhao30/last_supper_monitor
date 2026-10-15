# 《最后的晚餐》3 人余票监测

监测官方页面：

https://cenacolovinciano.vivaticket.it/en/event/cenacolo-vinciano/151991?idt=2547

目标日期：

- 2026-11-13
- 2026-11-14
- 2026-11-15
- 同一场次至少 3 张票

运行方式：GitHub Actions 云端每 5 分钟计划触发一次；发现满足条件的票后通过 Telegram Bot 推送到手机。

**建议把仓库设为 Public。** GitHub 对公开仓库的标准 GitHub-hosted runner 不计 Actions 分钟，而私人仓库通常有每月分钟额度；全天每 5 分钟监控会很快消耗私人仓库额度。Telegram token 和 Chat ID 只放在 GitHub Actions Secrets 中，不要提交到代码。

> 注意：GitHub Actions 的 `schedule` 不是实时调度系统。5 分钟是计划触发频率，高峰期可能延迟。脚本不会自动购票，也不会点击“加入购物车/继续支付”。

## 1. 创建 Telegram Bot

1. Telegram 搜索 `@BotFather`。
2. 发送 `/newbot`。
3. 按提示设置名称和用户名。
4. BotFather 会给你一串 token，形式类似：

   `123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxx`

   这就是 `TELEGRAM_BOT_TOKEN`。不要把它发到公开仓库，也不要写进 Python 文件。

5. 打开你刚创建的 bot，点击 `Start`，随便给它发一句 `hello`。

## 2. 获取 Telegram Chat ID

在浏览器地址栏打开：

`https://api.telegram.org/bot<你的TOKEN>/getUpdates`

把 `<你的TOKEN>` 换成 BotFather 给你的 token。

页面返回的 JSON 中找到：

`"chat":{"id":123456789,...}`

这里的数字就是 `TELEGRAM_CHAT_ID`。

如果返回 `result: []`，先回 Telegram 再给 bot 发一条消息，然后刷新上述地址。

## 3. 创建 GitHub 仓库

1. 登录 GitHub。
2. `New repository`。
3. 建议设为 **Public**（原因见上面的 Actions 分钟说明）；如果你有充足的付费/私人仓库 Actions 分钟，也可以设为 Private。
4. 名称例如：`last-supper-monitor`。
5. 把本项目中的文件上传到仓库：

   - `monitor.py`
   - `requirements.txt`
   - `.gitignore`
   - `README_CN.md`
   - `.github/workflows/monitor.yml`

如果 GitHub 网页上传时不方便创建 `.github/workflows/`：

1. 先上传前四个文件。
2. 进入仓库的 **Actions** 页面。
3. 选择 **set up a workflow yourself**。
4. 把本项目 `monitor.yml` 的内容复制进去。
5. 文件路径保持 `.github/workflows/monitor.yml`。

## 4. 在 GitHub 设置 Telegram Secrets

仓库页面：

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

建立两个 Secret：

### Secret 1

Name:

`TELEGRAM_BOT_TOKEN`

Value:

BotFather 给你的 token。

### Secret 2

Name:

`TELEGRAM_CHAT_ID`

Value:

你的 Chat ID。

## 5. 第一次手动测试

进入：

`Actions` → `Last Supper ticket monitor` → `Run workflow`

第一次建议先确认 Telegram 本身配置正确：

1. `Actions` → `Last Supper ticket monitor` → `Run workflow`。
2. `mode` 选择 **telegram-test**。
3. 运行后，手机应收到 `Last Supper monitor Telegram test`。

然后再手动运行一次，`mode` 选择 **monitor**。

如果任务失败，可再手动运行一次，`mode` 选择 **debug**。Actions 页面会生成一个名为 `vivaticket-debug-...` 的 artifact，其中包含截图、HTML 和页面文本，可用来调整网页选择器。

如果你在自己的电脑上测试，可运行：

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Windows PowerShell：

```powershell
$env:TELEGRAM_BOT_TOKEN="你的token"
$env:TELEGRAM_CHAT_ID="你的chat id"
python monitor.py --telegram-test
```

收到 Telegram：

`Last Supper monitor Telegram test`

说明 Telegram 配置正确。

GitHub Actions 正式任务运行 `python monitor.py`，没有余票时只会在 Actions 日志中记录，不给你发消息。

## 6. 程序怎样判断“3 张同场票”

程序不会仅仅因为 11 月 13/14/15 某天变成绿色就通知。

它会：

1. 打开官方 Vivaticket 页面。
2. 找到目标日期。
3. 找到该日期的可选时间。
4. 打开该时间对应的票种/数量页面。
5. 检查数量下拉框、数量上限或页面的可用数量字段。
6. 只有确认数量至少达到 3 才 Telegram 通知。

它不会自动：

- 登录你的 Vivaticket 账户；
- 填身份证/护照信息；
- 加入购物车；
- 支付；
- 自动下单。

## 7. 收到的 Telegram 大致长这样

```text
🚨 Last Supper tickets found
Need: 3 people in the same time slot

✅ Fri 13 Nov 2026 · 10:45
   Availability check: up to at least 3 selectable

Open official Vivaticket now
⚠️ Availability can disappear quickly.
```

点官方链接后自己立即购买即可。

## 8. 防止重复轰炸

脚本会把已经通知过的日期+时间写到 `state.json`，并通过 GitHub Actions Cache 在不同运行之间保留状态。

同一个场次默认 6 小时内不会重复提醒；出现新的场次会马上提醒。

## 9. 如果 Vivaticket 改网页结构怎么办

售票网站可能改版。程序已经用了多种方式寻找：

- 日期元素；
- 时间按钮/链接；
- 数量下拉框；
- number input；
- `data-availability` 等属性；
- 页面内嵌 availability JSON 字段。

如果 GitHub 的云端 IP 被 Vivaticket CAPTCHA / Access Denied 拦住，脚本会每天最多给你发一次故障提醒，而不会误报有票。

需要排查时可以在本地运行：

```bash
python monitor.py --debug --no-telegram
```

它会在 `debug/` 下保存页面截图、HTML 和文本，便于后续调整选择器。

## 10. 停止监控

最简单的方法：

GitHub 仓库 → `Actions` → `Last Supper ticket monitor` → 菜单 → `Disable workflow`。

或者删除 `.github/workflows/monitor.yml`。
