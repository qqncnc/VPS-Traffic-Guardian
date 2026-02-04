# Server Guardian - VPS 流量与连接安全守护者

这是一个专为云服务器（如阿里/腾讯/AWS）设计的自动化运维脚本，旨在通过动态流控和熔断机制保护服务器不被封禁或产生额外扣费。

### 核心功能
* **并发限制**：限制单机最大同时连接 IP 数（默认 8 个）。
* **动态降速**：检测到带宽持续高负载时，自动降速 15 分钟，规避云厂商风控。
* **流量熔断**：当日总流量超标或连接 IP 总数超标，自动执行关机。

### 快速部署
1. 下载脚本：
   `wget https://raw.githubusercontent.com/qqncnc/VPS-Traffic-Guardian/main/server_guardian.py`
2. 修改配置：
   使用 `vi server_guardian.py` 修改脚本顶部的参数（如限速值、流量上限等）。
3. 运行环境：
   `yum install -y net-tools iproute python3`
4. 运行脚本：
   `sudo python3 server_guardian.py`

### 声明
本项目仅用于企业内部跨境业务合规运维，请勿用于非法用途。
