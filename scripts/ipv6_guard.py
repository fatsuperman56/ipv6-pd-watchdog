#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
家用 Linux / NAS 的 IPv6 自愈守护（检测式）

设计要点
--------
1. 默认只「体检」，不通到阈值才动手，平时零影响。
2. 故障分层处理：
     L1 有免密 sudo -> ip link down/up 强制重发 RS，重新拿 RA（断网数秒）
     L2 无 sudo 或 L1 无效 -> 重启路由器（断网 1~2 分钟，依赖 zte_client.py）
3. 只有 IPv4 正常、单独 IPv6 不通时才动手；整网断（IPv4 也断）默认不动，
   因为那大概率是上游光猫/运营商问题，重启路由器救不了还白断网。
4. 冷却 + 每日上限，避免反复重启。

典型故障：宽带运营商静默更换 IPv6 PD 前缀，主机侧地址滞后（变 deprecated），
用旧源地址发包被运营商按源地址反欺骗（uRPF）丢弃 —— 表现成"地址有、网关通、但无回包"，
与"上游断了"的现象完全一样，极易误判。本脚本耐心等它自愈，等不到再推一把。

用法
----
    python3 ipv6_guard.py check     # 体检一次（建议由 cron 每 10 分钟调用）
    python3 ipv6_guard.py status    # 看当前状态与计数
    python3 ipv6_guard.py reset     # 清空状态
    python3 ipv6_guard.py test      # 只测连通性，不改任何状态
"""
import os
import re
import sys
import json
import time
import fcntl
import socket
import datetime
import subprocess

HOME = os.path.expanduser("~")
STATE_FILE = os.path.join(HOME, ".ipv6_guard_state.json")
LOCK_FILE = os.path.join(HOME, ".ipv6_guard.lock")
LOG_FILE = os.path.join(HOME, "ipv6_guard.log")
PWD_FILE = os.path.join(HOME, ".zte_pwd")

# ---- 可调参数 ----
FAIL_THRESHOLD = 3          # 连续失败多少次才动作（cron 10 分钟一次 = 30 分钟）
COOLDOWN_MIN = 120          # 动作后冷却分钟数
MAX_REBOOTS_PER_DAY = 3     # 每天最多重启路由器次数
V4_FAIL_THRESHOLD = 6       # IPv4 连续失败多少次（=1 小时）才考虑兜底
V4_DAILY_LIMIT = 1          # 整网断时每天最多兜底重启次数
TCP_TIMEOUT = 3

V6_TARGETS = [("2400:3200::1", 443), ("2400:3200::1", 80),
              ("ipv6.baidu.com", 443), ("www.taobao.com", 443)]
V6_MIN_OK = 2               # 至少 2 个通才算健康（防单点抖动误判）
V4_TARGETS = [("223.5.5.5", 53), ("119.29.29.29", 53)]
V4_MIN_OK = 1


# ---------------------------------------------------------------- 基础工具
def now():
    return datetime.datetime.now()


def log(msg):
    line = "[%s] %s" % (now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        # 只保留最近 800 行
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if len(lines) > 800:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines[-800:])
    except Exception:
        pass


def tcp(host, port, timeout=TCP_TIMEOUT, af=None):
    """可传域名或 IP 字面量；af 显式指定协议族（域名必须指定，否则可能走 IPv4）"""
    if af is None:
        af = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        infos = socket.getaddrinfo(host, port, af, socket.SOCK_STREAM)
    except Exception:
        return False
    for fam, typ, proto, _canon, sa in infos[:3]:
        s = None
        try:
            s = socket.socket(fam, typ, proto)
            s.settimeout(timeout)
            s.connect(sa)
            return True
        except Exception:
            pass
        finally:
            if s:
                try:
                    s.close()
                except Exception:
                    pass
    return False


def sh(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)


# ---------------------------------------------------------------- 网络探测
def iface():
    """自动找到默认 IPv6 出口网卡；找不到再退回 IPv4 默认网卡，最后给 eth0"""
    rc, out = sh("ip -6 route show default 2>/dev/null | head -3")
    m = re.search(r"dev\s+(\S+)", out)
    if m:
        return m.group(1)
    rc, out = sh("ip -4 route show default 2>/dev/null | head -3")
    m = re.search(r"dev\s+(\S+)", out)
    return m.group(1) if m else "eth0"


def v6_global_addrs(dev):
    """返回 [(地址, 状态)]，状态 preferred / deprecated"""
    rc, out = sh("ip -6 addr show dev %s scope global 2>/dev/null" % dev)
    res = []
    cur = None
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"inet6\s+([0-9a-fA-F:]+)/(\d+)", line)
        if m:
            cur = m.group(1)
            continue
        m = re.search(r"preferred_lft\s+(\d+)sec", line)
        if m and cur:
            res.append((cur, "deprecated" if int(m.group(1)) == 0 else "preferred"))
            cur = None
    return res


def check_v6(fast=False):
    """fast=True 时先测一个纯地址 target，通了立刻返回（用于自愈轮询，省时间）"""
    if fast:
        t = V6_TARGETS[0]
        if tcp(t[0], t[1], timeout=TCP_TIMEOUT, af=socket.AF_INET6):
            return True, "快测可达"
    good = [t for t in V6_TARGETS if tcp(t[0], t[1], af=socket.AF_INET6)]
    return (len(good) >= V6_MIN_OK), "%d/%d 可达" % (len(good), len(V6_TARGETS))


def check_v4():
    good = [t for t in V4_TARGETS if tcp(t[0], t[1], af=socket.AF_INET)]
    return (len(good) >= V4_MIN_OK), "%d/%d 可达" % (len(good), len(V4_TARGETS))


# ---------------------------------------------------------------- 状态
def load_state():
    st = {"v6_fail": 0, "v4_fail": 0, "last_action": "", "reboots_today": 0,
          "v4_reboots_today": 0, "day": now().strftime("%Y-%m-%d"), "history": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st.update(json.load(f))
    except Exception:
        pass
    if st.get("day") != now().strftime("%Y-%m-%d"):
        st["day"] = now().strftime("%Y-%m-%d")
        st["reboots_today"] = 0
        st["v4_reboots_today"] = 0
    return st


def save_state(st):
    st["history"] = st.get("history", [])[-30:]
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("状态写入失败：%s" % e)


def minutes_since(ts):
    if not ts:
        return 10 ** 6
    try:
        t = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        return (now() - t).total_seconds() / 60.0
    except Exception:
        return 10 ** 6


# ---------------------------------------------------------------- 动作
def ip_bin():
    rc, out = sh("command -v ip 2>/dev/null")
    return out.strip() or "/usr/bin/ip"


def l1_ready(dev, ip=None):
    """L1 是否就绪：只探测 ip 命令的免密 sudo（不能用 sudo -n true 判断，
    因为 sudoers 里可能只授权了 ip 这一条命令）"""
    ip = ip or ip_bin()
    rc, _ = sh("sudo -n %s -6 addr show dev %s >/dev/null 2>&1" % (ip, dev), timeout=8)
    return rc == 0


def try_refresh_ra(dev):
    """L1：让本机重新拿到 RA，分两小步，先无风险的再断网数秒的

    L1a  清掉全局 IPv6 地址，等路由器下一轮 RA（IPv4 / Tailscale 不受影响）
    L1b  ip link down/up 强制立刻发 RS（断网数秒，脚本在本地继续跑）
    """
    ip = ip_bin()
    if not l1_ready(dev, ip):
        log("L1 跳过：ip 命令没有免密 sudo")
        return False

    # ---- L1a：零风险，只动 IPv6 地址 ----
    log("L1a：清理 %s 的全局 IPv6 地址，等新 RA" % dev)
    sh("sudo -n %s -6 addr flush dev %s scope global" % (ip, dev), timeout=15)
    for i in range(12):          # 最多等约 120 秒
        time.sleep(10)
        ok, msg = check_v6(fast=True)
        if ok:
            log("L1a 成功：IPv6 已恢复 %s（等待 %d 秒）" % (msg, (i + 1) * 10))
            return True
    log("L1a 未恢复（约 120 秒无新地址或仍不通）")

    # ---- L1b：断网数秒，强制重发 RS ----
    log("L1b：%s down/up 强制重发 RS" % dev)
    try:
        sh("sudo -n %s link set dev %s down" % (ip, dev), timeout=15)
        time.sleep(3)
    finally:
        sh("sudo -n %s link set dev %s up" % (ip, dev), timeout=15)

    for i in range(12):
        time.sleep(10)
        ok, msg = check_v6(fast=True)
        if ok:
            log("L1b 成功：IPv6 已恢复 %s（等待 %d 秒）" % (msg, (i + 1) * 10))
            return True
    log("L1b 未恢复")
    return False


def find_zte_script():
    """L2 依赖路由器控制脚本：优先 zte_client.py，兼容旧名 zte_router.py"""
    for name in ("zte_client.py", "zte_router.py"):
        p = os.path.join(HOME, name)
        if os.path.exists(p):
            return p
    return None


def reboot_router(reason):
    """L2：重启路由器（依赖 zte_client.py / zte_router.py）"""
    pwd = os.environ.get("ZTE_PWD", "")
    if not pwd and os.path.exists(PWD_FILE):
        try:
            pwd = open(PWD_FILE, encoding="utf-8").read().strip()
        except Exception:
            pass
    if not pwd:
        log("L2 放弃：找不到路由器密码（应放 %s）" % PWD_FILE)
        return False
    zte = find_zte_script()
    if not zte:
        log("L2 放弃：找不到路由器控制脚本（zte_client.py / zte_router.py）")
        return False
    log("L2 执行：重启路由器（原因：%s），脚本=%s" % (reason, os.path.basename(zte)))
    env = dict(os.environ, ZTE_PWD=pwd)
    try:
        r = subprocess.run([sys.executable, zte, "reboot"],
                           cwd=HOME, env=env, capture_output=True, text=True, timeout=90)
        out = (r.stdout or "") + (r.stderr or "")
        log("L2 返回：%s" % out.strip().replace("\n", " ")[:220])
        return ">>> 重启请求已被接受" in out
    except Exception as e:
        log("L2 异常：%s" % e)
        return False


# ---------------------------------------------------------------- 主流程
def acquire_lock():
    """防止 cron 与手动执行重叠（自愈最坏要跑 6 分钟）"""
    try:
        f = open(LOCK_FILE, "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except Exception:
        return None


def cmd_check():
    lock = acquire_lock()
    if lock is None:
        log("上一次检测仍在运行，跳过本次")
        return 0
    st = load_state()
    dev = iface()
    v6ok, v6msg = check_v6()
    v4ok, v4msg = check_v4()
    addrs = v6_global_addrs(dev)
    pref = [a for a, s in addrs if s == "preferred"]
    dep = [a for a, s in addrs if s == "deprecated"]
    brief = "IPv6 %s | IPv4 %s | %s 上 preferred=%d deprecated=%d" % (
        v6msg, v4msg, dev, len(pref), len(dep))

    if v6ok:
        if st["v6_fail"]:
            log("恢复正常（此前连续失败 %d 次）：%s" % (st["v6_fail"], brief))
        else:
            log("正常：" + brief)
        st["v6_fail"] = 0
        st["v4_fail"] = 0
        save_state(st)
        return 0

    # IPv6 不通
    st["v6_fail"] += 1
    st.setdefault("history", []).append(
        {"t": now().strftime("%Y-%m-%d %H:%M:%S"), "v6": v6msg, "v4": v4msg})
    log("异常第 %d 次：%s | 地址：%s" % (st["v6_fail"], brief,
                                     ", ".join(pref) if pref else "无 preferred 地址"))

    # 整网断（IPv4 也不通）：默认不重启路由器
    if not v4ok:
        st["v4_fail"] += 1
        log("IPv4 也不通（第 %d 次）——大概率上游光猫/运营商问题" % st["v4_fail"])
        if (st["v4_fail"] >= V4_FAIL_THRESHOLD
                and st["v4_reboots_today"] < V4_DAILY_LIMIT
                and minutes_since(st.get("last_action")) >= COOLDOWN_MIN):
            log("整网持续不通，兜底重启路由器一次")
            if reboot_router("IPv4+IPv6 同时持续不通"):
                st["last_action"] = now().strftime("%Y-%m-%d %H:%M:%S")
                st["v4_reboots_today"] += 1
                st["reboots_today"] += 1
                st["v6_fail"] = 0
                st["v4_fail"] = 0
        save_state(st)
        return 1

    st["v4_fail"] = 0

    # 未达阈值：继续观察
    if st["v6_fail"] < FAIL_THRESHOLD:
        log("观察中（%d/%d），暂不动手" % (st["v6_fail"], FAIL_THRESHOLD))
        save_state(st)
        return 1

    # 冷却 / 每日上限
    cool = minutes_since(st.get("last_action"))
    if cool < COOLDOWN_MIN:
        log("冷却中（距上次动作 %.0f 分钟 < %d），跳过" % (cool, COOLDOWN_MIN))
        save_state(st)
        return 1
    if st["reboots_today"] >= MAX_REBOOTS_PER_DAY:
        log("今日重启已达上限 %d 次，跳过（需人工介入）" % MAX_REBOOTS_PER_DAY)
        save_state(st)
        return 1

    # 进入自愈
    log("连续 %d 次不通，启动自愈" % st["v6_fail"])
    st["last_action"] = now().strftime("%Y-%m-%d %H:%M:%S")

    if try_refresh_ra(dev):
        st["v6_fail"] = 0
        save_state(st)
        return 0

    if reboot_router("IPv6 连续 %d 次不通" % st["v6_fail"]):
        st["reboots_today"] += 1
        st["v6_fail"] = 0
        log("已重启路由器（今日第 %d 次）；90 秒后复查" % st["reboots_today"])
        save_state(st)
        time.sleep(90)
        ok, msg = check_v6()
        log("复查：%s %s" % ("已恢复" if ok else "仍不通", msg))
        return 0 if ok else 1

    log("自愈未成功，保留计数等下次")
    save_state(st)
    return 1


def cmd_status():
    st = load_state()
    dev = iface()
    v6ok, v6msg = check_v6()
    v4ok, v4msg = check_v4()
    print("时间        %s" % now().strftime("%Y-%m-%d %H:%M:%S"))
    print("网卡        %s" % dev)
    print("IPv6        %s %s" % ("通" if v6ok else "不通", v6msg))
    print("IPv4        %s %s" % ("通" if v4ok else "不通", v4msg))
    for a, s in v6_global_addrs(dev):
        print("  地址      %s (%s)" % (a, s))
    print("连续失败    v6=%d  v4=%d" % (st["v6_fail"], st["v4_fail"]))
    la = st.get("last_action")
    print("上次动作    %s" % (("%s（%.0f 分钟前）" % (la, minutes_since(la))) if la else "无"))
    print("今日重启    %d / %d" % (st["reboots_today"], MAX_REBOOTS_PER_DAY))
    print("L1 权限     %s" % ("就绪" if l1_ready(dev) else "未就绪（直接走 L2）"))
    print("阈值        %d 次连续失败，冷却 %d 分钟" % (FAIL_THRESHOLD, COOLDOWN_MIN))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        return cmd_check()
    if cmd == "status":
        cmd_status()
        return 0
    if cmd == "reset":
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        log("状态已清空")
        return 0
    if cmd == "test":
        dev = iface()
        v6ok, v6msg = check_v6()
        v4ok, v4msg = check_v4()
        print("网卡 %s | IPv6 %s %s | IPv4 %s %s" % (
            dev, "通" if v6ok else "不通", v6msg, "通" if v4ok else "不通", v4msg))
        print("L1 权限 %s" % ("就绪（%s 免密 sudo）" % ip_bin() if l1_ready(dev)
                            else "未就绪（将直接走 L2 重启路由器）"))
        for a, s in v6_global_addrs(dev):
            print("  %s (%s)" % (a, s))
        return 0 if v6ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
