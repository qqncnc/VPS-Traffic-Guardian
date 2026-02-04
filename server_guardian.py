#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Server Guardian - 流量与连接数自动化管理脚本
功能：
1. 限制最大并发 IP 连接数
2. 每日唯一 IP 访问数量超标自动关机
3. 动态流量整形：检测到持续高流量时自动降速，防止被云厂商风控
4. 每日总流量超标自动关机
"""

import os
import sys
import time
import subprocess
import datetime
import threading

# ================= 配置区域 (User Configuration) =================

# 网卡名称 (通常是 eth0, ens33 等，使用 ifconfig 查看)
INTERFACE = "eth0"

# 1. 连接数限制策略
MAX_CONCURRENT_IPS = 8  # 单机允许同时连接的最大 IP 数

# 2. 每日 IP 数量熔断策略
MAX_DAILY_UNIQUE_IPS = 15 # 当天连接过的唯一 IP 数量超过此值，关机

# 3. 带宽限制策略 (单位: mbit)
MAX_SPEED_LIMIT = 150     # 正常情况下的最大带宽限制
THROTTLE_SPEED_LIMIT = 60 # 触发降速后的带宽限制

# 4. 动态降速触发逻辑
TRIGGER_SPEED = 100       # 触发检测的速率阈值 (mbit)
TRIGGER_DURATION = 10     # 连续超过阈值多少秒触发降速 (秒)
PUNISH_DURATION = 900     # 降速持续时间 (15分钟 = 900秒)

# 5. 每日流量熔断策略 (单位: GB)
MAX_DAILY_TRAFFIC = 100   # 当天流量超过此值，关机
TRAFFIC_RESET_HOUR = 0    # 流量统计重置时间 (0点)

# 数据持久化文件路径 (用于记录当天的 IP 和流量)
DATA_FILE = "/var/log/server_guardian.dat"

# =================================================================

class ServerGuardian:
    def __init__(self):
        self.punish_end_time = 0
        self.high_load_duration = 0
        self.current_state = "NORMAL" # NORMAL or THROTTLED
        self.daily_ips = set()
        self.daily_traffic_bytes = 0
        self.last_check_date = datetime.date.today()
        
        # 初始化检查
        if os.geteuid() != 0:
            print("Error: 必须以 root 权限运行此脚本。")
            sys.exit(1)
            
    def run_cmd(self, cmd):
        """执行系统命令"""
        try:
            subprocess.run(cmd, shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            pass # 忽略部分重复规则添加的错误

    def init_iptables(self):
        """初始化 iptables 限制并发连接数"""
        print(f"[Init] 设置最大并发 IP 连接数为: {MAX_CONCURRENT_IPS}")
        # 清理旧规则 (简单处理，防止重复堆叠)
        self.run_cmd(f"iptables -D INPUT -p tcp --syn -m connlimit --connlimit-above {MAX_CONCURRENT_IPS} -j REJECT")
        # 添加新规则
        self.run_cmd(f"iptables -A INPUT -p tcp --syn -m connlimit --connlimit-above {MAX_CONCURRENT_IPS} -j REJECT")

    def set_tc_speed(self, speed_mbit):
        """使用 tc 设置网卡速度"""
        # 清除旧的 tc 规则
        self.run_cmd(f"tc qdisc del dev {INTERFACE} root")
        # 添加新的 TBF (Token Bucket Filter) 规则
        # burst 值设为带宽的 buffer，通常设大一点防止瞬间丢包
        cmd = f"tc qdisc add dev {INTERFACE} root tbf rate {speed_mbit}mbit burst 32kbit latency 400ms"
        self.run_cmd(cmd)
        print(f"[TC] 带宽限制已调整为: {speed_mbit} Mbps")

    def get_current_traffic(self):
        """读取 /sys/class/net 下的流量统计"""
        try:
            with open(f"/sys/class/net/{INTERFACE}/statistics/rx_bytes", 'r') as f:
                rx = int(f.read())
            with open(f"/sys/class/net/{INTERFACE}/statistics/tx_bytes", 'r') as f:
                tx = int(f.read())
            return rx + tx
        except Exception as e:
            print(f"Error reading network stats: {e}")
            return 0

    def get_active_ips(self):
        """获取当前连接的 IP 列表"""
        try:
            # 使用 netstat 获取 established 的连接
            output = subprocess.check_output("netstat -nt | grep ESTABLISHED | awk '{print $5}' | cut -d: -f1", shell=True)
            ips = output.decode().strip().split('\n')
            return [ip for ip in ips if ip]
        except:
            return []

    def check_daily_reset(self):
        """检查是否跨天，重置计数器"""
        today = datetime.date.today()
        if today != self.last_check_date:
            print("[Reset] 新的一天，重置 IP 记录和流量统计。")
            self.daily_ips.clear()
            self.daily_traffic_bytes = 0
            self.last_check_date = today
            # 如果之前因为流量关机了，这里实际上脚本重启后会自动重置

    def shutdown_server(self, reason):
        """执行关机操作"""
        print(f"[DANGER] 触发关机策略: {reason}")
        print("正在执行关机...")
        # 为了防止立即关机导致无法查看日志，可以写入日志文件
        with open("/var/log/server_shutdown.log", "a") as f:
            f.write(f"{datetime.datetime.now()} - Shutdown triggered by: {reason}\n")
        
        # 解除限速以便可能的最后通信? 不，直接关机。
        self.run_cmd("shutdown -h now")
        sys.exit(0)

    def main_loop(self):
        self.init_iptables()
        self.set_tc_speed(MAX_SPEED_LIMIT)
        
        last_traffic = self.get_current_traffic()
        
        print(f"Server Guardian 已启动，监听接口: {INTERFACE}")
        
        while True:
            # 1. 跨天检查
            self.check_daily_reset()
            
            # 2. 流量速率计算
            time.sleep(1)
            current_traffic = self.get_current_traffic()
            # 增量 (Bytes)
            diff_traffic = current_traffic - last_traffic
            last_traffic = current_traffic
            
            # 累加每日流量
            self.daily_traffic_bytes += diff_traffic
            
            # 转换为 Mbps (Bytes * 8 / 1024 / 1024)
            current_speed_mbps = (diff_traffic * 8) / (1024 * 1024)
            
            # 3. 每日流量上限检查
            daily_gb = self.daily_traffic_bytes / (1024 * 1024 * 1024)
            if daily_gb > MAX_DAILY_TRAFFIC:
                self.shutdown_server(f"当日流量达到 {daily_gb:.2f} GB，超过上限 {MAX_DAILY_TRAFFIC} GB")
            
            # 4. 每日 IP 数量检查
            active_ips = self.get_active_ips()
            for ip in active_ips:
                self.daily_ips.add(ip)
            
            if len(self.daily_ips) > MAX_DAILY_UNIQUE_IPS:
                self.shutdown_server(f"当日连接 IP 数达到 {len(self.daily_ips)} 个，超过上限 {MAX_DAILY_UNIQUE_IPS}")

            # 5. 动态降速逻辑
            now = time.time()
            
            # 如果处于惩罚期
            if self.current_state == "THROTTLED":
                remaining = self.punish_end_time - now
                if remaining <= 0:
                    print("[Recover] 惩罚结束，恢复正常网速。")
                    self.set_tc_speed(MAX_SPEED_LIMIT)
                    self.current_state = "NORMAL"
                    self.high_load_duration = 0
                else:
                    # 惩罚期间不需要做额外检测
                    pass
            
            # 如果处于正常期
            else:
                if current_speed_mbps > TRIGGER_SPEED:
                    self.high_load_duration += 1
                    # print(f"[Warn] 高负载检测: {current_speed_mbps:.2f} Mbps (持续 {self.high_load_duration}s)")
                    if self.high_load_duration >= TRIGGER_DURATION:
                        print(f"[Punish] 检测到连续 {TRIGGER_DURATION} 秒超过 {TRIGGER_SPEED} Mbps，执行降速！")
                        self.set_tc_speed(THROTTLE_SPEED_LIMIT)
                        self.current_state = "THROTTLED"
                        self.punish_end_time = now + PUNISH_DURATION
                else:
                    # 计数器归零（或者缓慢衰减，这里直接归零比较简单）
                    self.high_load_duration = 0

if __name__ == "__main__":
    try:
        guardian = ServerGuardian()
        guardian.main_loop()
    except KeyboardInterrupt:
        print("Stopping Server Guardian...")
        # 退出时可以选择清理规则，或者保留
        # subprocess.run(f"tc qdisc del dev {INTERFACE} root", shell=True)
