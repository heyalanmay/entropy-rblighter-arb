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

然后实盘（务必用 tmux）：
```bash
tmux new -s arb
bash ~/entropy-rblighter/trade.sh SNDK
# Ctrl+B 然后 D 脱离；回来用： tmux attach -t arb
```

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
