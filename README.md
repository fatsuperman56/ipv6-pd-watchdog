# ipv6-pd-watchdog

> **English abstract**
> A small, unattended self-healing watchdog for home IPv6. It periodically checks IPv6
> connectivity (TCP connect, no root needed); only when IPv6 is down for ~30 min does it act,
> escalating L1a (flush stale addresses) → L1b (interface down/up) → L2 (reboot router).
> Targets the common failure: ISP silently changes the IPv6 PD prefix, the host keeps a
> deprecated address, and outbound packets get dropped by uRPF — looks like "upstream down"
> but isn't. Tested on a 飞牛 OS / Linux NAS behind a ZTE router.

家里 IPv6 出口偶发不通（本仓库作者遇到的真实故障是**运营商静默更换 PD 前缀**，主机侧地址滞后），
在无人值守的情况下自动恢复，且平时对家里网络零打扰。

---

## ⚠️ 免责声明

本工具仅用于你**本人拥有**的设备 / 网络。请不要在未经授权的环境部署。

---

## 0. 一句话定位

一段跑在 NAS / 家用 Linux 主机上的 Python 脚本，由 cron 每 10 分钟叫醒一次，做一次 IPv6 连通性体检；
**连续 30 分钟不通**才动手，按 L1a → L1b → L2 逐级加深手段，修好就停，修不好也有次数上限。

---

## 1. 部署清单

| 文件 | 作用 | 说明 |
|---|---|---|
| `scripts/ipv6_guard.py` | 主脚本 | 检测 + 三级自愈 |
| `scripts/ipv6_check.py` | 体检脚本 | 手动排查用，无需 root |
| `zte_client.py` | 路由器控制（L2 依赖） | **来自同作者另一个仓库 [zte-router-api](https://github.com/) ，放到同一目录（家目录 `$HOME`）** |
| `~/.zte_pwd` | 路由器管理密码 | 一行纯文本，权限 600，L2 重启时读取 |
| `~/.ipv6_guard_state.json` | 运行状态 | 连续失败计数、上次动作时间、当日重启次数 |
| `~/.ipv6_guard.lock` | 并发锁 | 防止 cron 与手动执行重叠 |
| `~/ipv6_guard.log` | 业务日志 | 中文可读，自动滚存最近 800 行 |

系统侧只改一处（L1 需要 `ip` 的 root 权限，可选）：

| 文件 | 内容 |
|---|---|
| `/etc/sudoers.d/ipv6-guard` | `<你的用户> ALL=(ALL) NOPASSWD: /usr/bin/ip`（root:root 440） |

定时任务（用户级 crontab）：

```cron
*/10 * * * * /usr/bin/python3 $HOME/ipv6_guard.py check >> $HOME/ipv6_guard.cron.log 2>&1
```

> 不配置 L1 权限也能跑：脚本会自动降级到 L2（重启路由器）。L1 只是代价更小（断网数秒 vs 1~2 分钟）。

---

## 2. 安装步骤（可复现）

### 2.1 传脚本

```bash
# 本仓库
scp scripts/ipv6_guard.py scripts/ipv6_check.py <你的主机>:~/
# zte_client.py 来自 zte-router-api 仓库，同样放到家目录
scp zte_client.py <你的主机>:~/
ssh <你的主机> "cd ~ && chmod +x ipv6_guard.py ipv6_check.py zte_client.py"
```

### 2.2 存路由器密码

```bash
ssh <你的主机> "printf '%s' '你的路由器密码' > ~/.zte_pwd && chmod 600 ~/.zte_pwd"
```

### 2.3 （可选）开 L1 权限

L1 需要 `ip` 的 root 权限。标准做法（需要你能输 sudo 密码）：

```bash
echo '<你的用户> ALL=(ALL) NOPASSWD: /usr/bin/ip' | sudo tee /etc/sudoers.d/ipv6-guard
sudo chmod 440 /etc/sudoers.d/ipv6-guard
sudo chown root:root /etc/sudoers.d/ipv6-guard
```

> **附注（免密 sudo 不可用时的替代方案）**：如果你的用户在 `docker` 组（等价 root），但 `sudo` 要密码，
> 可以用一次性容器把上面那个文件写进去：
> ```bash
> # 先准备内容
> ssh <你的主机> "printf '<你的用户> ALL=(ALL) NOPASSWD: /usr/bin/ip\n' > ~/ipv6-guard.sudoers"
> # 用本地任意镜像挂载 /etc/sudoers.d 写入（容器默认 root）
> ssh <你的主机> "docker run --rm -v \$HOME/ipv6-guard.sudoers:/src:ro -v /etc/sudoers.d:/dst <本地镜像> \
>   sh -c 'cp /src /dst/ipv6-guard && chmod 440 /dst/ipv6-guard && chown 0:0 /dst/ipv6-guard'"
> ```
>
> 注意两个坑：
> 1. 很多发行版（含飞牛 OS）上 `ip` 在 **`/usr/bin/ip`**，不是 `/usr/sbin/ip`。
> 2. 只授权了 `ip` 之后，**`sudo -n true` 仍然会要密码**。脚本判断 L1 是否可用，
>    探的是 `sudo -n /usr/bin/ip …`，不是 `sudo -n true`。

验证：

```bash
ssh <你的主机> "sudo -n /usr/bin/ip -6 addr show dev <网卡名> | head -2"   # 有输出 = 就绪
```

### 2.4 装定时任务

```bash
ssh <你的主机> "(crontab -l 2>/dev/null | grep -v ipv6_guard; \
  echo '*/10 * * * * /usr/bin/python3 \$HOME/ipv6_guard.py check >> \$HOME/ipv6_guard.cron.log 2>&1') | crontab -"
```

### 2.5 验收

```bash
ssh <你的主机> "cd ~ && python3 ipv6_guard.py test"     # 应输出 IPv6 通 x/4、L1 权限状态
ssh <你的主机> "cd ~ && python3 ipv6_guard.py check"    # 应写一条"正常"日志
```

---

## 3. 原理

### 3.1 为什么需要它：故障长什么样

作者遇到的真实故障链（用文档前缀 `2001:db8::/64` 举例）：

```
运营商更换 IPv6 PD 委派前缀
    2001:db8:abcd:1234::/64  →  2001:db8:abcd:5678::/64
              ↓
主机接口上仍是旧前缀地址（后来变成 deprecated）
              ↓
用旧源地址发包，运营商按源地址反欺骗（uRPF）丢弃
              ↓
现象：地址有、网关 REACHABLE、ping/traceroute 全部无回包
     与"上游断了"的现象完全一样，极易误判
              ↓
路由器下发新前缀 RA 后，主机拿到新地址 → 自动恢复
```

所以这类故障**大多数会自愈**，只是滞后时间不确定（几分钟到几十分钟）。
脚本的作用是：耐心等它自愈（30 分钟），等不到再人工推一把。

> **排查铁律**：看到"IPv6 无回包"，先 `ip -6 addr` 看接口上有没有 **preferred 状态的新前缀地址**，
> 别光看 traceroute（前缀过期和上游断路，第 3 跳全 `*` 一模一样）。

### 3.2 检测层

每 10 分钟跑一次，做 4 类判断：

| 项目 | 做法 |
|---|---|
| IPv6 连通 | TCP 连 4 个目标：`2400:3200::1:443`、`2400:3200::1:80`、`ipv6.baidu.com:443`、`www.taobao.com:443` |
| 健康口径 | **≥2/4 通**才算健康（单点抖动不会被误判成故障） |
| IPv4 连通 | TCP 连 `223.5.5.5:53`、`119.29.29.29:53`，≥1 通即可 |
| 地址状态 | 解析 `ip -6 addr`，统计 preferred / deprecated 地址个数，写进日志 |
| 探测方式 | 用 TCP connect 而不用 ICMP，**不需要 root**，也不被 ICMP 限速干扰 |

> 目标地址是实测筛出来的。`240c::6666`（CNNIC）、`2408:8000::1`（联通）、
> `240e:ff:f100:1::1`（腾讯）在作者网络下都不可达，所以没进默认集；你可按需替换。

### 3.3 判定层（什么时候才动手）

```
IPv6 通                    → 计数清零，记一条"正常"，结束
IPv6 不通，IPv4 也断       → 大概率上游光猫/运营商问题，默认【不动手】
                              仅当持续 1 小时且当日未兜底过 → 重启路由器 1 次
IPv6 不通，IPv4 正常       → 计数 +1
    计数 < 3（不足 30 分钟）→ 继续观察，动手太早只会白白断网
    冷却中（120 分钟内动过）→ 跳过
    今日已重启 3 次         → 跳过，等人工处理
    以上都不拦              → 进入自愈
```

设计取向很明确：**宁可晚 30 分钟修好，也不要为了几分钟的抖动每天断一次网。**

### 3.4 自愈三层

| 级别 | 命令 | 作用机理 | 代价 |
|---|---|---|---|
| **L1a** | `sudo ip -6 addr flush dev <网卡> scope global` | 清掉接口上所有全局 IPv6 地址（含过期前缀的），让内核重新走一遍 RA 流程，用**当前有效前缀**重建地址。不动 IPv4、不动 VPN | IPv6 中断到新 RA 到达（实测 1~2 分钟） |
| **L1b** | `sudo ip link set dev <网卡> down` → 3 秒 → `up` | 网卡重新初始化会**立刻发出 Router Solicitation（RS）**，路由器马上回 RA，不用等周期性 RA（可能 200~600 秒一轮） | **全网断 3~10 秒**，VPN 会自己重连 |
| **L2** | `python3 zte_client.py reboot` | 登录路由器调重启接口，整机重启后 LAN 侧重新广播 RA | 断网 1~2 分钟 |

关键设计：**L1a 先做**。它零风险（IPv4 不断、VPN 不断、SSH 不会掉），
成功了就不必走到 L1b/L2。只有 L1a 等满 120 秒还不通，才升到 L1b。

L1b 的 `down/up` 用 `try/finally` 包住，**即使中途异常也保证把网卡 up 回来**，避免主机彻底失联。

### 3.5 保护机制

| 机制 | 值 | 作用 |
|---|---|---|
| 连续失败阈值 | 3 次（=30 分钟） | 过滤瞬时抖动 |
| 动作冷却 | 120 分钟 | 防止"重启→不通→再重启"死循环 |
| 每日重启上限 | 3 次 | 超过就停止动作，留日志等人工 |
| 整网断不动作 | 默认 | IPv4 也断时重启路由器救不了，只会雪上加霜 |
| 并发锁 | `fcntl` 非阻塞 | 自愈最坏跑 6 分钟，防止与下一次 cron 叠加 |
| 快测优化 | `check_v6(fast=True)` | 自愈轮询时先测 1 个目标，通了立即返回，避免每轮 4 目标×3 秒把时间拖长 |

最坏耗时估算：L1a 120 秒 + L1b 120 秒 + 重启后复查 90 秒 ≈ **6 分钟**，小于 10 分钟的 cron 周期。

---

## 4. 执行方式

### 4.1 自动（cron）

每 10 分钟一次 `check`。脚本自己处理加锁、写日志、更新状态文件，cron 只负责叫醒它。

### 4.2 手动

```bash
python3 ipv6_guard.py status   # 当前连通性 + 计数 + L1 是否就绪
python3 ipv6_guard.py test     # 只测连通，不改状态
python3 ipv6_guard.py check    # 跑一次完整判定（会改状态）
python3 ipv6_guard.py reset    # 清空计数（换网络/人工排除后）
tail -30 ipv6_guard.log        # 看历史
python3 ipv6_check.py          # 手动体检（无需 root）
```

### 4.3 退出码

| 码 | 含义 |
|---|---|
| 0 | 正常，或本次自愈成功 |
| 1 | 不通，且本次未恢复（部分场景需人工） |
| 2 | 参数错误 |

### 4.4 状态文件长什么样

```json
{
  "v6_fail": 0,            // 连续 IPv6 失败次数
  "v4_fail": 0,            // 连续 IPv4 失败次数
  "last_action": "",       // 上次动手时间（用于冷却）
  "reboots_today": 0,      // 今日重启路由器次数
  "v4_reboots_today": 0,   // 今日整网断兜底次数
  "day": "2026-10-24",     // 跨天自动清零
  "history": []            // 最近 30 次异常快照
}
```

---

## 5. 用户感知

### 5.1 平时（99% 的时间）：完全无感

- 每 10 分钟建立 4 个 TCP 连接、每个几 KB，跑完就关，**流量可以忽略**。
- 不常驻进程、不占 CPU、不读硬盘（只在写日志时碰一下）。
- 家里上网、看视频、下载全程无影响。

### 5.2 真出故障时的时间线

| 阶段 | 时长 | 你的感受 |
|---|---|---|
| 故障发生 → 脚本发现 | 0~10 分钟 | IPv6 不通，但**日常上网基本无感**：现代系统走 Happy Eyeballs，IPv6 试几百毫秒不通就回退 IPv4 |
| 观察期（3 次连续失败） | 30 分钟 | 同上，IPv4 一切正常。**这段时间脚本故意什么都不做** |
| L1a（清地址等 RA） | 1~2 分钟 | IPv6 完全中断；IPv4、VPN、远程访问全不受影响 |
| L1b（网卡 down/up） | **3~10 秒** | 全网短暂断一下，像眨个眼；正在下的东西会断流后重连 |
| L2（重启路由器） | 1~2 分钟 | 全家断网，WiFi 断开重连，光猫不受影响 |
| 修好后 | — | 脚本自动记一条"恢复正常（此前连续失败 N 次）：4/4 可达" |

### 5.3 三种典型剧本

**剧本 A：自愈型（占多数）**
前缀变了 → 20 分钟里主机自己拿到新 RA → 脚本在观察期就发现恢复了 → 记一条"恢复正常"，全程没动过网。

**剧本 B：地址卡死型**
30 分钟都不通 → L1a 清地址等新 RA → 通常 1~2 分钟恢复 → **IPv4 一次都没断**。

**剧本 C：路由器僵死型**
L1a 120 秒无效 → L1b 断网 3 秒重协商 → 还不行 → L2 重启路由器 1~2 分钟。
若当天已重启 3 次仍不通，脚本**停止动作**，只在日志里留痕，等你人工介入（这时候多半是光猫/运营商的事）。

### 5.4 你怎么知道它干过活

```bash
grep -E 'L1a|L1b|已重启|恢复正常' ipv6_guard.log | tail -20
```

正常时日志只有一行行"正常：IPv6 4/4 可达"，看起来平淡就是最好的状态。

---

## 6. 运维手册

### 临时关掉

```bash
(crontab -l 2>/dev/null | grep -v ipv6_guard) | crontab -
```

### 重新打开

```bash
(crontab -l 2>/dev/null | grep -v ipv6_guard; \
 echo '*/10 * * * * /usr/bin/python3 $HOME/ipv6_guard.py check >> $HOME/ipv6_guard.cron.log 2>&1') | crontab -
```

### 收回 sudo 授权

```bash
sudo rm -f /etc/sudoers.d/ipv6-guard
# 或用容器：docker run --rm -v /etc/sudoers.d:/dst <镜像> rm -f /dst/ipv6-guard
```

### 调参（改脚本顶部常量）

| 常量 | 现值 | 调整建议 |
|---|---|---|
| `FAIL_THRESHOLD` | 3 | 想更快介入改 2；想更稳改 4~6 |
| `COOLDOWN_MIN` | 120 | 家里网络差可调 60 |
| `MAX_REBOOTS_PER_DAY` | 3 | 不建议超过 3，重启治不了上游问题 |
| `V6_MIN_OK` | 2 | 目标集换了要一起调 |
| `TCP_TIMEOUT` | 3 | 别调太大，否则自愈轮询会变慢 |

### 已知边界（脚本治不了的情况）

1. **光猫 PPPoE 真的断了** —— IPv4 也会断，脚本默认不动手，得你拔光猫电源。
2. **运营商侧路由/防火墙问题** —— 三级手段都无效，只能报障。
3. **sudoers 被改** —— L1 会自动降级到 L2（日志里会写"L1 跳过"），不会报错卡住。
4. **docker 服务挂了** —— 不影响已装好的 sudoers，L1 仍可用。

---

## 7. 许可

[MIT](./LICENSE)。发布前请把 `LICENSE` 里的版权所有者替换为你的 GitHub 用户名或姓名。
依赖的路由器控制脚本见 [zte-router-api](https://github.com/)（同作者）。
