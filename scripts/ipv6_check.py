#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""家用 IPv6 体检：地址 / 默认网关 / ND 状态 / 出口 TCP 连通 / 收发计数。无需 root。

网卡默认自动探测（取 IPv6 默认路由所在网卡）；可用环境变量 CHECK_IFACE 强制指定。

典型用途：排查"IPv6 地址有、但出网无回包"这类故障 —— 先确认接口上有没有
preferred 状态的新前缀地址，再怀疑上游。很多情况下旧前缀地址变成 deprecated、
新前缀地址迟迟不来，就是运营商静默更换 PD 前缀所致。
"""
import os
import re
import socket
import subprocess
import time

CHECK_IFACE = os.environ.get("CHECK_IFACE")  # 留空则自动探测
TARGETS = [
    ("AliDNS", "2400:3200::1", 53),
    ("CNNIC", "240c::6666", 53),
    ("腾讯", "240e:ff:f100:1::1", 80),
]


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout.strip()


def detect_iface():
    if CHECK_IFACE:
        return CHECK_IFACE
    for fam in ("-6", "-4"):
        out = sh("ip", fam, "route", "show", "default")
        m = re.search(r"dev\s+(\S+)", out)
        if m:
            return m.group(1)
    return "eth0"


def snmp6(key):
    try:
        for line in open("/proc/net/snmp6"):
            if line.startswith(key):
                return int(line.split()[-1])
    except Exception:
        pass
    return 0


IFACE = detect_iface()
print("=" * 54)
print("1) 本机 IPv6 全局地址（网卡：%s）" % IFACE)
for line in sh("ip", "-6", "addr", "show", IFACE, "scope", "global").splitlines():
    if "inet6" in line:
        print("   " + line.strip())

print("\n2) IPv6 默认路由")
for line in sh("ip", "-6", "route").splitlines():
    if line.startswith("default"):
        print("   " + line)
        gw = line.split("via")[1].split()[0] if "via" in line else ""
        if gw:
            print("   网关 ND 状态: " + (sh("ip", "-6", "neigh", "show", gw) or "(无条目)"))

print("\n3) 出口 TCP 连通性")
alive = 0
for name, ip, port in TARGETS:
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.settimeout(5)
    t0 = time.time()
    try:
        s.connect((ip, port))
        print("   %-8s [%s]:%-3d CONNECT OK  %.1fs" % (name, ip, port, time.time() - t0))
        alive += 1
    except Exception as e:
        print("   %-8s [%s]:%-3d FAIL %s" % (name, ip, port, type(e).__name__))
    finally:
        s.close()

print("\n4) 5 秒收发计数（判断是「发得出去没收回来」还是「根本没发」）")
o1, i1 = snmp6("Ip6OutRequests"), snmp6("Ip6InReceives")
time.sleep(5)
o2, i2 = snmp6("Ip6OutRequests"), snmp6("Ip6InReceives")
print("   Ip6OutRequests %+d   Ip6InReceives %+d" % (o2 - o1, i2 - i1))

print("\n结论: " + ("IPv6 出口正常" if alive >= 2 else "IPv6 出口不通（%d/%d 成功）" % (alive, len(TARGETS))))
