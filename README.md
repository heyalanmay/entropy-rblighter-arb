# Entropy ↔ rblighter 专属套利部署包

基于开源引擎 `your-quantguy/entropy-arb`（已内置 `--hedge lighter-rh` = Lighter Robinhood 链 = rblighter），
做一个**只跑 Entropy ↔ rblighter 这一个币对方向**的独立部署。引擎代码来自上游开源仓库，
本包只提供：专属配置、密钥模板、采集/实盘启动脚本、以及下面的步骤说明。

> 为什么不直接从零写一套新 bot？
> 实时套利要处理两边 order book 同步、私钥签名、IOC 下单、成交确认、链上持仓对账、断线重连、滑点保护——
> 任何一处错都会拿真金白银买单。上游引擎已经把这些写好且开源可审计，本包复用它，只在配置层锁定 rblighter，
> 既满足「独立部署 / 重新部署一个」的诉求，又避开自研下单引擎的风险。
> （若你确实想要一份完全独立的 fork 仓库，也可以把 entropy-arb fork 到你自己 GitHub 再克隆，步骤一样。）

> ⚠️ **合规底线（必读）**：本包是**真实的双向套利**——每一笔都是 Entropy 买 + rblighter 卖（或反向）同时成交，赚两边真实价差。它**不是**、也**不应该**被用来做"自成交 / 假量 / wash trading"（自己跟自己刷、制造虚假活跃）。后者违反交易所服务条款，可能导致封号、冻结资金。下面所有"提频 / 增密"手段的目标，都是"在每笔仍净赚的前提下，让真实套利成交更频繁"，而不是亏本刷数。

---

## 一、准备新服务器与新账户

1. **新服务器**：买一台 Ubuntu 22.04（腾讯云/任意云都行），用 VS Code Remote-SSH 连上（你已熟）。
2. **新 Entropy 账户（Hyperliquid）**：
   - 打开 https://app.hyperliquid.xyz/API ，新建一个 **API (agent) 钱包**。
   - 记下 `agent 私钥`（填 `HL_PRIVATE_KEY`）和 `主账户地址`（填 `HL_ACCOUNT_ADDRESS`）。
   - 在 **io dex** 的 clearinghouse 充值 **USDC**（这是 Entropy 这边开仓用的钱）。
3. **新 rblighter 账户（Lighter Robinhood 链）**：
   - 在 Robinhood 链 Lighter 部署上新建账户并生成 API key（流程见 https://github.com/elliottech/lighter-python ）。
   - 记下 `LIGHTER_ACCOUNT_INDEX` / `LIGHTER_API_KEY_INDEX` / `LIGHTER_API_PRIVATE_KEY`。
   - **关键**：这 3 个值必须是「注册在 Robinhood 链部署」的，不是你主网 Lighter 的 key。
   - 在该账户充值 **USDG**（不是 USDC）。
4. **选币对**：确认该币对**同时**在 Entropy 和 rblighter 上上线（例如 SNDK）。不确定就先采集看两边是否都有行情。

## 二、上传部署包并初始化

1. 用 VS Code 把本文件夹（`entropy-rblighter-deploy`）拖到新服务器的 `~/` 下。
2. 在服务器终端进入该文件夹，运行：
   ```bash
   bash setup.sh
   ```
   脚本会自动：装系统依赖 → 克隆引擎到 `~/entropy-rblighter` → 建 Python 环境 → 写入专属 config 与 .env 模板 → 生成启动脚本。

## 三、填写密钥

编辑 `~/entropy-rblighter/.env`，填入上面准备好的 5 个值：
```bash
nano ~/entropy-rblighter/.env
```
保存后**切勿**把 .env 提交到任何公开地方。

## 四、先采集行情（不花钱）

```bash
bash ~/entropy-rblighter/collect.sh SNDK
```
让它跑 **至少几小时（最好一天）**。期间会写 `logs/minutes.csv`。
（用 tmux 跑可断线不死：先 `tmux new -s arb`，再运行，Ctrl+B D 脱离。）

## 五、算阈值（最关键一步）

```bash
cd ~/entropy-rblighter
source .venv/bin/activate
python3 tools/analyze.py --fees-bps 2.5
```
`--fees-bps 2.5` 代表 Entropy 2.5 + rblighter 0 = 往返 2.5 bps 手续费。
把输出的 `midline_bps / upper_bps / lower_bps` 记下来。

## 六、填阈值并实盘

编辑 `~/entropy-rblighter/config.yaml` 顶部 `thresholds:` 三行，替换成 analyze 的值。
同时确认 `entropy.max_position_usd` 与 `hedge.max_position_usd` 相等，且都 ≤ 较小账户余额的 80%
（rblighter/USDG 账户通常是短板）。

然后实盘（务必用 tmux）。**推荐用 `run.sh` 而不是裸跑 `trade.sh`**：

```bash
tmux new -s arb
bash ~/entropy-rblighter/run.sh SNDK
# Ctrl+B 然后 D 脱离；回来用： tmux attach -t arb
```

`run.sh` 是常驻智能控制器，会帮你做三件事（详见「九、美股开盘避让 + 人类化」）：
- **北京时间 21:00-22:00 自动只采集、不下单**（避开美股开盘高波动）；
- **交易时段内每 15-30 分钟自动换档重启，每次注入新的随机下单参数**（含单笔下单位 `min/max` 随机），单笔名义在区间里浮动，更像真人、规避机械刷量识别；
- **进程崩溃自动拉起**。

> 只想手动单模式跑（不想要上面这些）？用 `bash ~/entropy-rblighter/trade.sh SNDK` 直接实盘，
> 或 `bash ~/entropy-rblighter/collect.sh SNDK` 只采集。但实盘请务必放进 tmux，否则 SSH 一断程序就死。

## 七、上线后盯三件事

1. **信号区**：应显示 analyze 算出的中枢与带宽。
2. **中性开仓**：每笔成交 Entropy(+) 与 rblighter(−) 数量应相等，净敞口≈0。
3. **净盈亏**：完整往返扣费后应 ≥ 0；若持续负值，说明费率/阈值没对齐，立刻停。

## 八、频率与磨损调优（低磨损刷量 + 套利获利）

核心矛盾：**成交越频繁，越容易吃掉价差利润**。"低磨损刷量又能套利"唯一的正路是：
让每笔都在「薄但为正」的利润带上成交——靠**拆小单压滑点** + **收阈值提频率**，而不是亏本刷。

本包已把"与币对无关"的低磨损默认值预设好（见 `config.entropy-rblighter.yaml`），你只需关心阈值。

### 已预设的低磨损项（无需你改，除非想进一步压）

| 参数 | 默认值 | 作用 |
|------|--------|------|
| `sizing.take_fraction` | `0.2` | 只吃盘口顶部 20% 深度，价格冲击小 → 滑点低（磨损头号来源） |
| `sizing.max_order_notional_usd` | `25` | 单笔 $25，越小滑点越低，可把阈值收更紧、成交更密 |
| `inventory.scale_bps` / `floor_frac` | `2.0` / `0.3` | 加仓变"多次小步"，成交更密、风险更平滑 |
| `execution.leg/hedge_slippage_bps` | `30` / `15` | 收窄滑点保护，避免过度支付 |
| `hedge.max_orders_per_min` | `35` | 贴近 Lighter 上限 40 以增密，留 5 余量防限频 |

### 你要调的：阈值（来自 analyze，绝不可乱填）

引擎**保证**一次完整往返扣费后净赚 ≥ `upper + lower` bps（费已扣在内）。
唯一能吃掉利润的是**滑点**（不在保证内）。所以铁律：

> **`upper + lower` 必须 ≥ 实测往返滑点的 2 倍（留安全余量）。**

- 想**更频繁** → 把 `upper/lower` 往小收（例如 analyze 建议 `2.0/1.0`，可试 `1.0/0.5`）。
  - 收太狠而滑点 > `upper+lower` 时，每笔会变亏损 → 看 `logs/trades.csv` 的滑点列验证。
- 想**更省磨损** → 把 `sizing.take_fraction` 再降到 `0.1`、`max_order_notional_usd` 降到 `10~15`。
- 滑点从这读：`logs/trades.csv`（运行一天后最准）。

### 自动写入阈值（免手敲）

采集几小时后直接：
```bash
bash ~/entropy-rblighter/tune.sh        # 自动 analyze 并把 midline/upper/lower 写回 config.yaml
```

> 费用结构提醒：Entropy（Hyperliquid）是 **taker-only**，单笔吃 2.5 bps，这是磨损底线；
> rblighter 对冲腿 0 bps。所以往返成本约 2.5 bps，阈值绝不能收到比这还低太多。
> 想再降磨损，只能靠 Hyperliquid 的成交量返佣档（超出本包范围）。

> 安全红线：永远先用小仓位（本包默认每边上限 100、单笔 25）验证程序能稳定开平仓并自平，
> 确认无误再按账户余额逐步上调 `max_position_usd`。不要一次性拉满。

---

## 九、美股开盘避让 + 人类化（反女巫 / 像真实交易员）

### 1) 为什么要在美股开盘时段避让

`SNDK` 这类**股权类永续**，在美股交易时段（北京时间约 21:30 开盘，冬令时约 22:30），
不同交易所的预言机/盘口机制差异会被放大，价差会失真、滑点会突然变大。
entropy-arb 官方文档的 Known risks 里**明确写了**：

> "for equity perps (e.g. SNDK), off-hours oracle regimes differ per venue;
>  consider wider bands or not trading them."

—— 意思是股权类永续在美股时段，要么放宽阈值、要么干脆不交易。所以你要求
「21:00-22:00 只采集、不交易」，和官方风险建议完全一致，不是多此一举。

`run.sh` 的处理方式：**21:00-22:00 切成 `--record-only`（纯采集，零风险），其余时段实盘。**
这段时间还在跑的、之前已开的持仓不会被主动管理（不加不减不平），直到 22:00 回到实盘模式，
程序从链上重新读持仓继续管。这是刻意的——开盘波动大，不动就是最稳的。

> 冬令时美股开盘更晚（约 22:30 北京），若想覆盖更全，把 `run.sh` 顶部的
> `PAUSE_END_HOUR` 改成 `23` 即可（改完重启 run.sh 生效）。

### 2) 为什么还要“人类化”

项目方（交易所）给交易员发积分/空投时，通常会筛掉**女巫（sybil）**——即机械、雷同、像脚本的行为。
本 bot 做的是**真实跨所套利**（一边买、一边卖，赚真实价差），本身**不是自成交/假量**，
所以不会触发“假量女巫”规则。但“每天 24 小时像钟表一样、每笔下单位完全一样”仍可能被当成机器人。
`run.sh` 用两个手段让它更像真人：

| 手段 | 怎么做 | 效果 |
|------|--------|------|
| **每日固定休息** | 21:00-22:00 不交易 | 像真人会避开剧烈波动、会“下班” |
| **下单数量随机（核心）** | 引擎启动时只读一次 config、没有“每笔随机”能力；`run.sh` 改为**交易时段内每 15-30 分钟自动换档重启**，每次重启注入一组新随机参数：`take_fraction`(0.15–0.28)、`max_order_notional_usd`(20–32)、`min_order_notional_usd`(5–12)、`cooldown_sec`(0.5–2.5)、`premium_persist_sec`(1–3) | 单笔名义 = `clamp(take_fraction × 盘口深度, min, max)`，盘口深度每刻变 → 单笔大小自然浮动；跨档期参数换一批 → 浮动区间整体平移。不同时间段偏大额/偏小额、下单位从不重复，不复制同一套模式 |
| **换档间隔也随机** | 每次换档的间隔在 `RESHUFFLE_MIN~RESHUFFLE_MAX`（默认 900~1800 秒）间随机取 | 避免“每隔固定 N 分钟必重启”这种机械特征 |
| **天然不规律** | 只在真实价差触发时才下单（不是定时扫） | 成交时间点天然分散，不机械 |

> **为什么必须靠“换档重启”才能随机下单位**：entropy-arb 引擎只在进程启动时读取一次 `config.yaml`（无热加载），
> 运行期间改 config 不会生效。所以唯一的干净做法是周期性重启 bot、每次写入一组新随机参数。
> 换档重启只断几秒 WebSocket（重连+重读链上持仓），套利是均值回归、几秒空窗无影响；间隔默认 15-30 分钟足够长，不会打断正常交易。
> 想更“密”可调小 `RESHUFFLE_MIN/MAX`（建议别低于 600 秒/10 分钟），想更“稳”可调大。

### 3) 用法

```bash
tmux new -s arb
bash ~/entropy-rblighter/run.sh SNDK          # 常驻：自动避让 + 人类化 + 崩溃自拉起
# Ctrl+B D 脱离
```

查看模式切换记录：`tail -f ~/entropy-rblighter/logs/run.log`
停止整个控制器：`pkill -f "run.sh" ; pkill -f "main.py --symbol SNDK"`

### 4) 想关掉人类化抖动？

编辑 `run.sh` 顶部 `HUMANIZE=1` 改成 `HUMANIZE=0`，重启 run.sh 即可（时段避让不受影响）。

---

## 十、Web 控制台（浏览器可视化操作）

不想记一堆 SSH 命令？本包内置一个**网页控制台**（FastAPI + 静态页面），把启停、看日志、看成交、改参数、自动调阈值都搬到浏览器里。外观参照 panda-arb 的暗色控制台。

### 1) 启动

```bash
cd ~/entropy-rblighter
source .venv/bin/activate
bash web.sh                      # 前台启动；或放到后台： nohup bash web.sh &

# 想要指定端口 / 绑定本地（配合 SSH 隧道更安全）：
WEB_HOST=127.0.0.1 bash web.sh 8080
```

启动后访问 `http://服务器IP:8080`（或 SSH 隧道下的 `http://localhost:8080`）。

### 2) 页面能做什么

- **Control Center 卡片**：运行状态、阈值是否已校准、采集样本数、累计双腿成交数。
- **任务列表**：支持创建、保存、启动、停止、删除多个套利任务。
- **创建套利任务**：点右上角「➕ 创建任务」弹出 panda-arb 风格表单，可填 symbol、目标盈利 bps、中枢、单笔下/上限、最大仓位、冷却、溢价持续、样本数、费率、滑点等；创建时自动写回 `config.yaml`。
- **样本安全锁**：任务可设「开始实盘样本数」，若 `minutes.csv` 样本不足，系统会阻止实盘并提示先采集；也可一键「采集模式启动」。
- **任务详情**：当前模式（RUNNING / RECORDING / PAUSED）、PID、运行时长；实时展示 `midline/upper/lower` 阈值与两边费率、单笔上限。
- **实时价差监控**：折线图读取 `logs/minutes.csv` 的 premium 列。
- **最近成交**：表格读取 `logs/trades.csv`。
- **监控日志**：实时滚动读取 `logs/live.log`（智能模式与裸实盘都落这里）。
- **按钮**：`启动任务` / `采集模式启动` / `停止` / `删除` / `紧急停止` / `自动调阈值` / `修改参数`。
- **自动调阈值**：点一下在后台跑 `tune.sh`（分析样本并写回阈值），不用 SSH 手敲。
- **修改参数**：弹窗改阈值 / 费率 / 单笔大小，保存后若 bot 在跑会自动重启生效。

### 3) 接口一览（也可被其他脚本调用）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/status` | 进程状态 + 阈值/样本概览 |
| GET | `/api/config` | 读 config.yaml |
| POST | `/api/config` | 改 config.yaml（先备份 .bak） |
| GET | `/api/logs?type=live&tail=80` | 读日志（type: live/record/run/engine/trade/tune） |
| GET | `/api/trades?limit=20` | 读成交 |
| GET | `/api/premium?limit=120` | 读价差分钟线 |
| POST | `/api/control/{action}?symbol=SNDK` | 启停（action: start_smart/stop_smart/start_trade/stop_trade/start_record/stop_record/kill_all） |
| POST | `/api/tune?symbol=SNDK` | 后台自动调阈值 |
| GET | `/api/tasks` | 列出已保存任务 |
| POST | `/api/tasks` | 创建任务（写 config + 持久化） |
| GET | `/api/tasks/{id}` | 单个任务详情 |
| POST | `/api/tasks/{id}/start?force_record=true` | 启动任务（不满足样本数时返回错误，可强制采集模式） |
| POST | `/api/tasks/{id}/stop` | 停止任务对应 symbol 的进程 |
| DELETE | `/api/tasks/{id}` | 删除任务（不自动停止进程） |
| GET | `/healthz` | 健康检查（含 `auth_required` 标记） |

### 4) 安全（务必看）

控制台默认 `0.0.0.0:8080` 且**无鉴权**。任何能访问到端口的人，都能启停你的 bot、花你的钱。二选一：

- **推荐：只绑本地 + SSH 隧道**（上面 `WEB_HOST=127.0.0.1` 那种），服务器防火墙甚至不用开 8080；
- **或加令牌**：用 `WEB_TOKEN=一段随机串 bash web.sh` 启动，前端首次访问会弹窗要令牌，之后所有 `/api` 请求带 `Authorization: Bearer <WEB_TOKEN>`。

> 这是进程级简单防护，不是完整账号系统。公网长期暴露请再加 HTTPS 反向代理 + 防火墙白名单。控制台**不持有任何私钥**，只读本地日志/配置/进程状态。

### 5) 排错

- **页面打不开**：先 `tail -f ~/entropy-rblighter/logs/web.log` 看后端报错；确认 8080 端口在防火墙放行（或用了 SSH 隧道）。
- **“Failed to fetch”**：浏览器访问的地址与后端监听地址不一致（例如后端绑了 127.0.0.1 但浏览器用了服务器公网 IP）；或 `WEB_TOKEN` 已开启但没输入令牌。
- **日志区空白但 bot 在跑**：确认 bot 是用 `run.sh` 或 `trade.sh` 启动的（它们落 `live.log`）；裸 `main.py` 直接跑不会写 `live.log`。

