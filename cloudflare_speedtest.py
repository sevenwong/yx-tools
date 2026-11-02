#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cloudflare SpeedTest 跨平台自动化脚本
支持 Windows、Linux、macOS (Darwin)
支持完整的 Cloudflare 数据中心机场码映射
"""

import os
import sys
import time
import math
import threading
import platform
import subprocess
import requests
import json
import csv
import argparse
from pathlib import Path
from datetime import datetime


# 节点名称是否带测速值
# False：只写地区名（并发测速的绝对值会被本机带宽压缩，写进名字是失真的）
NAME_WITH_SPEED = False


# ── 阶段计时（仅诊断用，不参与任何判断）────────────────────────────
# 整轮 779 秒此前完全没有分段数据，只能靠两轮实测相减去猜，结果把瓶颈
# 归到了 colo 探测上（实测只占 6 秒）。打点后每段真实占比直接可见。
_STAGE_T0 = time.time()
_STAGE_LAST = [_STAGE_T0]


def stage_timer(label):
    """打印"距启动累计耗时 / 距上一个打点耗时"，用于定位耗时瓶颈"""
    now = time.time()
    print(f"⏱️  [stage] {label:<26} 累计 {now - _STAGE_T0:7.1f}s   本段 {now - _STAGE_LAST[0]:7.1f}s")
    _STAGE_LAST[0] = now


def build_node_name(region_name, speed):
    if NAME_WITH_SPEED:
        return f"{region_name}-{speed:.2f}MB/s"
    return f"{region_name}"


# 使用curl的备用HTTP请求函数（解决SSL模块不可用的问题）
def curl_request(url, method='GET', data=None, headers=None, timeout=30):
    """
    使用curl命令进行HTTP请求（当requests的SSL模块不可用时使用）
    
    Args:
        url: 请求的URL
        method: HTTP方法（GET, POST, DELETE等）
        data: 请求数据（将被转换为JSON）
        headers: 请求头字典
        timeout: 超时时间（秒）
    
    Returns:
        dict: 包含status_code、json、text等属性的响应对象模拟
    """
    cmd = ['curl', '-s', '-w', '\\n%{http_code}', '-X', method, '--connect-timeout', str(timeout)]
    
    # 添加请求头
    if headers:
        for key, value in headers.items():
            cmd.extend(['-H', f'{key}: {value}'])
    
    # 添加请求数据
    if data:
        json_data = json.dumps(data)
        cmd.extend(['-d', json_data])
    
    # 添加URL
    cmd.append(url)
    
    try:
        # 执行curl命令，指定编码为utf-8
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
        output = result.stdout
        
        # 分离响应体和状态码
        lines = output.strip().split('\n')
        if len(lines) >= 1:
            status_code = int(lines[-1])
            response_text = '\n'.join(lines[:-1])
        else:
            status_code = 0
            response_text = output
        
        # 创建响应对象模拟
        class CurlResponse:
            def __init__(self, status_code, text):
                self.status_code = status_code
                self.text = text
                self._json = None
            
            def json(self):
                if self._json is None:
                    self._json = json.loads(self.text) if self.text else {}
                return self._json
        
        return CurlResponse(status_code, response_text)
    
    except subprocess.TimeoutExpired:
        raise Exception("请求超时，请检查网络连接")
    except subprocess.CalledProcessError as e:
        raise Exception(f"curl命令执行失败: {e}")
    except FileNotFoundError:
        raise Exception("curl命令未找到，请确保系统已安装curl")
    except Exception as e:
        raise Exception(f"curl请求失败: {e}")


# Cloudflare 数据中心完整机场码映射
# 数据来源：Cloudflare 官方数据中心列表
AIRPORT_CODES = {
    # 亚太地区 - 中国及周边
    "HKG": {"name": "香港", "region": "亚太", "country": "中国香港"},
    "TPE": {"name": "台北", "region": "亚太", "country": "中国台湾"},
    
    # 亚太地区 - 日本
    "NRT": {"name": "东京成田", "region": "亚太", "country": "日本"},
    "KIX": {"name": "大阪", "region": "亚太", "country": "日本"},
    "ITM": {"name": "大阪伊丹", "region": "亚太", "country": "日本"},
    "FUK": {"name": "福冈", "region": "亚太", "country": "日本"},
    
    # 亚太地区 - 韩国
    "ICN": {"name": "首尔仁川", "region": "亚太", "country": "韩国"},
    
    # 亚太地区 - 东南亚
    "SIN": {"name": "新加坡", "region": "亚太", "country": "新加坡"},
    "BKK": {"name": "曼谷", "region": "亚太", "country": "泰国"},
    "HAN": {"name": "河内", "region": "亚太", "country": "越南"},
    "SGN": {"name": "胡志明市", "region": "亚太", "country": "越南"},
    "MNL": {"name": "马尼拉", "region": "亚太", "country": "菲律宾"},
    "CGK": {"name": "雅加达", "region": "亚太", "country": "印度尼西亚"},
    "KUL": {"name": "吉隆坡", "region": "亚太", "country": "马来西亚"},
    "RGN": {"name": "仰光", "region": "亚太", "country": "缅甸"},
    "PNH": {"name": "金边", "region": "亚太", "country": "柬埔寨"},
    
    # 亚太地区 - 南亚
    "BOM": {"name": "孟买", "region": "亚太", "country": "印度"},
    "DEL": {"name": "新德里", "region": "亚太", "country": "印度"},
    "MAA": {"name": "金奈", "region": "亚太", "country": "印度"},
    "BLR": {"name": "班加罗尔", "region": "亚太", "country": "印度"},
    "HYD": {"name": "海得拉巴", "region": "亚太", "country": "印度"},
    "CCU": {"name": "加尔各答", "region": "亚太", "country": "印度"},
    
    # 亚太地区 - 澳洲
    "SYD": {"name": "悉尼", "region": "亚太", "country": "澳大利亚"},
    "MEL": {"name": "墨尔本", "region": "亚太", "country": "澳大利亚"},
    "BNE": {"name": "布里斯班", "region": "亚太", "country": "澳大利亚"},
    "PER": {"name": "珀斯", "region": "亚太", "country": "澳大利亚"},
    "AKL": {"name": "奥克兰", "region": "亚太", "country": "新西兰"},
    
    # 北美地区 - 美国西海岸
    "LAX": {"name": "洛杉矶", "region": "北美", "country": "美国"},
    "SJC": {"name": "圣何塞", "region": "北美", "country": "美国"},
    "SEA": {"name": "西雅图", "region": "北美", "country": "美国"},
    "SFO": {"name": "旧金山", "region": "北美", "country": "美国"},
    "PDX": {"name": "波特兰", "region": "北美", "country": "美国"},
    "SAN": {"name": "圣地亚哥", "region": "北美", "country": "美国"},
    "PHX": {"name": "凤凰城", "region": "北美", "country": "美国"},
    "LAS": {"name": "拉斯维加斯", "region": "北美", "country": "美国"},
    
    # 北美地区 - 美国东海岸
    "EWR": {"name": "纽瓦克", "region": "北美", "country": "美国"},
    "IAD": {"name": "华盛顿", "region": "北美", "country": "美国"},
    "BOS": {"name": "波士顿", "region": "北美", "country": "美国"},
    "PHL": {"name": "费城", "region": "北美", "country": "美国"},
    "ATL": {"name": "亚特兰大", "region": "北美", "country": "美国"},
    "MIA": {"name": "迈阿密", "region": "北美", "country": "美国"},
    "MCO": {"name": "奥兰多", "region": "北美", "country": "美国"},
    
    # 北美地区 - 美国中部
    "ORD": {"name": "芝加哥", "region": "北美", "country": "美国"},
    "DFW": {"name": "达拉斯", "region": "北美", "country": "美国"},
    "IAH": {"name": "休斯顿", "region": "北美", "country": "美国"},
    "DEN": {"name": "丹佛", "region": "北美", "country": "美国"},
    "MSP": {"name": "明尼阿波利斯", "region": "北美", "country": "美国"},
    "DTW": {"name": "底特律", "region": "北美", "country": "美国"},
    "STL": {"name": "圣路易斯", "region": "北美", "country": "美国"},
    "MCI": {"name": "堪萨斯城", "region": "北美", "country": "美国"},
    
    # 北美地区 - 加拿大
    "YYZ": {"name": "多伦多", "region": "北美", "country": "加拿大"},
    "YVR": {"name": "温哥华", "region": "北美", "country": "加拿大"},
    "YUL": {"name": "蒙特利尔", "region": "北美", "country": "加拿大"},
    
    # 欧洲地区 - 西欧
    "LHR": {"name": "伦敦", "region": "欧洲", "country": "英国"},
    "CDG": {"name": "巴黎", "region": "欧洲", "country": "法国"},
    "FRA": {"name": "法兰克福", "region": "欧洲", "country": "德国"},
    "AMS": {"name": "阿姆斯特丹", "region": "欧洲", "country": "荷兰"},
    "BRU": {"name": "布鲁塞尔", "region": "欧洲", "country": "比利时"},
    "ZRH": {"name": "苏黎世", "region": "欧洲", "country": "瑞士"},
    "VIE": {"name": "维也纳", "region": "欧洲", "country": "奥地利"},
    "MUC": {"name": "慕尼黑", "region": "欧洲", "country": "德国"},
    "DUS": {"name": "杜塞尔多夫", "region": "欧洲", "country": "德国"},
    "HAM": {"name": "汉堡", "region": "欧洲", "country": "德国"},
    
    # 欧洲地区 - 南欧
    "MAD": {"name": "马德里", "region": "欧洲", "country": "西班牙"},
    "BCN": {"name": "巴塞罗那", "region": "欧洲", "country": "西班牙"},
    "MXP": {"name": "米兰", "region": "欧洲", "country": "意大利"},
    "FCO": {"name": "罗马", "region": "欧洲", "country": "意大利"},
    "ATH": {"name": "雅典", "region": "欧洲", "country": "希腊"},
    "LIS": {"name": "里斯本", "region": "欧洲", "country": "葡萄牙"},
    
    # 欧洲地区 - 北欧
    "ARN": {"name": "斯德哥尔摩", "region": "欧洲", "country": "瑞典"},
    "CPH": {"name": "哥本哈根", "region": "欧洲", "country": "丹麦"},
    "OSL": {"name": "奥斯陆", "region": "欧洲", "country": "挪威"},
    "HEL": {"name": "赫尔辛基", "region": "欧洲", "country": "芬兰"},
    
    # 欧洲地区 - 东欧
    "WAW": {"name": "华沙", "region": "欧洲", "country": "波兰"},
    "PRG": {"name": "布拉格", "region": "欧洲", "country": "捷克"},
    "BUD": {"name": "布达佩斯", "region": "欧洲", "country": "匈牙利"},
    "OTP": {"name": "布加勒斯特", "region": "欧洲", "country": "罗马尼亚"},
    "SOF": {"name": "索非亚", "region": "欧洲", "country": "保加利亚"},
    
    # 中东地区
    "DXB": {"name": "迪拜", "region": "中东", "country": "阿联酋"},
    "TLV": {"name": "特拉维夫", "region": "中东", "country": "以色列"},
    "BAH": {"name": "巴林", "region": "中东", "country": "巴林"},
    "AMM": {"name": "安曼", "region": "中东", "country": "约旦"},
    "KWI": {"name": "科威特", "region": "中东", "country": "科威特"},
    "DOH": {"name": "多哈", "region": "中东", "country": "卡塔尔"},
    "MCT": {"name": "马斯喀特", "region": "中东", "country": "阿曼"},
    
    # 南美地区
    "GRU": {"name": "圣保罗", "region": "南美", "country": "巴西"},
    "GIG": {"name": "里约热内卢", "region": "南美", "country": "巴西"},
    "EZE": {"name": "布宜诺斯艾利斯", "region": "南美", "country": "阿根廷"},
    "BOG": {"name": "波哥大", "region": "南美", "country": "哥伦比亚"},
    "LIM": {"name": "利马", "region": "南美", "country": "秘鲁"},
    "SCL": {"name": "圣地亚哥", "region": "南美", "country": "智利"},
    
    # 非洲地区
    "JNB": {"name": "约翰内斯堡", "region": "非洲", "country": "南非"},
    "CPT": {"name": "开普敦", "region": "非洲", "country": "南非"},
    "CAI": {"name": "开罗", "region": "非洲", "country": "埃及"},
    "LOS": {"name": "拉各斯", "region": "非洲", "country": "尼日利亚"},
    "NBO": {"name": "内罗毕", "region": "非洲", "country": "肯尼亚"},
    "ACC": {"name": "阿克拉", "region": "非洲", "country": "加纳"},
}

# 在线机场码列表URL（GitHub社区维护）
AIRPORT_CODES_URL = "https://raw.githubusercontent.com/cloudflare/cf-ui/master/packages/colo-config/src/data.json"
AIRPORT_CODES_FILE = "airport_codes.json"

# 上报前的IP 可用性验证（WS 握手）
# 用实际使用的 Worker 域名和 WS 路径做握手，期望返回 101。
# 背景：Cloudflare 边缘对部分 IP 段访问第三方 Worker 域名会返回 "error code: 1034"，
# 此时测速仍能测出高速度（因为测的是 speed.cloudflare.com 自家CDN），但节点不可用。
WORKER_VERIFY_DOMAIN = "by.linkup6.xyz"
WORKER_VERIFY_PATH = "/?ed=2048"

# Cloudflare 网段级 WS 可用性白名单（实测结论，非官方文档）
#
# 数据来源：CloudflareST -dd 延迟测速采到 1169 个真实可用 IP（覆盖 20 个 /16 段），
#   每段抽 6 个做 WS 握手（--noproxy '*'），2026-10-18 实测 108 个样本：
#
#   ✅ 100% 可用（42/42）
#      104.16 104.17 104.18 104.19 104.20 104.21 104.24 104.25 104.27
#      172.66  172.67
#   ⚠️ 大部分可用（偶发 403，段内不一致，建议不放进白名单）
#      141.101 4/5    188.114 4/4    190.93 3/3    172.64 5/6
#   ❌ 全部被拒（0/16）
#      108.162 0/5    162.159 0/5    198.41 0/3    172.65 0/2
#   ❓ 样本不足
#      103.31 (1个可用)   173.245 (1/3)
#
# 现象要点：
#   1. 被拒 IP 访问 cloudflare.com / speed.cloudflare.com 都正常（301/200），
#      **只有访问 Worker 域名才403 + "error code: 1034"**，Worker 代码未执行。
#   2. 同一段内也可能不一致（172.64 有1/6 被拒，173.245 有1/3 可用），
#      所以段级白名单只是优化，**不能替代上传前的逐个握手验证**。
#   3. 000 = 该 IP 未分配（连不上），与 403 被拦是两回事，统计时必须分开。
#
# 至于 Cloudflare 内部按什么标准划分这些段：**无公开资料，未验证，不做推测。**
# 白名单是实测归纳，可能随 CF 策略变化失效。
# 白名单清空时 build_ws_verified_ip_list() 返回 False，自动退回全量模式 + 逐个验证。
WS_OK_CIDR_PREFIXES = [
    "104.16.", "104.17.", "104.18.", "104.19.", "104.20.", "104.21.",
    "104.24.", "104.25.", "104.27.",
    "172.66.", "172.67.",
]

# Cloudflare IP列表URL和文件
CLOUDFLARE_IP_URL = "https://www.cloudflare.com/ips-v4/"
CLOUDFLARE_IP_FILE = "Cloudflare.txt"
CLOUDFLARE_IPV6_URL = "https://www.cloudflare.com/ips-v6/"
CLOUDFLARE_IPV6_FILE = "Cloudflare_ipv6.txt"

# 默认测速URL
# 注意：speed.cloudflare.com/__down 对 bytes >= 10,000,000 直接返回 429（响应体 1 字节），
# 会导致下载测速全部 0.00 MB/s、被 -sl 门槛全过滤。这里用 9,999,999 贴着上限，实测 200。
DEFAULT_SPEEDTEST_URL = "https://speed.cloudflare.com/__down?bytes=9999999"

# Cloudflare IPv6 地址段（内置）
# 数据来源：https://www.cloudflare.com/ips-v6/
CLOUDFLARE_IPV6_RANGES = [
    # 主要地址段
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
    
    # 详细子网段
    "2400:cb00:2049::/48",
    "2400:cb00:f00e::/48",
    "2606:4700:10::/48",
    "2606:4700:130::/48",
    "2606:4700:3000::/48",
    "2606:4700:3001::/48",
    "2606:4700:3002::/48",
    "2606:4700:3003::/48",
    "2606:4700:3004::/48",
    "2606:4700:3005::/48",
    "2606:4700:3006::/48",
    "2606:4700:3007::/48",
    "2606:4700:3008::/48",
    "2606:4700:3009::/48",
    "2606:4700:3010::/48",
    "2606:4700:3011::/48",
    "2606:4700:3012::/48",
    "2606:4700:3013::/48",
    "2606:4700:3014::/48",
    "2606:4700:3015::/48",
    "2606:4700:3016::/48",
    "2606:4700:3017::/48",
    "2606:4700:3018::/48",
    "2606:4700:3019::/48",
    "2606:4700:3020::/48",
    "2606:4700:3021::/48",
    "2606:4700:3022::/48",
    "2606:4700:3023::/48",
    "2606:4700:3024::/48",
    "2606:4700:3025::/48",
    "2606:4700:3026::/48",
    "2606:4700:3027::/48",
    "2606:4700:3028::/48",
    "2606:4700:3029::/48",
    "2606:4700:3030::/48",
    "2606:4700:3031::/48",
    "2606:4700:3032::/48",
    "2606:4700:3033::/48",
    "2606:4700:3034::/48",
    "2606:4700:3035::/48",
    "2606:4700:3036::/48",
    "2606:4700:3037::/48",
    "2606:4700:3038::/48",
    "2606:4700:3039::/48",
    "2606:4700:a0::/48",
    "2606:4700:a1::/48",
    "2606:4700:a8::/48",
    "2606:4700:a9::/48",
    "2606:4700:a::/48",
    "2606:4700:b::/48",
    "2606:4700:c::/48",
    "2606:4700:d0::/48",
    "2606:4700:d1::/48",
    "2606:4700:d::/48",
    "2606:4700:e0::/48",
    "2606:4700:e1::/48",
    "2606:4700:e2::/48",
    "2606:4700:e3::/48",
    "2606:4700:e4::/48",
    "2606:4700:e5::/48",
    "2606:4700:e6::/48",
    "2606:4700:e7::/48",
    "2606:4700:e::/48",
    "2606:4700:f1::/48",
    "2606:4700:f2::/48",
    "2606:4700:f3::/48",
    "2606:4700:f4::/48",
    "2606:4700:f5::/48",
    "2606:4700:f::/48",
    "2803:f800:50::/48",
    "2803:f800:51::/48",
    "2a06:98c1:3100::/48",
    "2a06:98c1:3101::/48",
    "2a06:98c1:3102::/48",
    "2a06:98c1:3103::/48",
    "2a06:98c1:3104::/48",
    "2a06:98c1:3105::/48",
    "2a06:98c1:3106::/48",
    "2a06:98c1:3107::/48",
    "2a06:98c1:3108::/48",
    "2a06:98c1:3109::/48",
    "2a06:98c1:310a::/48",
    "2a06:98c1:310b::/48",
    "2a06:98c1:310c::/48",
    "2a06:98c1:310d::/48",
    "2a06:98c1:310e::/48",
    "2a06:98c1:310f::/48",
    "2a06:98c1:3120::/48",
    "2a06:98c1:3121::/48",
    "2a06:98c1:3122::/48",
    "2a06:98c1:3123::/48",
    "2a06:98c1:3200::/48",
    "2a06:98c1:50::/48",
    "2a06:98c1:51::/48",
    "2a06:98c1:54::/48",
    "2a06:98c1:58::/48",
]

# GitHub Release版本 - 使用官方CloudflareSpeedTest
GITHUB_VERSION = "v2.3.4"
GITHUB_REPO = "XIU2/CloudflareSpeedTest"

# 配置文件路径
CONFIG_FILE = ".cloudflare_speedtest_config.json"

# 保存交互模式下生成的命令（用于定时任务）
LAST_GENERATED_COMMAND = None


def generate_ipv6_file():
    """生成 IPv6 地址列表文件"""
    try:
        with open(CLOUDFLARE_IPV6_FILE, 'w', encoding='utf-8') as f:
            for ipv6_range in CLOUDFLARE_IPV6_RANGES:
                f.write(ipv6_range + '\n')
        print(f"✅ IPv6 地址列表已生成: {CLOUDFLARE_IPV6_FILE}")
        print(f"   共 {len(CLOUDFLARE_IPV6_RANGES)} 个 IPv6 地址段")
        return True
    except Exception as e:
        print(f"❌ 生成 IPv6 地址列表失败: {e}")
        return False


def get_system_info():
    """获取系统信息"""
    system = platform.system().lower()
    machine = platform.machine().lower()
    
    # 标准化系统名称
    if system == "darwin":
        os_type = "darwin"
    elif system == "linux":
        os_type = "linux"
    elif system == "windows":
        os_type = "win"
    else:
        print(f"不支持的操作系统: {system}")
        if sys.platform == "win32":
            input("按 Enter 键退出...")
        sys.exit(1)
    
    # 标准化架构名称
    if machine in ["x86_64", "amd64", "x64"]:
        arch_type = "amd64"
    elif machine in ["arm64", "aarch64"]:
        arch_type = "arm64"
    elif machine in ["armv7l", "armv6l"]:
        arch_type = "arm"
    else:
        print(f"不支持的架构: {machine}")
        if sys.platform == "win32":
            input("按 Enter 键退出...")
        sys.exit(1)
    
    return os_type, arch_type


def get_executable_name(os_type, arch_type):
    """获取可执行文件名 - 使用官方命名规则"""
    if os_type == "win":
        return f"CloudflareST_windows_{arch_type}.exe"
    elif os_type == "darwin":
        return f"CloudflareST_darwin_{arch_type}"
    else:  # linux
        return f"CloudflareST_linux_{arch_type}"


def download_file(url, filename):
    """下载文件 - 支持多种下载方法"""
    print(f"正在下载: {url}")
    
    # 方法1: 尝试使用 requests（SSL不可用时静默切换到curl）
    try:
        try:
            response = requests.get(url, stream=True, timeout=60)
            response.raise_for_status()
            
            with open(filename, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            print(f"✅ 下载完成: {filename}")
            return True
        except ImportError as e:
            # SSL模块不可用，静默切换到curl下载
            if "SSL module is not available" in str(e):
                result = subprocess.run([
                    "curl", "-L", "-o", filename, url
                ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
                
                if result.returncode == 0 and os.path.exists(filename):
                    print(f"✅ 下载完成: {filename}")
                    return True
            else:
                raise
    except Exception:
        # 静默失败，继续尝试其他方法
        pass
    
    # 方法2: 尝试使用 wget
    try:
        result = subprocess.run([
            "wget", "-O", filename, url
        ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
        
        if result.returncode == 0 and os.path.exists(filename):
            print(f"✅ 下载完成: {filename}")
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # wget 不可用，静默继续
        pass
    except Exception:
        # wget 执行失败，静默继续
        pass
    
    # 方法3: 尝试使用 curl
    try:
        result = subprocess.run([
            "curl", "-L", "-o", filename, url
        ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
        
        if result.returncode == 0 and os.path.exists(filename):
            print(f"✅ 下载完成: {filename}")
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # curl 不可用，静默继续
        pass
    except Exception:
        # curl 执行失败，静默继续
        pass
    
    # 方法3.5: Windows PowerShell 下载
    if sys.platform == "win32":
        try:
            ps_cmd = f'Invoke-WebRequest -Uri "{url}" -OutFile "{filename}"'
            result = subprocess.run([
                "powershell", "-Command", ps_cmd
            ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
            
            if result.returncode == 0 and os.path.exists(filename):
                print(f"✅ 下载完成: {filename}")
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            # PowerShell 不可用，静默继续
            pass
        except Exception:
            # PowerShell 执行失败，静默继续
            pass
    
    # 方法4: 尝试使用 urllib
    try:
        import urllib.request
        urllib.request.urlretrieve(url, filename)
        print(f"✅ 下载完成: {filename}")
        return True
    except Exception:
        # urllib 下载失败，静默继续
        pass
    
    # 方法5: 尝试 HTTP 版本
    if url.startswith("https://"):
        http_url = url.replace("https://", "http://")
        try:
            try:
                response = requests.get(http_url, stream=True, timeout=60)
                response.raise_for_status()
                
                with open(filename, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                
                print(f"✅ 下载完成: {filename}")
                return True
            except ImportError as e:
                # SSL模块不可用，静默切换到curl下载
                if "SSL module is not available" in str(e):
                    result = subprocess.run([
                        "curl", "-L", "-o", filename, http_url
                    ], capture_output=True, text=True, timeout=60)
                    
                    if result.returncode == 0 and os.path.exists(filename):
                        print(f"✅ 下载完成: {filename}")
                        return True
                else:
                    raise
        except Exception:
            # HTTP 下载失败，静默继续
            pass
    
    # 所有方法都失败
    print("❌ 下载失败")
    return False


def download_cloudflare_speedtest(os_type, arch_type):
    """下载 CloudflareSpeedTest 可执行文件（优先使用反代版本）"""
    # 优先检查反代版本
    if os_type == "win":
        proxy_exec_name = f"CloudflareST_proxy_{os_type}_{arch_type}.exe"
    else:
        proxy_exec_name = f"CloudflareST_proxy_{os_type}_{arch_type}"
    
    if os.path.exists(proxy_exec_name):
        print(f"✓ 使用反代版本: {proxy_exec_name}")
        return proxy_exec_name
    
    # 检查是否已下载反代版本
    print("反代版本不存在，开始下载反代版本...")
    
    # 构建下载URL - 使用您的GitHub仓库
    if os_type == "win":
        if arch_type == "amd64":
            archive_name = "CloudflareST_proxy_windows_amd64.zip"
        else:
            archive_name = "CloudflareST_proxy_windows_386.zip"
    elif os_type == "darwin":
        if arch_type == "amd64":
            archive_name = "CloudflareST_proxy_darwin_amd64.zip"
        else:
            archive_name = "CloudflareST_proxy_darwin_arm64.zip"
    else:  # linux
        if arch_type == "amd64":
            archive_name = "CloudflareST_proxy_linux_amd64.tar.gz"
        elif arch_type == "386":
            archive_name = "CloudflareST_proxy_linux_386.tar.gz"
        else:  # arm64
            archive_name = "CloudflareST_proxy_linux_arm64.tar.gz"
    
    download_url = f"https://github.com/byJoey/CloudflareSpeedTest/releases/download/v1.0/{archive_name}"
    
    if not download_file(download_url, archive_name):
        # 备用方案: 尝试 HTTP 下载
        http_url = download_url.replace("https://", "http://")
        if not download_file(http_url, archive_name):
            # 所有自动下载都失败，提供手动下载说明
            print("\n" + "="*60)
            print("自动下载失败，请手动下载反代版本:")
            print(f"下载地址: {download_url}")
            print(f"解压后文件名应为: CloudflareST_proxy_{os_type}_{arch_type}{'.exe' if os_type == 'win' else ''}")
            print("="*60)
            
            # 检查是否有手动下载的反代版本文件
            if os_type == "win":
                proxy_exec_name = f"CloudflareST_proxy_{os_type}_{arch_type}.exe"
            else:
                proxy_exec_name = f"CloudflareST_proxy_{os_type}_{arch_type}"
            
            if os.path.exists(proxy_exec_name):
                print(f"找到手动下载的反代版本: {proxy_exec_name}")
                # 手动下载的文件也需要赋予执行权限
                if os_type != "win":
                    os.chmod(proxy_exec_name, 0o755)
                    print(f"已赋予执行权限: {proxy_exec_name}")
                return proxy_exec_name
            else:
                print("未找到反代版本文件，程序无法继续")
                if sys.platform == "win32":
                    input("按 Enter 键退出...")
                sys.exit(1)
    else:
        # 解压文件
        print(f"正在解压: {archive_name}")
        try:
            if archive_name.endswith('.zip'):
                import zipfile
                with zipfile.ZipFile(archive_name, 'r') as zip_ref:
                    zip_ref.extractall('.')
            elif archive_name.endswith('.tar.gz'):
                import tarfile
                with tarfile.open(archive_name, 'r:gz') as tar_ref:
                    tar_ref.extractall('.')
            
            # 查找反代版本可执行文件
            found_executable = None
            for root, dirs, files in os.walk('.'):
                for file in files:
                    if file.startswith('CloudflareST_proxy_') and not file.endswith(('.zip', '.tar.gz')):
                        found_executable = os.path.join(root, file)
                        break
                if found_executable:
                    break
            
            if found_executable:
                # 获取最终文件名 - 使用标准格式
                if os_type == "win":
                    final_name = f"CloudflareST_proxy_{os_type}_{arch_type}.exe"
                else:
                    final_name = f"CloudflareST_proxy_{os_type}_{arch_type}"
                
                # 如果文件不在当前目录或文件名不匹配，移动到当前目录并重命名
                if os.path.abspath(found_executable) != os.path.abspath(final_name):
                    if os.path.exists(final_name):
                        os.remove(final_name)
                    # 确保源文件存在
                    if os.path.exists(found_executable):
                        os.rename(found_executable, final_name)
                    else:
                        print(f"❌ 源文件不存在: {found_executable}")
                        if sys.platform == "win32":
                            input("按 Enter 键退出...")
                        sys.exit(1)
                
                # 设置执行权限
                if os_type != "win":
                    os.chmod(final_name, 0o755)
                
                print(f"✓ 反代版本设置完成: {final_name}")
                return final_name
            else:
                print("解压后未找到反代版本可执行文件")
                # 列出解压后的所有文件用于调试
                print("解压后的文件:")
                for root, dirs, files in os.walk('.'):
                    for file in files:
                        if not file.endswith(('.zip', '.tar.gz', '.txt', '.md')):
                            print(f"  - {os.path.join(root, file)}")
                if sys.platform == "win32":
                    input("按 Enter 键退出...")
                sys.exit(1)
            
            # 清理压缩包
            os.remove(archive_name)
            
        except Exception as e:
            print(f"解压失败: {e}")
            if sys.platform == "win32":
                input("按 Enter 键退出...")
            sys.exit(1)
    
    # 在Unix系统上赋予执行权限
    if os_type != "win":
        os.chmod(proxy_exec_name, 0o755)
        print(f"已赋予执行权限: {proxy_exec_name}")
    
    return proxy_exec_name


def select_ip_version():
    """选择IP版本（IPv4或IPv6）"""
    print("\n" + "=" * 60)
    print(" IP 版本选择")
    print("=" * 60)
    print("  1. IPv4 - 测试 IPv4 地址（推荐，兼容性最好）")
    print("  2. IPv6 - 测试 IPv6 地址（需要本地网络支持IPv6）")
    print("=" * 60)
    
    while True:
        choice = input("\n请选择 IP 版本 [1/2，默认：1]: ").strip()
        if not choice or choice == "1":
            print("✓ 已选择: IPv4")
            return "ipv4", CLOUDFLARE_IP_FILE
        elif choice == "2":
            print("✓ 已选择: IPv6")
            return "ipv6", CLOUDFLARE_IPV6_FILE
        else:
            print("✗ 请输入 1 或 2")


def download_cloudflare_ips(ip_version="ipv4", ip_file=CLOUDFLARE_IP_FILE):
    """下载或生成 Cloudflare IP 列表
    
    Args:
        ip_version: IP版本 ("ipv4" 或 "ipv6")
        ip_file: IP文件路径
    """
    # 2026-10-18: 优先使用 WS 可用网段生成候选池。
    # 全量 CF 网段只有约 13% 能连上 Worker（error 1034），直接测速会浪费时间并污染排名。
    # 生成失败则退回原有逻辑（下载全量列表），上传前仍会逐个握手验证作为兜底。
    if ip_version != "ipv6" and WS_OK_CIDR_PREFIXES:
        try:
            if build_ws_verified_ip_list():
                return True
        except Exception as e:
            print(f"⚠️  生成 WS 可用候选池异常，将使用全量 IP 列表: {e}")

    # 检查文件是否已存在
    if os.path.exists(ip_file):
        print(f"✅ 使用已有IP文件: {ip_file}")
        return True

    if ip_version == "ipv6":
        # IPv6 使用内置地址段生成
        print("正在生成 Cloudflare IPv6 地址列表...")
        return generate_ipv6_file()
    else:
        # IPv4 从网络下载
        print("正在下载 Cloudflare IPv4 列表...")
        
        if not download_file(CLOUDFLARE_IP_URL, CLOUDFLARE_IP_FILE):
            print("下载 Cloudflare IP 列表失败")
            return False
        
        # 检查文件是否为空
        if os.path.getsize(CLOUDFLARE_IP_FILE) == 0:
            print("Cloudflare IP 列表文件为空")
            return False
        
        print(f"Cloudflare IP 列表已保存到: {CLOUDFLARE_IP_FILE}")
        return True


def load_local_airport_codes():
    """从本地文件加载机场码（如果存在）"""
    if os.path.exists(AIRPORT_CODES_FILE):
        try:
            with open(AIRPORT_CODES_FILE, 'r', encoding='utf-8') as f:
                custom_codes = json.load(f)
                AIRPORT_CODES.update(custom_codes)
                print(f"✓ 已加载本地机场码配置（{len(custom_codes)} 个）")
        except Exception as e:
            print(f"加载本地机场码失败: {e}")


def save_airport_codes():
    """保存机场码到本地文件"""
    try:
        with open(AIRPORT_CODES_FILE, 'w', encoding='utf-8') as f:
            json.dump(AIRPORT_CODES, f, ensure_ascii=False, indent=2)
        print(f"✓ 机场码已保存到: {AIRPORT_CODES_FILE}")
    except Exception as e:
        print(f"保存机场码失败: {e}")


def display_airport_codes(region_filter=None):
    """显示所有支持的机场码，可按地区筛选"""
    # 按地区分组
    regions = {}
    for code, info in AIRPORT_CODES.items():
        region = info.get('region', '其他')
        if region not in regions:
            regions[region] = []
        regions[region].append((code, info))
    
    # 显示统计信息
    print(f"\n支持的机场码列表（共 {len(AIRPORT_CODES)} 个数据中心）")
    print("=" * 70)
    
    # 如果指定了地区筛选
    if region_filter:
        region_filter = region_filter.strip()
        if region_filter in regions:
            print(f"\n【{region_filter}地区】")
            print("-" * 70)
            for code, info in sorted(regions[region_filter], key=lambda x: x[0]):
                country = info.get('country', '')
                print(f"  {code:5s} - {info['name']:20s} ({country})")
        else:
            print(f"未找到地区: {region_filter}")
            print(f"可用地区: {', '.join(sorted(regions.keys()))}")
        return
    
    # 显示所有地区
    region_order = ["亚太", "北美", "欧洲", "中东", "南美", "非洲", "其他"]
    for region in region_order:
        if region in regions:
            print(f"\n【{region}地区】（{len(regions[region])} 个）")
            print("-" * 70)
            for code, info in sorted(regions[region], key=lambda x: x[0]):
                country = info.get('country', '')
                print(f"  {code:5s} - {info['name']:20s} ({country})")
    
    print("=" * 70)


def display_popular_codes():
    """显示热门机场码"""
    popular = {
        "HKG": "香港", "SIN": "新加坡", "NRT": "东京成田", "ICN": "首尔", 
        "LAX": "洛杉矶", "SJC": "圣何塞", "LHR": "伦敦", "FRA": "法兰克福"
    }
    
    print("\n热门机场码:")
    print("-" * 50)
    for code, name in popular.items():
        if code in AIRPORT_CODES:
            info = AIRPORT_CODES[code]
            region = info.get('region', '')
            print(f"  {code:5s} - {name:15s} [{region}]")
    print("-" * 50)


def find_airport_by_name(query):
    """根据城市名称查找机场码（支持模糊匹配）"""
    query = query.strip()
    if not query:
        return None
    
    # 先尝试精确匹配机场码
    query_upper = query.upper()
    if query_upper in AIRPORT_CODES:
        return query_upper
    
    # 构建城市名称到机场码的映射
    results = []
    
    for code, info in AIRPORT_CODES.items():
        name = info.get('name', '').lower()
        country = info.get('country', '').lower()
        query_lower = query.lower()
        
        # 精确匹配城市名称
        if name == query_lower:
            return code
        
        # 模糊匹配（包含关系）
        if query_lower in name or name in query_lower:
            results.append((code, info, 1))  # 优先级1
        elif query_lower in country:
            results.append((code, info, 2))  # 优先级2
    
    # 如果有匹配结果
    if results:
        # 按优先级排序
        results.sort(key=lambda x: x[2])
        
        # 如果只有一个结果，直接返回
        if len(results) == 1:
            return results[0][0]
        
        # 如果有多个结果，显示让用户选择
        print(f"\n找到 {len(results)} 个匹配的城市:")
        print("-" * 60)
        for idx, (code, info, _) in enumerate(results[:10], 1):  # 最多显示10个
            region = info.get('region', '')
            country = info.get('country', '')
            print(f"  {idx}. {code:5s} - {info['name']:20s} ({country}) [{region}]")
        print("-" * 60)
        
        try:
            choice = input(f"\n请选择 [1-{min(len(results), 10)}] 或按回车取消: ").strip()
            if choice:
                idx = int(choice) - 1
                if 0 <= idx < min(len(results), 10):
                    return results[idx][0]
        except (ValueError, IndexError):
            pass
    
    return None


def display_preset_configs():
    """显示预设配置"""
    print("\n" + "=" * 60)
    print(" 预设配置选项")
    print("=" * 60)
    print("  1. 快速测试 (10个IP, 1MB/s, 1000ms)")
    print("  2. 标准测试 (20个IP, 2MB/s, 500ms)")
    print("  3. 高质量测试 (50个IP, 5MB/s, 200ms)")
    print("  4. 自定义配置")
    print("=" * 60)


def get_user_input(ip_file=CLOUDFLARE_IP_FILE, ip_version="ipv4"):
    """获取用户输入参数
    
    Args:
        ip_file: 要使用的IP文件路径
        ip_version: IP版本（"ipv4" 或 "ipv6"）
    """
    # 询问功能选择
    print("\n" + "=" * 60)
    print(" 功能选择")
    print("=" * 60)
    print("  1. 小白快速测试 - 简单输入，适合新手")
    print("  2. 常规测速 - 测试指定机场码的IP速度")
    print("  3. 优选反代 - 从CSV文件生成反代IP列表")
    print("=" * 60)
    
    choice = input("\n请选择功能 [默认: 1]: ").strip()
    if not choice:
        choice = "1"
    
    if choice == "1":
        # 小白快速测试模式
        return handle_beginner_mode(ip_file, ip_version)
    elif choice == "3":
        # 优选反代模式
        return handle_proxy_mode()
    else:
        # 常规测速模式
        return handle_normal_mode(ip_file, ip_version)


def select_csv_file():
    """选择CSV文件"""
    while True:
        csv_file = input("\n请输入CSV文件路径 [默认: result.csv]: ").strip()
        if not csv_file:
            csv_file = "result.csv"
        
        if os.path.exists(csv_file):
            print(f"找到文件: {csv_file}")
            return csv_file
        else:
            print(f"文件不存在: {csv_file}")
            print("请确保文件路径正确，或先运行常规测速生成result.csv")
            retry = input("是否重新输入？[Y/n]: ").strip().lower()
            if retry in ['n', 'no']:
                return None






def handle_proxy_mode():
    """处理优选反代模式"""
    print("\n" + "=" * 70)
    print(" 优选反代模式")
    print("=" * 70)
    print(" 此功能将从CSV文件中提取IP和端口信息，生成反代IP列表")
    print(" CSV文件格式要求：")
    print("   - 包含 'IP 地址' 和 '端口' 列")
    print("   - 或包含 'ip' 和 'port' 列")
    print("   - 支持逗号分隔的CSV格式")
    print("=" * 70)
    
    # 选择CSV文件
    csv_file = select_csv_file()
    
    if not csv_file:
        print("未选择有效文件，退出优选反代模式")
        return None, None, None, None
    
    # 生成反代IP列表
    print(f"\n正在处理CSV文件: {csv_file}")
    success = generate_proxy_list(csv_file, "ips_ports.txt")
    
    if success:
        print("\n" + "=" * 60)
        print(" 优选反代功能完成！")
        print("=" * 60)
        print(" 生成的文件:")
        print("   - ips_ports.txt (反代IP列表)")
        print("   - 格式: IP:端口 (每行一个)")
        print("\n 使用说明:")
        print("   - 可直接用于反代配置")
        print("   - 支持各种代理软件")
        print("   - 建议定期更新IP列表")
        print("=" * 60)
        
        # 询问是否进行测速
        print("\n" + "=" * 50)
        test_choice = input("是否对反代IP列表进行测速？[Y/n]: ").strip().lower()
        
        if test_choice in ['n', 'no']:
            print("跳过测速，优选反代功能完成")
            return None, None, None, None

        print("开始对反代IP列表进行测速...")
        print("注意: 反代模式直接对IP列表测速，不需要选择机场码")
        
        # 显示预设配置选项
        display_preset_configs()
        
        # 获取配置选择
        while True:
            config_choice = input("\n请选择配置 [默认: 1]: ").strip()
            if not config_choice:
                config_choice = "1"
            
            if config_choice == "1":
                # 快速测试
                dn_count = "10"
                speed_limit = "1"
                time_limit = "1000"
                print("✓ 已选择: 快速测试 (10个IP, 1MB/s, 1000ms)")
                break
            elif config_choice == "2":
                # 标准测试
                dn_count = "20"
                speed_limit = "2"
                time_limit = "500"
                print("✓ 已选择: 标准测试 (20个IP, 2MB/s, 500ms)")
                break
            elif config_choice == "3":
                # 高质量测试
                dn_count = "50"
                speed_limit = "5"
                time_limit = "200"
                print("✓ 已选择: 高质量测试 (50个IP, 5MB/s, 200ms)")
                break
            elif config_choice == "4":
                # 自定义配置
                print("\n自定义配置:")
                
                # 获取测试IP数量
                while True:
                    dn_count = input("请输入要测试的 IP 数量 [默认: 10]: ").strip()
                    if not dn_count:
                        dn_count = "10"
                    
                    try:
                        dn_count_int = int(dn_count)
                        if dn_count_int <= 0:
                            print("✗ 请输入大于0的数字")
                            continue
                        if dn_count_int > 200:
                            confirm = input(f"  警告: 测试 {dn_count_int} 个IP可能需要较长时间，是否继续？[y/N]: ").strip().lower()
                            if confirm != 'y':
                                continue
                        dn_count = str(dn_count_int)
                        break
                    except ValueError:
                        print("✗ 请输入有效的数字")
                
                # 获取下载速度下限
                while True:
                    speed_limit = input("请输入下载速度下限 (MB/s) [默认: 1]: ").strip()
                    if not speed_limit:
                        speed_limit = "1"
                    
                    try:
                        speed_limit_float = float(speed_limit)
                        if speed_limit_float < 0:
                            print("✗ 请输入大于等于0的数字")
                            continue
                        if speed_limit_float > 100:
                            print("警告: 速度阈值过高，可能找不到符合条件的IP")
                            confirm = input("  是否继续？[y/N]: ").strip().lower()
                            if confirm != 'y':
                                continue
                        speed_limit = str(speed_limit_float)
                        break
                    except ValueError:
                        print("✗ 请输入有效的数字")
                
                # 获取延迟阈值
                while True:
                    time_limit = input("请输入延迟阈值 (ms) [默认: 1000]: ").strip()
                    if not time_limit:
                        time_limit = "1000"
                    
                    try:
                        time_limit_int = int(time_limit)
                        if time_limit_int <= 0:
                            print("✗ 请输入大于0的数字")
                            continue
                        if time_limit_int > 5000:
                            print("警告: 延迟阈值过高，可能影响使用体验")
                            confirm = input("  是否继续？[y/N]: ").strip().lower()
                            if confirm != 'y':
                                continue
                        time_limit = str(time_limit_int)
                        break
                    except ValueError:
                        print("✗ 请输入有效的数字")
                
                print(f"✓ 自定义配置: {dn_count}个IP, {speed_limit}MB/s, {time_limit}ms")
                break
            else:
                print("✗ 无效选择，请输入 1-4")
        
        # 获取延迟测速线程数
        print(f"\n⚡ 设置延迟测速线程数")
        print("说明：线程数越多延迟测速越快，性能弱的设备(如路由器)请勿太高")
        while True:
            thread_count = input("请输入延迟测速线程数 [默认: 200, 最多: 1000]: ").strip()
            if not thread_count:
                thread_count = "200"
            try:
                thread_count_int = int(thread_count)
                if thread_count_int <= 0:
                    print("✗ 请输入大于0的数字")
                    continue
                if thread_count_int > 1000:
                    print("✗ 线程数不能超过1000")
                    continue
                thread_count = str(thread_count_int)
                break
            except ValueError:
                print("✗ 请输入有效的数字")
        
        print(f"\n测速参数: 测试{dn_count}个IP, 速度下限{speed_limit}MB/s, 延迟上限{time_limit}ms, 线程数={thread_count}")
        print("模式: 反代IP列表测速")
        
        # 运行测速
        result_code = run_speedtest_with_file("ips_ports.txt", dn_count, speed_limit, time_limit, thread_count)
        
        # 如果测速成功，询问是否上报结果
        if result_code == 0 and os.path.exists("result.csv"):
            upload_results_to_api("result.csv")
        
        return None, None, None, None
    else:
        print("\n优选反代功能失败")
        return None, None, None, None


def handle_beginner_mode(ip_file=CLOUDFLARE_IP_FILE, ip_version="ipv4"):
    """处理小白快速测试模式
    
    Args:
        ip_file: 要使用的IP文件路径
        ip_version: IP版本（"ipv4" 或 "ipv6"）
    """
    print("\n" + "=" * 70)
    print(" 小白快速测试模式")
    print("=" * 70)
    print(" 此功能专为新手设计，只需要输入3个简单的数字即可开始测试")
    print(" 无需了解复杂的参数设置，程序会引导您完成所有配置")
    print("=" * 70)
    
    # 获取测试IP数量
    print("\n📊 第一步：设置测试IP数量")
    print("说明：测试的IP数量越多，结果越准确，但耗时越长")
    while True:
        dn_count = input("请输入要测试的IP数量 [默认: 10]: ").strip()
        if not dn_count:
            dn_count = "10"
        try:
            dn_count_int = int(dn_count)
            if dn_count_int <= 0:
                print("✗ 请输入大于0的数字")
                continue
            if dn_count_int > 100:
                print("⚠️  测试数量较多，可能需要较长时间")
                confirm = input("  是否继续？[y/N]: ").strip().lower()
                if confirm != 'y':
                    continue
            dn_count = str(dn_count_int)
            break
        except ValueError:
            print("✗ 请输入有效的数字")
    
    # 获取延迟阈值
    print(f"\n⏱️  第二步：设置延迟上限")
    print("说明：延迟越低，网络响应越快。一般建议100-1000ms")
    while True:
        time_limit = input("请输入延迟上限(ms) [默认: 1000]: ").strip()
        if not time_limit:
            time_limit = "1000"
        try:
            time_limit_int = int(time_limit)
            if time_limit_int <= 0:
                print("✗ 请输入大于0的数字")
                continue
            if time_limit_int > 5000:
                print("⚠️  延迟阈值过高，可能影响使用体验")
                confirm = input("  是否继续？[y/N]: ").strip().lower()
                if confirm != 'y':
                    continue
            time_limit = str(time_limit_int)
            break
        except ValueError:
            print("✗ 请输入有效的数字")
    
    # 获取下载速度下限
    print(f"\n🚀 第三步：设置下载速度下限")
    print("说明：速度越高，网络越快。一般建议1-10MB/s")
    while True:
        speed_limit = input("请输入下载速度下限(MB/s) [默认: 1]: ").strip()
        if not speed_limit:
            speed_limit = "1"
        try:
            speed_limit_float = float(speed_limit)
            if speed_limit_float < 0:
                print("✗ 请输入大于等于0的数字")
                continue
            if speed_limit_float > 50:
                print("⚠️  速度阈值过高，可能找不到符合条件的IP")
                confirm = input("  是否继续？[y/N]: ").strip().lower()
                if confirm != 'y':
                    continue
            speed_limit = str(speed_limit_float)
            break
        except ValueError:
            print("✗ 请输入有效的数字")
    
    # 获取延迟测速线程数
    print(f"\n⚡ 第四步：设置延迟测速线程数")
    print("说明：线程数越多延迟测速越快，性能弱的设备(如路由器)请勿太高")
    while True:
        thread_count = input("请输入延迟测速线程数 [默认: 200, 最多: 1000]: ").strip()
        if not thread_count:
            thread_count = "200"
        try:
            thread_count_int = int(thread_count)
            if thread_count_int <= 0:
                print("✗ 请输入大于0的数字")
                continue
            if thread_count_int > 1000:
                print("✗ 线程数不能超过1000")
                continue
            thread_count = str(thread_count_int)
            break
        except ValueError:
            print("✗ 请输入有效的数字")
    
    print(f"\n✅ 配置完成！")
    print(f"📋 测试参数:")
    print(f"   - 测试IP数量: {dn_count} 个")
    print(f"   - 延迟上限: {time_limit} ms")
    print(f"   - 速度下限: {speed_limit} MB/s")
    print(f"   - 延迟测速线程数: {thread_count}")
    print("=" * 50)
    
    print(f"\n🎯 开始测速...")
    print(f"参数: 测试{dn_count}个IP, 速度下限{speed_limit}MB/s, 延迟上限{time_limit}ms")
    print("模式: 小白快速测试（全自动，无需选择地区）")
    
    # 直接使用 Cloudflare IP 列表进行测速
    print(f"\n正在使用 Cloudflare IP 列表进行测速...")
    
    # 获取系统信息和可执行文件
    os_type, arch_type = get_system_info()
    exec_name = download_cloudflare_speedtest(os_type, arch_type)
    
    # 构建测速命令
    if sys.platform == "win32":
        cmd = [exec_name]
    else:
        cmd = [f"./{exec_name}"]
    
    cmd.extend([
        "-f", ip_file,
        "-n", thread_count,
        "-dn", dn_count,
        "-sl", speed_limit,
        "-tl", time_limit,
        "-url", DEFAULT_SPEEDTEST_URL,
        "-o", "result.csv"
    ])
    
    print(f"\n运行命令: {' '.join(cmd)}")
    print("=" * 50)
    
    # 运行测速
    result = subprocess.run(cmd, encoding='utf-8', errors='replace')
    
    if result.returncode == 0:
        print("\n✅ 测速完成！结果已保存到 result.csv")
        print("📊 您可以查看 result.csv 文件来了解详细的测试结果")
        print("💡 提示：结果文件中的IP按速度从快到慢排序")
        
        # 询问是否上报结果
        upload_info = upload_results_to_api("result.csv")
        
        # 输出对应的命令行命令
        print("\n" + "=" * 80)
        print(" 💡 快速复用命令")
        print("=" * 80)
        cli_cmd = generate_cli_command("beginner", ip_version, None, dn_count, speed_limit, time_limit, upload_info, thread_count)
        # 保存命令供定时任务使用
        global LAST_GENERATED_COMMAND
        LAST_GENERATED_COMMAND = cli_cmd
        print("本次交互对应的命令行命令：")
        print("-" * 80)
        print(cli_cmd)
        print("-" * 80)
        print("💡 提示：您可以复制上面的命令，下次直接使用命令行模式运行")
        print("=" * 80)
    else:
        print("\n❌ 测速失败")
    
    return "ALL", dn_count, speed_limit, time_limit, thread_count


def handle_normal_mode(ip_file=CLOUDFLARE_IP_FILE, ip_version="ipv4"):
    """处理常规测速模式
    
    Args:
        ip_file: 要使用的IP文件路径
        ip_version: IP版本（"ipv4" 或 "ipv6"）
    """
    print("\n开始检测可用地区...")
    print("正在使用HTTPing模式检测各地区可用性...")
    
    # 先运行一次HTTPing检测，获取可用地区
    available_regions = detect_available_regions()
    
    if not available_regions:
        print("❌ 未检测到可用地区，请检查网络连接")
        return None
    
    print(f"\n检测到 {len(available_regions)} 个可用地区:")
    for i, (region_code, region_name, count) in enumerate(available_regions, 1):
        print(f"  {i}. {region_code} - {region_name} (可用{count}个IP)")
    
    # 让用户选择地区
    while True:
        try:
            choice = int(input(f"\n请选择地区 [1-{len(available_regions)}]: ").strip())
            if 1 <= choice <= len(available_regions):
                selected_region = available_regions[choice - 1]
                cfcolo = selected_region[0]
                region_name = selected_region[1]
                count = selected_region[2]
                print(f"✓ 已选择: {region_name} ({cfcolo}) - 可用{count}个IP")
                break
            else:
                print(f"✗ 请输入 1-{len(available_regions)} 之间的数字")
        except ValueError:
            print("✗ 请输入有效的数字")
    
    # 显示预设配置选项
    display_preset_configs()
    
    # 获取配置选择
    while True:
        config_choice = input("\n请选择配置 [1-4]: ").strip()
        if config_choice == "1":
            dn_count = "10"
            speed_limit = "1"
            time_limit = "1000"
            print("✓ 快速测试: 10个IP, 1MB/s, 1000ms")
            break
        elif config_choice == "2":
            dn_count = "20"
            speed_limit = "5"
            time_limit = "500"
            print("✓ 标准测试: 20个IP, 5MB/s, 500ms")
            break
        elif config_choice == "3":
            dn_count = "50"
            speed_limit = "10"
            time_limit = "200"
            print("✓ 高质量测试: 50个IP, 10MB/s, 200ms")
            break
        elif config_choice == "4":
            # 自定义配置
            while True:
                try:
                    dn_count = input("请输入测试IP数量 [默认: 10]: ").strip()
                    if not dn_count:
                        dn_count = "10"
                    dn_count_int = int(dn_count)
                    if dn_count_int <= 0:
                        print("✗ 请输入大于0的数字")
                        continue
                    if dn_count_int > 1000:
                        print("警告: 测试数量过多，可能需要很长时间")
                        confirm = input("  是否继续？[y/N]: ").strip().lower()
                        if confirm != 'y':
                            continue
                    break
                except ValueError:
                    print("✗ 请输入有效的数字")
            
            # 获取下载速度下限
            while True:
                speed_limit = input("请输入下载速度下限 (MB/s) [默认: 1]: ").strip()
                if not speed_limit:
                    speed_limit = "1"
                
                try:
                    speed_limit_float = float(speed_limit)
                    if speed_limit_float < 0:
                        print("✗ 请输入大于等于0的数字")
                        continue
                    if speed_limit_float > 100:
                        print("警告: 速度阈值过高，可能找不到符合条件的IP")
                        confirm = input("  是否继续？[y/N]: ").strip().lower()
                        if confirm != 'y':
                            continue
                    break
                except ValueError:
                    print("✗ 请输入有效的数字")
            
            # 获取延迟阈值
            while True:
                time_limit = input("请输入延迟阈值 (ms) [默认: 1000]: ").strip()
                if not time_limit:
                    time_limit = "1000"
                
                try:
                    time_limit_int = int(time_limit)
                    if time_limit_int <= 0:
                        print("✗ 请输入大于0的数字")
                        continue
                    if time_limit_int > 5000:
                        print("警告: 延迟阈值过高，可能影响使用体验")
                        confirm = input("  是否继续？[y/N]: ").strip().lower()
                        if confirm != 'y':
                            continue
                    break
                except ValueError:
                    print("✗ 请输入有效的数字")
            
            print(f"✓ 自定义配置: {dn_count}个IP, {speed_limit}MB/s, {time_limit}ms")
            break
        else:
            print("✗ 无效选择，请输入 1-4")
    
    # 获取延迟测速线程数
    print(f"\n⚡ 设置延迟测速线程数")
    print("说明：线程数越多延迟测速越快，性能弱的设备(如路由器)请勿太高")
    while True:
        thread_count = input("请输入延迟测速线程数 [默认: 200, 最多: 1000]: ").strip()
        if not thread_count:
            thread_count = "200"
        try:
            thread_count_int = int(thread_count)
            if thread_count_int <= 0:
                print("✗ 请输入大于0的数字")
                continue
            if thread_count_int > 1000:
                print("✗ 线程数不能超过1000")
                continue
            thread_count = str(thread_count_int)
            break
        except ValueError:
            print("✗ 请输入有效的数字")
    
    print(f"\n测速参数: 地区={cfcolo}, 测试{dn_count}个IP, 速度下限{speed_limit}MB/s, 延迟上限{time_limit}ms, 线程数={thread_count}")
    print("模式: 常规测速（指定地区）")
    
    # 从地区扫描结果中提取该地区的IP进行测速
    if os.path.exists("region_scan.csv"):
        print(f"\n正在从扫描结果中提取 {cfcolo} 地区的IP...")
        
        # 读取该地区的IP
        region_ips = []
        with open("region_scan.csv", 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                colo = (row.get('地区码') or '').strip()
                if colo == cfcolo:
                    ip = (row.get('IP 地址') or '').strip()
                    if ip:
                        region_ips.append(ip)
        
        if region_ips:
            # 创建该地区的IP文件
            region_ip_file = f"{cfcolo.lower()}_ips.txt"
            with open(region_ip_file, 'w', encoding='utf-8') as f:
                for ip in region_ips:
                    f.write(f"{ip}\n")
            
            print(f"找到 {len(region_ips)} 个 {cfcolo} 地区的IP，开始测速...")
            
            # 使用该地区的IP文件进行测速
            os_type, arch_type = get_system_info()
            exec_name = download_cloudflare_speedtest(os_type, arch_type)
            
            # 构建测速命令
            if sys.platform == "win32":
                cmd = [exec_name]
            else:
                cmd = [f"./{exec_name}"]
            
            cmd.extend([
                "-f", region_ip_file,
                "-n", thread_count,
                "-dn", dn_count,
                "-sl", speed_limit,
                "-tl", time_limit,
                "-url", DEFAULT_SPEEDTEST_URL,
                "-o", "result.csv"
            ])
            
            print(f"\n运行命令: {' '.join(cmd)}")
            print("=" * 50)
            
            # 运行测速
            result = subprocess.run(cmd, encoding='utf-8', errors='replace')
            
            # 清理临时文件
            if os.path.exists(region_ip_file):
                os.remove(region_ip_file)
            
            if result.returncode == 0:
                print("\n✅ 测速完成！结果已保存到 result.csv")
                
                # 询问是否上报结果
                upload_info = upload_results_to_api("result.csv")
                
                # 输出对应的命令行命令
                print("\n" + "=" * 80)
                print(" 💡 快速复用命令")
                print("=" * 80)
                cli_cmd = generate_cli_command("normal", ip_version, cfcolo, dn_count, speed_limit, time_limit, upload_info, thread_count)
                # 保存命令供定时任务使用
                global LAST_GENERATED_COMMAND
                LAST_GENERATED_COMMAND = cli_cmd
                print("本次交互对应的命令行命令：")
                print("-" * 80)
                print(cli_cmd)
                print("-" * 80)
                print("💡 提示：您可以复制上面的命令，下次直接使用命令行模式运行")
                print("=" * 80)
            else:
                print("\n❌ 测速失败")
        else:
            print(f"❌ 未找到 {cfcolo} 地区的IP")
    else:
        print("❌ 未找到地区扫描结果文件")
    
    return cfcolo, dn_count, speed_limit, time_limit


def generate_proxy_list(result_file="result.csv", output_file="ips_ports.txt"):
    """从测速结果生成反代IP列表"""
    if not os.path.exists(result_file):
        print(f"未找到测速结果文件: {result_file}")
        return False
    
    try:
        import csv
        
        print(f"\n正在生成反代IP列表...")
        
        # 读取CSV文件
        with open(result_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        
        if not rows:
            print("测速结果文件为空")
            return False
        
        # 生成反代IP列表
        proxy_ips = []
        for row in rows:
            # 查找IP和端口列
            ip = None
            port = None
            
            # 查找IP列
            for key in row.keys():
                if 'ip' in key.lower() and '地址' in key and row[key] is not None:
                    ip = str(row[key]).strip()
                    break
                elif key.lower() == 'ip' and row[key] is not None:
                    ip = str(row[key]).strip()
                    break
            
            # 查找端口列
            for key in row.keys():
                if '端口' in key and row[key] is not None:
                    port = str(row[key]).strip()
                    break
                elif key.lower() == 'port' and row[key] is not None:
                    port = str(row[key]).strip()
                    break
            
            # 如果IP地址中包含端口信息（如 1.2.3.4:443），提取端口
            if ip and ':' in ip:
                ip_parts = ip.split(':')
                if len(ip_parts) == 2:
                    ip = ip_parts[0]  # 提取纯IP地址
                    if not port:  # 如果还没有找到端口，使用IP中的端口
                        port = ip_parts[1]
            
            # 如果没有找到端口，使用默认值
            if not port:
                port = '443'
            
            if ip and port:
                proxy_ips.append(f"{ip}:{port}")
        
        # 保存到文件
        with open(output_file, 'w', encoding='utf-8') as f:
            for proxy in proxy_ips:
                f.write(proxy + '\n')
        
        print(f"反代IP列表已生成: {output_file}")
        print(f"共生成 {len(proxy_ips)} 个反代IP")
        print(f"📝 格式: IP:端口 (如: 1.2.3.4:443)")
        
        # 显示前10个IP作为示例
        if proxy_ips:
            print(f"\n前10个反代IP示例:")
            for i, proxy in enumerate(proxy_ips[:10], 1):
                print(f"  {i:2d}. {proxy}")
            if len(proxy_ips) > 10:
                print(f"  ... 还有 {len(proxy_ips) - 10} 个IP")
        
        return True
        
    except Exception as e:
        print(f"生成反代IP列表失败: {e}")
        return False


def run_speedtest_with_file(ip_file, dn_count, speed_limit, time_limit, thread_count="200"):
    """使用指定IP文件运行测速（反代模式，不需要机场码）"""
    try:
        # 获取系统信息
        os_type, arch_type = get_system_info()
        exec_name = download_cloudflare_speedtest(os_type, arch_type)
        
        # 构建命令（反代模式使用TCPing，专注于端口信息）
        cmd = [
            f"./{exec_name}",
            "-f", ip_file,
            "-n", thread_count,
            "-dn", dn_count,
            "-sl", speed_limit,
            "-tl", time_limit,
            "-url", DEFAULT_SPEEDTEST_URL,
            "-p", "20"  # 显示前20个结果
        ]
        
        print(f"\n运行命令: {' '.join(cmd)}")
        print("=" * 50)
        
        # 运行测速 - 实时显示输出
        print("正在运行测速，请稍候...")
        result = subprocess.run(cmd, text=True, encoding='utf-8', errors='replace')
        
        if result.returncode == 0:
            print("\n测速完成！")
            print("结果已保存到 result.csv")
        else:
            print(f"\n测速失败，返回码: {result.returncode}")
        
        # 等待用户按键，不自动关闭窗口
        input("\n按回车键退出...")
        return 0
        
    except Exception as e:
        print(f"运行测速失败: {e}")
        return 1


def run_speedtest(exec_name, cfcolo, dn_count, speed_limit, time_limit, thread_count="200"):
    """运行 CloudflareSpeedTest"""
    print(f"\n开始运行 CloudflareSpeedTest...")
    print(f"测试参数:")
    print(f"  - 机场码: {cfcolo} ({AIRPORT_CODES.get(cfcolo, {}).get('name', '未知')})")
    print(f"  - 测试 IP 数量: {dn_count}")
    print(f"  - 下载速度阈值: {speed_limit} MB/s")
    print(f"  - 延迟阈值: {time_limit} ms")
    print(f"  - 延迟测速线程数: {thread_count}")
    print("-" * 50)
    
    # 构建命令
    if sys.platform == "win32":
        cmd = [exec_name]
    else:
        cmd = [f"./{exec_name}"]
    
    cmd.extend([
        "-n", thread_count,
        "-dn", dn_count,
        "-sl", speed_limit,
        "-tl", time_limit,
        "-f", CLOUDFLARE_IP_FILE,
        "-url", DEFAULT_SPEEDTEST_URL
    ])
    
    try:
        result = subprocess.run(cmd, check=True)
        print("\nCloudflareSpeedTest 任务完成！")
        return result.returncode
    except subprocess.CalledProcessError as e:
        print(f"\n运行失败: {e}")
        return e.returncode
    except FileNotFoundError:
        print(f"\n找不到可执行文件: {exec_name}")
        return 1


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description='Cloudflare SpeedTest 跨平台自动化脚本',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 快速测试模式（默认参数）
  python cloudflare_speedtest.py --mode beginner
  
  # 指定测试参数
  python cloudflare_speedtest.py --mode beginner --count 20 --speed 2 --delay 500
  
  # 常规测速模式（需要先运行地区检测）
  python cloudflare_speedtest.py --mode normal --region HKG --count 10
  
  # 优选反代模式
  python cloudflare_speedtest.py --mode proxy --csv result.csv
  
  # 指定IP版本
  python cloudflare_speedtest.py --mode beginner --ipv6
  
  # 上传结果到API（清空现有IP）
  python cloudflare_speedtest.py --mode beginner --upload api --worker-domain example.com --uuid abc123 --clear
  
  # 上传结果到API（不清空，IP会累积）
  python cloudflare_speedtest.py --mode beginner --upload api --worker-domain example.com --uuid abc123
  
  # 上传结果到GitHub
  python cloudflare_speedtest.py --mode beginner --upload github --repo owner/repo --token ghp_xxx
        """
    )
    
    # 模式选择（必需参数）
    parser.add_argument('--mode', choices=['beginner', 'normal', 'proxy'], required=True,
                       help='运行模式: beginner(小白快速测试), normal(常规测速), proxy(优选反代)')
    
    # IP版本
    parser.add_argument('--ipv6', action='store_true',
                       help='使用IPv6（默认使用IPv4）')
    
    # 测试参数
    parser.add_argument('--count', type=int, default=10,
                       help='测试IP数量（默认: 10）')
    parser.add_argument('--speed', type=float, default=1.0,
                       help='下载速度下限 MB/s（默认: 1.0）')
    parser.add_argument('--delay', type=int, default=1000,
                       help='延迟上限 ms（默认: 1000）')
    parser.add_argument('--thread', type=int, default=200,
                       help='延迟测速线程数；越多延迟测速越快，性能弱的设备(如路由器)请勿太高（默认: 200, 最多: 1000）')
    parser.add_argument('--top', type=int, default=0,
                       help='候选池按 TCP 建连延迟排序后，只保留最快的 N 个进入下载测速（0=不截断，默认: 0）')
    parser.add_argument('--max-rtt', type=float, default=0,
                       help='RTT 硬门槛（毫秒）：TCP 建连耗时超过该值的 IP 直接淘汰，'
                            '用于切掉新加坡等远端边缘（0=不限制，默认: 0）')
    
    # 常规测速模式参数
    parser.add_argument('--region', type=str,
                       help='地区码（常规测速模式需要，例如: HKG, SIN）')
    
    # 优选反代模式参数
    parser.add_argument('--csv', type=str, default='result.csv',
                       help='CSV文件路径（优选反代模式，默认: result.csv）')
    
    # 上传参数
    parser.add_argument('--upload', choices=['api', 'github', 'none'], default='none',
                       help='上传方式: api(Cloudflare Workers API), github(GitHub仓库), none(不上传)')
    
    # Cloudflare Workers API参数
    parser.add_argument('--worker-domain', type=str,
                       help='Worker域名（API上传方式需要）')
    parser.add_argument('--uuid', type=str,
                       help='UUID或路径（API上传方式需要）')
    
    # GitHub参数
    parser.add_argument('--repo', type=str,
                       help='GitHub仓库路径，格式: owner/repo（GitHub上传方式需要）')
    parser.add_argument('--token', type=str,
                       help='GitHub Personal Access Token（GitHub上传方式需要）')
    parser.add_argument('--file-path', type=str, default='cloudflare_ips.txt',
                       help='GitHub文件路径（默认: cloudflare_ips.txt）')
    
    # 其他参数
    parser.add_argument('--upload-count', type=int, default=10,
                       help='上传IP数量（默认: 10）')
    parser.add_argument('--clear', action='store_true',
                       help='上传前清空现有IP（避免IP累积，推荐使用）')
    
    return parser.parse_args()


def run_with_args(args):
    """使用命令行参数运行"""
    # 候选池截断数量（--top），由 build_ws_verified_ip_list 读取
    global POOL_TOP_N, POOL_MAX_RTT
    POOL_TOP_N = max(0, getattr(args, 'top', 0) or 0)
    POOL_MAX_RTT = max(0.0, getattr(args, 'max_rtt', 0) or 0.0)
    # 设置控制台编码（Windows 兼容）
    if sys.platform == "win32":
        try:
            import codecs
            sys.stdout = codecs.getwriter('utf-8')(sys.stdout.detach())
            sys.stderr = codecs.getwriter('utf-8')(sys.stderr.detach())
        except:
            pass
    
    stage_timer("启动")
    print("=" * 80)
    print(" Cloudflare SpeedTest 跨平台自动化脚本（命令行模式）")
    print("=" * 80)
    
    # 获取系统信息
    os_type, arch_type = get_system_info()
    print(f"\n[系统信息]")
    print(f"  操作系统: {os_type}")
    print(f"  架构类型: {arch_type}")
    print(f"  Python版本: {sys.version.split()[0]}")
    
    # 加载本地机场码配置（如果存在）
    load_local_airport_codes()
    
    # 下载 CloudflareSpeedTest
    print(f"\n[程序准备]")
    exec_name = download_cloudflare_speedtest(os_type, arch_type)
    stage_timer("程序准备")
    
    # 选择 IP 版本
    if args.ipv6:
        ip_version, ip_file = "ipv6", CLOUDFLARE_IPV6_FILE
        print("✓ 已选择: IPv6")
    else:
        ip_version, ip_file = "ipv4", CLOUDFLARE_IP_FILE
        print("✓ 已选择: IPv4")
    
    # 下载或生成 Cloudflare IP 列表
    if not download_cloudflare_ips(ip_version, ip_file):
        print("❌ 准备IP列表失败")
        return 1
    stage_timer("候选池生成(WS状态+RTT+top截断)")
    
    # 根据模式运行
    if args.mode == 'beginner':
        # 小白快速测试模式
        print(f"\n[小白快速测试模式]")
        print(f"  测试IP数量: {args.count}")
        print(f"  速度下限: {args.speed} MB/s")
        print(f"  延迟上限: {args.delay} ms")
        print(f"  延迟测速线程数: {args.thread}")
        
        # 验证线程数
        if args.thread < 1 or args.thread > 1000:
            print(f"❌ 线程数必须在 1-1000 之间，当前值: {args.thread}")
            return 1
        
        # 构建测速命令
        if sys.platform == "win32":
            cmd = [exec_name]
        else:
            cmd = [f"./{exec_name}"]
        
        cmd.extend([
            "-f", ip_file,
            "-n", str(args.thread),
            "-dn", str(args.count),
            "-sl", str(args.speed),
            "-tl", str(args.delay),
            "-url", DEFAULT_SPEEDTEST_URL,
            "-o", "result.csv"
        ])
        
        print(f"\n运行命令: {' '.join(cmd)}")
        print("=" * 50)
        
        stage_timer("进入二进制测速(延迟扫描+下载测速)")
        result = subprocess.run(cmd, encoding='utf-8', errors='replace')
        stage_timer("二进制测速完成")
        
        if result.returncode == 0:
            print("\n✅ 测速完成！结果已保存到 result.csv")
            
            # 处理上传
            if args.upload == 'api':
                if not args.worker_domain or not args.uuid:
                    print("❌ API上传需要提供 --worker-domain 和 --uuid 参数")
                else:
                    # 调用命令行模式的上传函数
                    upload_to_cloudflare_api_cli("result.csv", args.worker_domain, args.uuid, args.upload_count, clear_existing=args.clear)
            elif args.upload == 'github':
                if not args.repo or not args.token:
                    print("❌ GitHub上传需要提供 --repo 和 --token 参数")
                else:
                    # 调用命令行模式的上传函数
                    upload_to_github_cli("result.csv", args.repo, args.token, args.file_path, args.upload_count)
        else:
            print("\n❌ 测速失败")
            return 1
            
    elif args.mode == 'normal':
        # 常规测速模式
        if not args.region:
            print("❌ 常规测速模式需要提供 --region 参数（例如: --region HKG）")
            return 1
        
        print(f"\n[常规测速模式]")
        print(f"  地区码: {args.region}")
        print(f"  测试IP数量: {args.count}")
        print(f"  速度下限: {args.speed} MB/s")
        print(f"  延迟上限: {args.delay} ms")
        print(f"  延迟测速线程数: {args.thread}")
        
        # 验证线程数
        if args.thread < 1 or args.thread > 1000:
            print(f"❌ 线程数必须在 1-1000 之间，当前值: {args.thread}")
            return 1
        
        # 检查是否有地区扫描结果
        if not os.path.exists("region_scan.csv"):
            print("⚠️  未找到地区扫描结果文件，建议先运行交互式模式进行地区检测")
            print("   或者使用小白快速测试模式")
            return 1
        
        # 从地区扫描结果中提取该地区的IP
        region_ips = []
        with open("region_scan.csv", 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                colo = (row.get('地区码') or '').strip()
                if colo == args.region:
                    ip = (row.get('IP 地址') or '').strip()
                    if ip:
                        region_ips.append(ip)
        
        if not region_ips:
            print(f"❌ 未找到 {args.region} 地区的IP")
            return 1
        
        # 创建该地区的IP文件
        region_ip_file = f"{args.region.lower()}_ips.txt"
        with open(region_ip_file, 'w', encoding='utf-8') as f:
            for ip in region_ips:
                f.write(f"{ip}\n")
        
        print(f"找到 {len(region_ips)} 个 {args.region} 地区的IP，开始测速...")
        
        # 构建测速命令
        if sys.platform == "win32":
            cmd = [exec_name]
        else:
            cmd = [f"./{exec_name}"]
        
        cmd.extend([
            "-f", region_ip_file,
            "-n", str(args.thread),
            "-dn", str(args.count),
            "-sl", str(args.speed),
            "-tl", str(args.delay),
            "-url", DEFAULT_SPEEDTEST_URL,
            "-o", "result.csv"
        ])
        
        print(f"\n运行命令: {' '.join(cmd)}")
        print("=" * 50)
        
        result = subprocess.run(cmd, encoding='utf-8', errors='replace')
        
        # 清理临时文件
        if os.path.exists(region_ip_file):
            os.remove(region_ip_file)
        
        if result.returncode == 0:
            print("\n✅ 测速完成！结果已保存到 result.csv")
            
            # 处理上传
            if args.upload == 'api':
                if not args.worker_domain or not args.uuid:
                    print("❌ API上传需要提供 --worker-domain 和 --uuid 参数")
                else:
                    # 调用命令行模式的上传函数
                    upload_to_cloudflare_api_cli("result.csv", args.worker_domain, args.uuid, args.upload_count, clear_existing=args.clear)
            elif args.upload == 'github':
                if not args.repo or not args.token:
                    print("❌ GitHub上传需要提供 --repo 和 --token 参数")
                else:
                    # 调用命令行模式的上传函数
                    upload_to_github_cli("result.csv", args.repo, args.token, args.file_path, args.upload_count)
        else:
            print("\n❌ 测速失败")
            return 1
            
    elif args.mode == 'proxy':
        # 优选反代模式
        print(f"\n[优选反代模式]")
        print(f"  CSV文件: {args.csv}")
        
        if not os.path.exists(args.csv):
            print(f"❌ 未找到CSV文件: {args.csv}")
            return 1
        
        # 生成反代IP列表
        success = generate_proxy_list(args.csv, "ips_ports.txt")
        if success:
            print("\n✅ 优选反代功能完成！")
            print("  生成的文件: ips_ports.txt")
        else:
            print("\n❌ 优选反代功能失败")
            return 1
    else:
        print("❌ 请指定运行模式: --mode beginner/normal/proxy")
        return 1
    
    return 0


def generate_cli_command(mode, ip_version, cfcolo=None, dn_count=None, speed_limit=None, time_limit=None, upload_info=None, thread_count="200"):
    """生成对应的命令行命令
    
    Args:
        upload_info: 上传配置信息字典，可以包含:
            - upload_method: 'api' 或 'github'
            - worker_domain: Cloudflare Workers 域名 (api方式)
            - uuid: UUID或路径 (api方式)
            - upload_count: 上传数量 (api方式)
            - clear_existing: 是否清空现有IP (api方式，布尔值)
            - github_token: GitHub Token (github方式)
            - repo_info: 仓库信息 owner/repo (github方式)
            - file_path: 文件路径 (github方式)
        thread_count: 延迟测速线程数（默认: 200）
    """
    # 获取实际的应用名（可能是封装后的可执行文件或改名的.py文件）
    import os
    script_path = os.path.abspath(sys.argv[0])  # 使用绝对路径
    app_name = os.path.basename(script_path)
    
    # 判断是否是Python脚本（.py文件）还是封装后的可执行文件
    if app_name.endswith('.py'):
        # Python脚本，使用完整路径的Python可执行文件（避免cron找不到python3）
        python_exe = get_python_executable()
        cmd_parts = [python_exe, script_path]
    else:
        # 封装后的可执行文件，使用绝对路径
        cmd_parts = [script_path]
    
    # 添加模式
    if mode == "beginner":
        cmd_parts.append("--mode beginner")
    elif mode == "normal":
        cmd_parts.append("--mode normal")
    elif mode == "proxy":
        cmd_parts.append("--mode proxy")
    
    # 添加IP版本
    if ip_version == "ipv6":
        cmd_parts.append("--ipv6")
    
    # 添加参数
    if dn_count:
        cmd_parts.append(f"--count {dn_count}")
    if speed_limit:
        cmd_parts.append(f"--speed {speed_limit}")
    if time_limit:
        cmd_parts.append(f"--delay {time_limit}")
    if thread_count:
        cmd_parts.append(f"--thread {thread_count}")
    
    # 添加地区码（常规模式）
    if mode == "normal" and cfcolo:
        cmd_parts.append(f"--region {cfcolo}")
    
    # 添加上传配置
    if upload_info:
        if upload_info.get("upload_method") == "api":
            cmd_parts.append("--upload api")
            if upload_info.get("worker_domain"):
                cmd_parts.append(f"--worker-domain {upload_info['worker_domain']}")
            if upload_info.get("uuid"):
                cmd_parts.append(f"--uuid {upload_info['uuid']}")
            if upload_info.get("upload_count"):
                cmd_parts.append(f"--upload-count {upload_info['upload_count']}")
            # 如果选择了清空选项，添加 --clear 参数
            if upload_info.get("clear_existing"):
                cmd_parts.append("--clear")
        elif upload_info.get("upload_method") == "github":
            cmd_parts.append("--upload github")
            if upload_info.get("github_token"):
                # Token较长，使用引号包裹
                cmd_parts.append(f"--token '{upload_info['github_token']}'")
            if upload_info.get("repo_info"):
                cmd_parts.append(f"--repo {upload_info['repo_info']}")
            if upload_info.get("file_path"):
                cmd_parts.append(f"--file-path {upload_info['file_path']}")
            if upload_info.get("upload_count"):
                cmd_parts.append(f"--upload-count {upload_info['upload_count']}")
    
    return " ".join(cmd_parts)


def main():
    """主函数"""
    # 检查是否有命令行参数
    if len(sys.argv) > 1:
        # 命令行模式
        args = parse_args()
        return run_with_args(args)
    
    # 检查是否是交互式环境（非交互式环境如cron、Docker容器等）
    try:
        is_interactive = sys.stdin.isatty()
    except:
        # 如果无法检测，假设是交互式环境
        is_interactive = True
    
    # 如果不是交互式环境，显示帮助信息并退出
    if not is_interactive:
        print("=" * 80)
        print(" Cloudflare SpeedTest 跨平台自动化脚本")
        print("=" * 80)
        print("检测到非交互式环境，请使用命令行参数模式运行。")
        print("")
        print("示例命令：")
        print("  python3 cloudflare_speedtest.py --mode beginner --count 10 --speed 1 --delay 1000")
        print("  python3 cloudflare_speedtest.py --mode normal --region HKG --count 10")
        print("  python3 cloudflare_speedtest.py --mode proxy --csv result.csv")
        print("")
        print("查看完整帮助：")
        print("  python3 cloudflare_speedtest.py --help")
        print("=" * 80)
        return 1
    
    # 交互式模式
    # 设置控制台编码（Windows 兼容）
    if sys.platform == "win32":
        try:
            import codecs
            sys.stdout = codecs.getwriter('utf-8')(sys.stdout.detach())
            sys.stderr = codecs.getwriter('utf-8')(sys.stderr.detach())
        except:
            pass
    
    print("=" * 80)
    print(" Cloudflare SpeedTest 跨平台自动化脚本")
    print("=" * 80)
    print(" 支持 Windows / Linux / macOS (Darwin)")
    print(f" 内置 {len(AIRPORT_CODES)} 个全球数据中心机场码")
    print(" 支持单个/多机场码/地区优选测速")
    print(" 支持优选反代IP列表生成")
    print("=" * 80)
    
    # 获取系统信息
    os_type, arch_type = get_system_info()
    print(f"\n[系统信息]")
    print(f"  操作系统: {os_type}")
    print(f"  架构类型: {arch_type}")
    print(f"  Python版本: {sys.version.split()[0]}")
    
    # 加载本地机场码配置（如果存在）
    print(f"\n[配置加载]")
    load_local_airport_codes()
    
    # 下载 CloudflareSpeedTest
    print(f"\n[程序准备]")
    exec_name = download_cloudflare_speedtest(os_type, arch_type)
    
    # 选择 IP 版本
    ip_version, ip_file = select_ip_version()
    
    # 下载或生成 Cloudflare IP 列表
    if not download_cloudflare_ips(ip_version, ip_file):
        print("❌ 准备IP列表失败")
        return 1
    
    # 获取用户输入
    print(f"\n[参数配置]")
    print("=" * 60)
    print(" GitHub https://github.com/byJoey/yx-tools")
    print(" YouTube https://www.youtube.com/@Joeyblog")
    print(" 博客 https://joeyblog.net")
    print(" Telegram交流群: https://t.me/+ft-zI76oovgwNmRh")
    print("=" * 60)
    result = get_user_input(ip_file, ip_version)
    
    # 检查是否是优选反代模式
    if result == (None, None, None, None):
        print("\n优选反代功能已完成，程序退出")
        # Windows 系统添加暂停，避免窗口立即关闭
        if sys.platform == "win32":
            print("\n" + "=" * 60)
            input("按 Enter 键退出...")
        return 0
    
    # 常规测速模式和小白快速测试模式已经在各自的函数中完成测速并输出命令
    print(f"\n测速已完成")
    
    # Linux/macOS 环境询问是否设置定时任务（使用 cron）
    if sys.platform.startswith('linux') or sys.platform == "darwin":
        setup_cron_job()
    # Windows 环境询问是否设置定时任务（使用任务计划程序）
    elif sys.platform == "win32":
        setup_windows_task()
    
    # Windows 系统添加暂停，避免窗口立即关闭
    if sys.platform == "win32":
        print("\n" + "=" * 60)
        input("按 Enter 键退出...")
    
    return 0


def is_openwrt():
    """检测是否是OpenWrt系统"""
    try:
        # 检查是否存在OpenWrt特有的文件
        if os.path.exists('/etc/openwrt_release'):
            return True
        # 检查uname输出
        result = subprocess.run(['uname', '-a'], capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode == 0 and 'openwrt' in result.stdout.lower():
            return True
    except:
        pass
    return False


def get_python_executable():
    """获取Python可执行文件的完整路径（用于cron任务）"""
    import shutil
    
    # 优先使用当前运行的Python解释器路径
    python_exe = sys.executable
    
    # 如果是相对路径或不在PATH中，尝试查找完整路径
    if not os.path.isabs(python_exe) or not os.path.exists(python_exe):
        # 尝试使用which命令查找
        try:
            if sys.platform == "win32":
                # Windows使用where命令
                result = subprocess.run(['where', 'python'], capture_output=True, text=True, timeout=5)
            else:
                # Unix系统使用which命令
                result = subprocess.run(['which', 'python3'], capture_output=True, text=True, timeout=5)
            
            if result.returncode == 0:
                found_path = result.stdout.strip().split('\n')[0]
                if found_path and os.path.exists(found_path):
                    python_exe = found_path
        except:
            pass
    
    # 如果还是找不到，尝试使用shutil.which
    if not os.path.exists(python_exe):
        try:
            if sys.platform == "win32":
                found_path = shutil.which('python')
            else:
                found_path = shutil.which('python3')
            if found_path:
                python_exe = found_path
        except:
            pass
    
    return python_exe


def get_current_command():
    """获取本次运行的完整命令（用于定时任务，使用绝对路径）"""
    import os
    
    # 获取脚本的绝对路径
    script_path = os.path.abspath(sys.argv[0])
    app_name = os.path.basename(script_path)
    
    # 如果是命令行模式，从sys.argv重新构建命令
    if len(sys.argv) > 1:
        if app_name.endswith('.py'):
            # 使用完整路径的Python可执行文件
            python_exe = get_python_executable()
            # 使用绝对路径
            cmd_parts = [python_exe, script_path] + sys.argv[1:]
        else:
            # 使用绝对路径
            cmd_parts = [script_path] + sys.argv[1:]
        return ' '.join(cmd_parts)
    
    # 交互模式下，返回None（需要从其他地方获取）
    return None


def check_existing_cron_jobs(command_pattern=None):
    """检查crontab中是否已有类似的任务"""
    try:
        # 获取当前用户的crontab
        result = subprocess.run(['crontab', '-l'], capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode != 0:
            # 没有crontab或出错
            return []
        
        existing_jobs = []
        lines = result.stdout.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            # 跳过注释和空行
            if not line or line.startswith('#'):
                continue
            
            # 检查是否包含应用名
            app_name = os.path.basename(sys.argv[0])
            if app_name in line or (command_pattern and command_pattern in line):
                existing_jobs.append(line)
        
        return existing_jobs
    except Exception as e:
        print(f"⚠️  检查crontab失败: {e}")
        return []


def setup_cron_job():
    """设置定时任务（Linux/macOS 使用 cron）"""
    print("\n" + "=" * 70)
    print(" 定时任务设置")
    print("=" * 70)
    
    # 检测系统类型
    if sys.platform == "darwin":
        system_type = "macOS"
    elif is_openwrt():
        system_type = "OpenWrt"
    else:
        system_type = "Linux"
    print(f"检测到 {system_type} 环境，可以设置定时任务（使用 cron）")
    
    # 询问是否要设置定时任务
    choice = input("\n是否要设置定时任务？[y/N]: ").strip().lower()
    if choice not in ['y', 'yes']:
        print("跳过设置定时任务")
        return
    
    # 获取本次运行的命令
    current_command = get_current_command()
    
    # 如果是交互模式，从保存的命令中获取
    if not current_command:
        global LAST_GENERATED_COMMAND
        if LAST_GENERATED_COMMAND:
            # generate_cli_command已经使用绝对路径，直接使用
            current_command = LAST_GENERATED_COMMAND
        else:
            print("⚠️  无法获取本次运行的命令，请手动设置定时任务")
            print("   您可以使用 'crontab -e' 手动编辑定时任务")
            return
    
    # 检查是否已有类似的任务
    app_name = os.path.basename(sys.argv[0])
    existing_jobs = check_existing_cron_jobs(app_name)
    
    if existing_jobs:
        print(f"\n⚠️  检测到已存在 {len(existing_jobs)} 个类似的定时任务：")
        for i, job in enumerate(existing_jobs, 1):
            print(f"  {i}. {job}")
        
        print("\n请选择操作：")
        print("  1. 清理现有任务后添加新任务")
        print("  2. 继续添加新任务（保留现有任务）")
        print("  3. 取消设置")
        
        while True:
            choice = input("\n请选择 [1/2/3]: ").strip()
            if choice == "1":
                should_clear = True
                break
            elif choice == "2":
                should_clear = False
                break
            elif choice == "3":
                print("取消设置定时任务")
                return
            else:
                print("✗ 请输入 1、2 或 3")
    else:
        should_clear = False
    
    # 获取cron时间表达式
    print("\n" + "=" * 70)
    print(" 设置定时任务时间")
    print("=" * 70)
    print("Cron时间格式: 分 时 日 月 周")
    print("示例:")
    print("  每天凌晨2点: 0 2 * * *")
    print("  每小时: 0 * * * *")
    print("  每30分钟: */30 * * * *")
    print("  每周一凌晨3点: 0 3 * * 1")
    print("  每月1号凌晨1点: 0 1 1 * *")
    print("=" * 70)
    
    while True:
        cron_time = input("\n请输入Cron时间表达式 [例如: 0 2 * * *]: ").strip()
        if not cron_time:
            print("✗ 时间表达式不能为空")
            continue
        
        # 验证cron时间格式（简单验证）
        parts = cron_time.split()
        if len(parts) != 5:
            print("✗ Cron时间格式错误，应为5个字段（分 时 日 月 周）")
            continue
        
        # 确认时间表达式
        print(f"\n您设置的时间表达式: {cron_time}")
        confirm = input("确认使用此时间？[Y/n]: ").strip().lower()
        if confirm not in ['n', 'no']:
            break
    
    # 构建cron任务（如果是Python脚本，添加PATH环境变量）
    script_path = os.path.abspath(sys.argv[0])
    app_name = os.path.basename(script_path)
    
    # 检查是否是Python脚本
    if app_name.endswith('.py'):
        # 获取当前PATH环境变量
        current_path = os.environ.get('PATH', '')
        # 获取Python可执行文件的目录
        python_exe = get_python_executable()
        python_dir = os.path.dirname(python_exe)
        
        # 构建带环境变量的cron命令
        # 设置PATH环境变量，确保能找到python3和其他命令
        if current_path:
            # 将PATH分割成列表，去除重复
            path_list = current_path.split(':')
            # 如果Python目录不在PATH中，添加到前面
            if python_dir not in path_list:
                path_list.insert(0, python_dir)
            # 去除重复的路径
            seen = set()
            unique_paths = []
            for path in path_list:
                if path and path not in seen:
                    seen.add(path)
                    unique_paths.append(path)
            env_path = ':'.join(unique_paths)
        else:
            env_path = python_dir
        
        # 构建cron命令，包含PATH环境变量设置
        cron_line = f"{cron_time} PATH={env_path} {current_command}"
    else:
        # 非Python脚本，直接使用命令
        cron_line = f"{cron_time} {current_command}"
    
    try:
        # 读取现有crontab
        result = subprocess.run(['crontab', '-l'], capture_output=True, text=True, encoding='utf-8', errors='replace')
        existing_crontab = ""
        if result.returncode == 0:
            existing_crontab = result.stdout
        
        # 如果需要清理，移除类似的任务
        if should_clear:
            lines = existing_crontab.strip().split('\n')
            filtered_lines = []
            for line in lines:
                if app_name not in line:
                    filtered_lines.append(line)
            existing_crontab = '\n'.join(filtered_lines)
            if existing_crontab and not existing_crontab.endswith('\n'):
                existing_crontab += '\n'
        
        # 添加新任务
        new_crontab = existing_crontab
        if new_crontab and not new_crontab.endswith('\n'):
            new_crontab += '\n'
        new_crontab += f"# Cloudflare SpeedTest 定时任务 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        new_crontab += f"{cron_line}\n"
        
        # 写入crontab
        process = subprocess.Popen(['crontab', '-'], stdin=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
        process.communicate(input=new_crontab)
        
        if process.returncode == 0:
            print("\n✅ 定时任务设置成功！")
            print(f"任务: {cron_line}")
            print("\n💡 提示:")
            print("  - 使用 'crontab -l' 查看所有定时任务")
            print("  - 使用 'crontab -e' 编辑定时任务")
            print("  - 使用 'crontab -r' 删除所有定时任务")
        else:
            print("❌ 设置定时任务失败")
    except Exception as e:
        print(f"❌ 设置定时任务失败: {e}")
        print("   请手动使用 'crontab -e' 编辑定时任务")


def setup_windows_task():
    """设置 Windows 定时任务（使用任务计划程序）"""
    print("\n" + "=" * 70)
    print(" 定时任务设置")
    print("=" * 70)
    print("检测到 Windows 环境，可以设置定时任务（使用任务计划程序）")
    
    # 询问是否要设置定时任务
    choice = input("\n是否要设置定时任务？[y/N]: ").strip().lower()
    if choice not in ['y', 'yes']:
        print("跳过设置定时任务")
        return
    
    # 获取本次运行的命令
    current_command = get_current_command()
    
    # 如果是交互模式，从保存的命令中获取
    if not current_command:
        global LAST_GENERATED_COMMAND
        if LAST_GENERATED_COMMAND:
            current_command = LAST_GENERATED_COMMAND
        else:
            print("⚠️  无法获取本次运行的命令，请手动设置定时任务")
            print("   您可以使用任务计划程序手动创建任务")
            return
    
    # 获取任务名称
    app_name = os.path.basename(sys.argv[0]).replace('.py', '').replace('.exe', '')
    task_name = f"CloudflareSpeedTest_{app_name}"
    
    # 检查是否已有任务
    try:
        result = subprocess.run(
            ['schtasks', '/query', '/tn', task_name],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        if result.returncode == 0:
            print(f"\n⚠️  检测到已存在任务: {task_name}")
            print("请选择操作：")
            print("  1. 删除现有任务后创建新任务")
            print("  2. 取消设置")
            
            while True:
                choice = input("\n请选择 [1/2]: ").strip()
                if choice == "1":
                    # 删除现有任务
                    subprocess.run(
                        ['schtasks', '/delete', '/tn', task_name, '/f'],
                        capture_output=True,
                        text=True,
                        encoding='utf-8',
                        errors='replace'
                    )
                    print("✓ 已删除现有任务")
                    break
                elif choice == "2":
                    print("取消设置定时任务")
                    return
                else:
                    print("✗ 请输入 1 或 2")
    except Exception:
        pass  # 任务不存在，继续创建
    
    # 获取时间设置
    print("\n" + "=" * 70)
    print(" 设置定时任务时间")
    print("=" * 70)
    print("Windows 任务计划程序支持多种触发方式：")
    print("  1. 每天指定时间（例如: 每天凌晨2点）")
    print("  2. 每小时（例如: 每小时的第0分钟）")
    print("  3. 每N分钟（例如: 每30分钟）")
    print("  4. 每周指定时间（例如: 每周一凌晨3点）")
    print("=" * 70)
    
    print("\n请选择触发方式：")
    print("  1. 每天指定时间")
    print("  2. 每小时")
    print("  3. 每N分钟")
    print("  4. 每周指定时间")
    
    schedule_type = input("\n请选择 [1-4]: ").strip()
    
    # 构建 schtasks 命令
    # 直接使用 current_command，因为它已经包含了完整的命令和参数
    # 但需要确保路径格式正确（Windows 使用反斜杠）
    if current_command:
        # current_command 已经是完整命令，直接使用
        # 但需要处理路径中的空格（用引号包裹整个命令）
        full_command = current_command
        # 如果命令中包含空格路径，需要确保正确转义
        # schtasks 的 /tr 参数会自动处理引号
    else:
        # 如果没有 current_command，构建基本命令
        script_path = os.path.abspath(sys.argv[0])
        if script_path.endswith('.py'):
            python_exe = get_python_executable()
            if ' ' in python_exe:
                python_exe = f'"{python_exe}"'
            if ' ' in script_path:
                script_path = f'"{script_path}"'
            full_command = f"{python_exe} {script_path}"
        else:
            if ' ' in script_path:
                script_path = f'"{script_path}"'
            full_command = script_path
    
    # 根据选择的类型构建 schtasks 命令
    schtasks_cmd = ['schtasks', '/create', '/tn', task_name, '/tr', full_command, '/sc']
    
    if schedule_type == "1":
        # 每天指定时间
        time_str = input("请输入时间 (HH:MM，例如: 02:00): ").strip()
        if not time_str:
            print("✗ 时间不能为空")
            return
        schtasks_cmd.extend(['daily', '/st', time_str])
        
    elif schedule_type == "2":
        # 每小时
        minute = input("请输入分钟数 (0-59，例如: 0): ").strip() or "0"
        schtasks_cmd.extend(['hourly', '/mo', '1'])
        # 注意：Windows 任务计划程序的 hourly 不支持指定分钟，需要手动计算
        print("⚠️  注意：Windows 任务计划程序的每小时触发不支持指定分钟")
        print("   将设置为每小时的第0分钟执行")
        
    elif schedule_type == "3":
        # 每N分钟
        minutes = input("请输入分钟数 (例如: 30): ").strip()
        if not minutes or not minutes.isdigit():
            print("✗ 请输入有效的数字")
            return
        schtasks_cmd.extend(['minute', '/mo', minutes])
        
    elif schedule_type == "4":
        # 每周指定时间
        day = input("请输入星期几 (1=周一, 2=周二, ..., 7=周日，例如: 1): ").strip()
        time_str = input("请输入时间 (HH:MM，例如: 03:00): ").strip()
        if not day or not time_str:
            print("✗ 星期和时间不能为空")
            return
        schtasks_cmd.extend(['weekly', '/d', day, '/st', time_str])
        
    else:
        print("✗ 无效选择")
        return
    
    # 添加其他参数
    schtasks_cmd.extend(['/f'])  # 强制创建（如果已存在则覆盖）
    
    # 确认
    print(f"\n任务名称: {task_name}")
    print(f"命令: {full_command}")
    print(f"触发方式: {schedule_type}")
    confirm = input("\n确认创建此任务？[Y/n]: ").strip().lower()
    if confirm in ['n', 'no']:
        print("取消创建任务")
        return
    
    # 执行创建任务
    try:
        result = subprocess.run(
            schtasks_cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        
        if result.returncode == 0:
            print("\n✅ 定时任务设置成功！")
            print(f"任务名称: {task_name}")
            print("\n💡 提示:")
            print("  - 使用 'schtasks /query /tn " + task_name + "' 查看任务详情")
            print("  - 使用 'schtasks /delete /tn " + task_name + " /f' 删除任务")
            print("  - 使用 'taskschd.msc' 打开任务计划程序图形界面")
        else:
            print("❌ 设置定时任务失败")
            if result.stderr:
                print(f"错误信息: {result.stderr}")
            print("\n💡 提示: 可能需要管理员权限，请以管理员身份运行")
    except Exception as e:
        print(f"❌ 设置定时任务失败: {e}")
        print("   请使用任务计划程序（taskschd.msc）手动创建任务")


def load_config():
    """从配置文件加载上次保存的配置"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                return config
        except Exception as e:
            print(f"⚠️  读取配置文件失败: {e}")
            return None
    return None


def save_config(worker_domain=None, uuid=None, github_token=None, repo_info=None, file_path=None):
    """保存配置到文件"""
    try:
        # 加载现有配置
        existing_config = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    existing_config = json.load(f)
            except:
                pass
        
        # 更新配置
        if worker_domain and uuid:
            existing_config["worker_domain"] = worker_domain
            existing_config["uuid"] = uuid
            existing_config["api_last_used"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        if github_token and repo_info:
            existing_config["github_token"] = github_token
            existing_config["repo_info"] = repo_info
            if file_path:
                existing_config["file_path"] = file_path
            existing_config["github_last_used"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 保存配置
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(existing_config, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"⚠️  保存配置失败: {e}")
        return False


def clear_config():
    """清除保存的配置"""
    try:
        if os.path.exists(CONFIG_FILE):
            os.remove(CONFIG_FILE)
            print("✅ 已清除保存的配置")
            return True
    except Exception as e:
        print(f"⚠️  清除配置失败: {e}")
        return False


def upload_results_to_api(result_file="result.csv"):
    """上报优选结果到 Cloudflare Workers API 或 GitHub
    
    Returns:
        dict: 上传配置信息，包含上传方式、相关参数等，如果未上传则返回None
    """
    print("\n" + "=" * 70)
    print(" 优选结果上报功能")
    print("=" * 70)
    print(" 此功能可以将测速结果上报到您的 Cloudflare Workers API 或 GitHub")
    print("=" * 70)
    
    # 询问是否上报
    choice = input("\n是否要上报优选结果？[y/N]: ").strip().lower()
    if choice not in ['y', 'yes']:
        print("跳过上报")
        return None
    
    # 选择上传方式
    print("\n" + "=" * 70)
    print(" 请选择上传方式")
    print("=" * 70)
    print("  1. Cloudflare Workers API")
    print("  2. GitHub (Gist)")
    print("=" * 70)
    
    while True:
        upload_method = input("\n请选择上传方式 [1/2]: ").strip()
        if upload_method == "1":
            upload_info = upload_to_cloudflare_api(result_file)
            return upload_info
        elif upload_method == "2":
            upload_info = upload_to_github(result_file)
            return upload_info
        else:
            print("✗ 请输入 1 或 2")


def upload_to_cloudflare_api(result_file="result.csv"):
    """上报优选结果到 Cloudflare Workers API"""
    print("\n" + "=" * 70)
    print(" Cloudflare Workers API 上报")
    print("=" * 70)
    print(" 需要提供您的 Worker 域名和 UUID或者路径")
    print("=" * 70)
    
    # 检查结果文件是否存在
    if not os.path.exists(result_file):
        print(f"❌ 未找到测速结果文件: {result_file}")
        print("请先完成测速后再上报结果")
        return None
    
    # 尝试加载保存的配置
    saved_config = load_config()
    worker_domain = None
    uuid = None
    
    if saved_config:
        saved_domain = saved_config.get('worker_domain', '')
        saved_uuid = saved_config.get('uuid', '')
        last_used = saved_config.get('last_used', '未知')
        
        print(f"\n💾 检测到上次使用的配置:")
        print(f"   Worker 域名: {saved_domain}")
        print(f"   UUID或者路径: {saved_uuid}")
        print(f"   上次使用: {last_used}")
        print("\n是否使用上次的配置？")
        print("  1. 是 - 使用上次配置")
        print("  2. 否 - 输入新的URL")
        print("  3. 清除配置 - 删除保存的配置")
        
        while True:
            config_choice = input("\n请选择 [1/2/3]: ").strip()
            if config_choice == "1":
                worker_domain = saved_domain
                uuid = saved_uuid
                print(f"\n✅ 使用保存的配置")
                print(f"   Worker 域名: {worker_domain}")
                print(f"   UUID或者路径: {uuid}")
                # 更新最后使用时间
                save_config(worker_domain=worker_domain, uuid=uuid)
                break
            elif config_choice == "2":
                print("\n请输入新的配置...")
                break
            elif config_choice == "3":
                clear_config()
                print("请重新输入配置...")
                break
            else:
                print("✗ 请输入 1、2 或 3")
    
    # 如果没有使用保存的配置，则获取新的URL
    if not worker_domain or not uuid:
        # 获取管理页面 URL
        print("\n📝 请输入您的 Worker 管理页面 URL")
        print("示例: https://你的域名/你的UUID或者路径")
        print("提示: 直接复制浏览器地址栏的完整URL即可")
        
        management_url = input("\n管理页面 URL: ").strip()
        if not management_url:
            print("❌ URL 不能为空")
            return None
    
        # 解析 URL，提取域名和 UUID
        try:
            from urllib.parse import urlparse
            
            # 移除可能的协议前缀和尾部斜杠
            management_url = management_url.strip().rstrip('/')
            
            # 如果没有协议前缀，添加 https://
            if not management_url.startswith(('http://', 'https://')):
                management_url = 'https://' + management_url
            
            # 解析 URL
            parsed = urlparse(management_url)
            worker_domain = parsed.netloc
            
            # 从路径中提取 UUID（不再验证格式）
            if not worker_domain:
                print("❌ 无法解析域名，请检查 URL 格式")
                return None
            
            # 从路径中提取最后一个非空部分作为UUID
            path_parts = [p for p in parsed.path.strip('/').split('/') if p]
            if not path_parts:
                print("❌ 无法从 URL 中提取 UUID或者路径")
                print("   请确保 URL 包含 UUID或者路径")
                print("   格式示例: https://域名/UUID或者路径")
                return None
            
            uuid = path_parts[-1]
            
            # 显示解析结果
            print(f"\n✅ 成功解析配置:")
            print(f"   Worker 域名: {worker_domain}")
            print(f"   UUID或者路径: {uuid}")
            
            # 询问是否保存配置
            save_choice = input("\n是否保存此配置供下次使用？[Y/n]: ").strip().lower()
            if save_choice not in ['n', 'no']:
                if save_config(worker_domain=worker_domain, uuid=uuid):
                    print("✅ 配置已保存")
                else:
                    print("⚠️  配置保存失败，但不影响本次上报")
            
        except Exception as e:
            print(f"❌ URL 解析失败: {e}")
            print("   请检查 URL 格式是否正确")
            return None
    
    # 构建 API URL
    api_url = f"https://{worker_domain}/{uuid}/api/preferred-ips"
    
    # 检查是否已有数据
    print("\n🔍 正在检查现有优选IP...")
    try:
        try:
            response = requests.get(api_url, timeout=10)
        except ImportError as e:
            # SSL模块不可用，静默切换到curl
            if "SSL module is not available" in str(e):
                response = curl_request(api_url, method='GET', timeout=10)
            else:
                raise
        
        if response.status_code == 200:
            result = response.json()
            existing_count = result.get('count', 0)
            if existing_count > 0:
                print(f"⚠️  发现已存在 {existing_count} 个优选IP")
                print("\n是否要清空现有数据后再添加新的？")
                print("  1. 是 - 清空后添加（推荐，避免重复）")
                print("  2. 否 - 直接添加（可能有重复提示）")
                
                while True:
                    clear_choice = input("\n请选择 [1/2]: ").strip()
                    if clear_choice == "1":
                        print("准备清空现有数据...")
                        should_clear = True
                        break
                    elif clear_choice == "2":
                        print("将直接添加，跳过清空")
                        should_clear = False
                        break
                    else:
                        print("✗ 请输入 1 或 2")
            else:
                should_clear = False
                print("✅ 当前无数据，将直接添加")
        else:
            should_clear = False
            print("⚠️  无法获取现有数据状态，将直接尝试添加")
    except Exception as e:
        should_clear = False
        print(f"⚠️  检查现有数据失败: {e}")
        print("将继续尝试添加...")
    
    # 读取测速结果
    print("\n📊 正在读取测速结果...")
    try:
        best_ips = []
        with open(result_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # 安全获取数据，避免NoneType错误
                ip = (row.get('IP 地址') or '').strip()
                port = (row.get('端口') or '').strip()
                
                # 尝试多种可能的列名来获取速度
                speed = ''
                for speed_key in ['下载速度(MB/s)', '下载速度 (MB/s)', '下载速度']:
                    if speed_key in row and row[speed_key] is not None:
                        speed = str(row[speed_key]).strip()
                        break
                
                # 尝试多种可能的列名来获取延迟
                latency = ''
                for latency_key in ['平均延迟', '延迟', 'latency']:
                    if latency_key in row and row[latency_key] is not None:
                        latency = str(row[latency_key]).strip()
                        break
                
                # 获取地区码
                region_code = (row.get('地区码') or '').strip()
                
                # 如果IP地址中包含端口信息
                if ip and ':' in ip:
                    ip_parts = ip.split(':')
                    if len(ip_parts) == 2:
                        ip = ip_parts[0]
                        if not port:
                            port = ip_parts[1]
                
                # 设置默认端口
                if not port:
                    port = '443'
                
                if ip:
                    try:
                        speed_val = float(speed) if speed else 0
                        latency_val = latency if latency else 'N/A'
                        
                        # 获取地区中文名称
                        region_name = '未知地区'
                        if region_code and region_code in AIRPORT_CODES:
                            region_name = AIRPORT_CODES[region_code].get('name', region_code)
                        elif region_code:
                            region_name = region_code
                        
                        best_ips.append({
                            'ip': ip,
                            'port': int(port),
                            'speed': speed_val,
                            'latency': latency_val,
                            'region_code': region_code,
                            'region_name': region_name
                        })
                    except ValueError:
                        continue
        
        if not best_ips:
            print("❌ 未找到有效的测速结果")
            return
        
        print(f"✅ 找到 {len(best_ips)} 个测速结果")
        
        # 询问要上报多少个结果
        while True:
            count_input = input(f"\n请输入要上报的IP数量 [默认: 10, 最多: {len(best_ips)}]: ").strip()
            if not count_input:
                upload_count = min(10, len(best_ips))
                break
            try:
                upload_count = int(count_input)
                if upload_count <= 0:
                    print("✗ 请输入大于0的数字")
                    continue
                if upload_count > len(best_ips):
                    print(f"⚠️  最多只能上报 {len(best_ips)} 个结果")
                    upload_count = len(best_ips)
                break
            except ValueError:
                print("✗ 请输入有效的数字")
        
        # 显示将要上报的IP
        print(f"\n将上报以下 {upload_count} 个优选IP:")
        print("-" * 70)
        for i, ip_info in enumerate(best_ips[:upload_count], 1):
            region_display = f"{ip_info['region_name']}" if ip_info.get('region_name') else '未知地区'
            print(f"  {i:2d}. {ip_info['ip']:15s}:{ip_info['port']:<5d} - {ip_info['speed']:.2f} MB/s - {region_display} - 延迟: {ip_info['latency']}")
        print("-" * 70)
        
        # 确认上报
        confirm = input("\n确认上报以上IP？[Y/n]: ").strip().lower()
        if confirm in ['n', 'no']:
            print("取消上报")
            return None
        
        # 如果需要清空，先执行清空操作
        if should_clear:
            print("\n🗑️  正在清空现有数据...")
            try:
                try:
                    delete_response = requests.delete(
                        api_url,
                        json={"all": True},
                        headers={"Content-Type": "application/json"},
                        timeout=10
                    )
                except ImportError as e:
                    # SSL模块不可用，静默切换到curl
                    if "SSL module is not available" in str(e):
                        delete_response = curl_request(
                            api_url,
                            method='DELETE',
                            data={"all": True},
                            headers={"Content-Type": "application/json"},
                            timeout=10
                        )
                    else:
                        raise
                
                if delete_response.status_code == 200:
                    print("✅ 现有数据已清空")
                else:
                    print(f"⚠️  清空失败 (HTTP {delete_response.status_code})，继续尝试添加...")
            except Exception as e:
                print(f"⚠️  清空操作失败: {e}，继续尝试添加...")
        
        # 构建批量上报数据
        print("\n🚀 开始批量上报优选IP...")
        batch_data = []
        for ip_info in best_ips[:upload_count]:
            # 构建节点名称：地区名-速度MB/s
            region_name = ip_info.get('region_name', '未知地区')
            speed = ip_info['speed']
            name = build_node_name(region_name, speed)
            
            batch_data.append({
                "ip": ip_info['ip'],
                "port": ip_info['port'],
                "name": name
            })
        
        # 发送批量POST请求
        use_curl_fallback = False
        response = None
        success_count = 0
        fail_count = 0
        skipped_count = 0
        
        try:
            try:
                response = requests.post(
                    api_url,
                    json=batch_data,
                    headers={"Content-Type": "application/json"},
                    timeout=30
                )
            except ImportError as e:
                # SSL模块不可用，静默切换到curl备用方案
                if "SSL module is not available" in str(e):
                    use_curl_fallback = True
                    response = curl_request(
                        api_url,
                        method='POST',
                        data=batch_data,
                        headers={"Content-Type": "application/json"},
                        timeout=30
                    )
                else:
                    raise
            
            # 处理响应
            if response and response.status_code == 200:
                result = response.json()
                if result.get('success'):
                    success_count = result.get('added', 0)
                    fail_count = result.get('failed', 0)
                    skipped_count = result.get('skipped', 0)
                    
                    print("✅ 批量上报完成！")
                    print(f"   成功添加: {success_count} 个")
                    if skipped_count > 0:
                        print(f"   跳过重复: {skipped_count} 个")
                    if fail_count > 0:
                        print(f"   失败: {fail_count} 个")
                else:
                    print(f"❌ 批量上报失败: {result.get('error', '未知错误')}")
                    fail_count = upload_count
            elif response and response.status_code == 403:
                print(f"❌ 认证失败！请检查：")
                print(f"   1. UUID或者路径是否正确")
                print(f"   2. 是否在配置页面开启了 'API管理' 功能")
                fail_count = upload_count
            elif response:
                print(f"❌ 批量上报失败 (HTTP {response.status_code})")
                try:
                    error_detail = response.json()
                    print(f"   错误详情: {error_detail.get('error', '无详情')}")
                except:
                    pass
                fail_count = upload_count
                
        except requests.exceptions.Timeout:
            print(f"❌ 请求超时，请检查网络连接")
            print(f"   建议：检查网络连接或稍后重试")
            fail_count = upload_count
        except requests.exceptions.RequestException as e:
            print(f"❌ 网络错误: {e}")
            print(f"   建议：检查网络连接或API地址是否正确")
            fail_count = upload_count
        except Exception as e:
            print(f"❌ 请求失败: {e}")
            print(f"   建议：检查配置是否正确，或联系技术支持")
            fail_count = upload_count
        
        # 显示统计信息
        print("\n" + "=" * 70)
        print(" 批量上报完成！")
        print("=" * 70)
        print(f"  ✅ 成功添加: {success_count} 个")
        if 'skipped_count' in locals() and skipped_count > 0:
            print(f"  ⚠️  跳过重复: {skipped_count} 个")
        if fail_count > 0:
            print(f"  ❌ 失败: {fail_count} 个")
        print(f"  📊 总计: {upload_count} 个")
        print("=" * 70)
        
        if success_count > 0:
            print(f"\n💡 提示:")
            print(f"   - 您可以访问 https://{worker_domain}/{uuid} 查看管理页面")
            print(f"   - 优选IP已添加，订阅生成时会自动使用")
            print(f"   - 批量上报速度更快，避免了逐个请求的超时问题")
        
        # 返回上传配置信息
        return {
            "upload_method": "api",
            "worker_domain": worker_domain,
            "uuid": uuid,
            "upload_count": upload_count,
            "clear_existing": should_clear  # 保存清空选项
        }
        
    except Exception as e:
        print(f"❌ 读取测速结果失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def upload_to_github(result_file="result.csv"):
    """上传优选结果到 GitHub 公开仓库
    
    Returns:
        dict: 上传配置信息，包含上传方式、相关参数等，如果未上传则返回None
    """
    print("\n" + "=" * 70)
    print(" GitHub 仓库上传")
    print("=" * 70)
    print(" 此功能可以将测速结果上传到 GitHub 公开仓库")
    print(" 需要提供 GitHub Personal Access Token")
    print("=" * 70)
    
    # 检查结果文件是否存在
    if not os.path.exists(result_file):
        print(f"❌ 未找到测速结果文件: {result_file}")
        print("请先完成测速后再上传结果")
        return None
    
    # 尝试加载保存的配置
    saved_config = load_config()
    github_token = None
    repo_info = None
    file_path = "cloudflare_ips.txt"
    
    if saved_config:
        saved_token = saved_config.get('github_token', '')
        saved_repo = saved_config.get('repo_info', '')
        saved_file_path = saved_config.get('file_path', 'cloudflare_ips.txt')
        last_used = saved_config.get('github_last_used', '未知')
        
        if saved_token and saved_repo:
            print(f"\n💾 检测到上次使用的配置:")
            print(f"   GitHub Token: {saved_token[:10]}...{saved_token[-4:]}")
            print(f"   仓库: {saved_repo}")
            print(f"   文件路径: {saved_file_path}")
            print(f"   上次使用: {last_used}")
            print("\n是否使用上次的配置？")
            print("  1. 是 - 使用上次配置")
            print("  2. 否 - 输入新的配置")
            print("  3. 清除配置 - 删除保存的配置")
            
            while True:
                config_choice = input("\n请选择 [1/2/3]: ").strip()
                if config_choice == "1":
                    github_token = saved_token
                    repo_info = saved_repo
                    file_path = saved_file_path
                    print(f"\n✅ 使用保存的配置")
                    print(f"   仓库: {repo_info}")
                    print(f"   文件路径: {file_path}")
                    # 更新最后使用时间
                    save_config(github_token=github_token, repo_info=repo_info, file_path=file_path)
                    break
                elif config_choice == "2":
                    print("\n请输入新的配置...")
                    break
                elif config_choice == "3":
                    # 只清除GitHub配置，保留API配置
                    if os.path.exists(CONFIG_FILE):
                        try:
                            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                                config = json.load(f)
                            config.pop('github_token', None)
                            config.pop('repo_info', None)
                            config.pop('file_path', None)
                            config.pop('github_last_used', None)
                            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                                json.dump(config, f, ensure_ascii=False, indent=2)
                            print("✅ 已清除保存的GitHub配置")
                        except:
                            pass
                    print("请重新输入配置...")
                    break
                else:
                    print("✗ 请输入 1、2 或 3")
    
    # 如果没有使用保存的配置，则获取新的配置
    if not github_token or not repo_info:
        # 获取 GitHub Token
        print("\n📝 请输入您的 GitHub Personal Access Token")
        print("提示: 如果没有Token，请访问 https://github.com/settings/tokens 创建")
        print("     需要 repo 权限")
        
        github_token = input("\nGitHub Token: ").strip()
        if not github_token:
            print("❌ Token 不能为空")
            return None
        
        # 获取仓库信息
        print("\n📝 请输入仓库信息")
        print("格式: owner/repo (例如: username/repo-name)")
        
        repo_info = input("\n仓库 (owner/repo): ").strip()
        if not repo_info or '/' not in repo_info:
            print("❌ 仓库格式不正确，应为 owner/repo")
            return None
        
        # 获取文件路径
        file_path_input = input("\n文件路径 [默认: cloudflare_ips.txt]: ").strip()
        if file_path_input:
            file_path = file_path_input
        
        # 询问是否保存配置
        save_choice = input("\n是否保存此配置供下次使用？[Y/n]: ").strip().lower()
        if save_choice not in ['n', 'no']:
            if save_config(github_token=github_token, repo_info=repo_info, file_path=file_path):
                print("✅ 配置已保存")
            else:
                print("⚠️  配置保存失败，但不影响本次上传")
    
    repo_parts = repo_info.split('/', 1)
    owner = repo_parts[0]
    repo = repo_parts[1]
    
    # 读取测速结果
    print("\n📊 正在读取测速结果...")
    try:
        best_ips = []
        with open(result_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # 安全获取数据，避免NoneType错误
                ip = (row.get('IP 地址') or '').strip()
                port = (row.get('端口') or '').strip()
                
                # 尝试多种可能的列名来获取速度
                speed = ''
                for speed_key in ['下载速度(MB/s)', '下载速度 (MB/s)', '下载速度']:
                    if speed_key in row and row[speed_key] is not None:
                        speed = str(row[speed_key]).strip()
                        break
                
                # 尝试多种可能的列名来获取延迟
                latency = ''
                for latency_key in ['平均延迟', '延迟', 'latency']:
                    if latency_key in row and row[latency_key] is not None:
                        latency = str(row[latency_key]).strip()
                        break
                
                # 获取地区码
                region_code = (row.get('地区码') or '').strip()
                
                # 如果IP地址中包含端口信息
                if ip and ':' in ip:
                    ip_parts = ip.split(':')
                    if len(ip_parts) == 2:
                        ip = ip_parts[0]
                        if not port:
                            port = ip_parts[1]
                
                # 设置默认端口
                if not port:
                    port = '443'
                
                if ip:
                    try:
                        speed_val = float(speed) if speed else 0
                        latency_val = latency if latency else 'N/A'
                        
                        # 获取地区中文名称
                        region_name = '未知地区'
                        if region_code and region_code in AIRPORT_CODES:
                            region_name = AIRPORT_CODES[region_code].get('name', region_code)
                        elif region_code:
                            region_name = region_code
                        
                        best_ips.append({
                            'ip': ip,
                            'port': int(port),
                            'speed': speed_val,
                            'latency': latency_val,
                            'region_code': region_code,
                            'region_name': region_name
                        })
                    except ValueError:
                        continue
        
        if not best_ips:
            print("❌ 未找到有效的测速结果")
            return
        
        print(f"✅ 找到 {len(best_ips)} 个测速结果")
        
        # 询问要上传多少个结果
        while True:
            count_input = input(f"\n请输入要上传的IP数量 [默认: 10, 最多: {len(best_ips)}]: ").strip()
            if not count_input:
                upload_count = min(10, len(best_ips))
                break
            try:
                upload_count = int(count_input)
                if upload_count <= 0:
                    print("✗ 请输入大于0的数字")
                    continue
                if upload_count > len(best_ips):
                    print(f"⚠️  最多只能上传 {len(best_ips)} 个结果")
                    upload_count = len(best_ips)
                break
            except ValueError:
                print("✗ 请输入有效的数字")
        
        # 显示将要上传的IP
        print(f"\n将上传以下 {upload_count} 个优选IP:")
        print("-" * 70)
        for i, ip_info in enumerate(best_ips[:upload_count], 1):
            region_display = f"{ip_info['region_name']}" if ip_info.get('region_name') else '未知地区'
            print(f"  {i:2d}. {ip_info['ip']:15s}:{ip_info['port']:<5d} - {ip_info['speed']:.2f} MB/s - {region_display} - 延迟: {ip_info['latency']}")
        print("-" * 70)
        
        # 确认上传
        confirm = input("\n确认上传以上IP？[Y/n]: ").strip().lower()
        if confirm in ['n', 'no']:
            print("取消上传")
            return
        
        # 格式化数据为换行符分隔的格式（包含注释，和Cloudflare Workers API一样）
        print("\n🚀 开始上传到 GitHub 仓库...")
        content_lines = []
        for ip_info in best_ips[:upload_count]:
            # 构建节点名称：地区名-速度MB/s（和Cloudflare Workers API一样）
            region_name = ip_info.get('region_name', '未知地区')
            speed = ip_info['speed']
            name = build_node_name(region_name, speed)
            # 格式：IP:端口#地区名-速度MB/s（井号前后无空格）
            content_lines.append(f"{ip_info['ip']}:{ip_info['port']}#{name}")
        
        # 使用换行符连接所有行
        content = '\n'.join(content_lines)
        
        # 检查文件是否已存在
        print(f"\n🔍 正在检查文件是否存在...")
        file_sha = None
        try:
            try:
                check_response = requests.get(
                    f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}",
                    headers={
                        "Authorization": f"token {github_token}",
                        "Accept": "application/vnd.github.v3+json"
                    },
                    timeout=10
                )
                if check_response.status_code == 200:
                    file_data = check_response.json()
                    file_sha = file_data.get('sha', '')
                    print(f"⚠️  文件已存在，将更新文件")
                elif check_response.status_code == 404:
                    print(f"✅ 文件不存在，将创建新文件")
                else:
                    print(f"⚠️  无法检查文件状态，将尝试创建/更新")
            except ImportError as e:
                # SSL模块不可用，静默切换到curl
                if "SSL module is not available" in str(e):
                    check_response = curl_request(
                        f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}",
                        method='GET',
                        headers={
                            "Authorization": f"token {github_token}",
                            "Accept": "application/vnd.github.v3+json"
                        },
                        timeout=10
                    )
                    if check_response.status_code == 200:
                        file_data = check_response.json()
                        file_sha = file_data.get('sha', '')
                        print(f"⚠️  文件已存在，将更新文件")
                    elif check_response.status_code == 404:
                        print(f"✅ 文件不存在，将创建新文件")
                    else:
                        print(f"⚠️  无法检查文件状态，将尝试创建/更新")
                else:
                    raise
        except Exception as e:
            print(f"⚠️  检查文件状态失败: {e}，将尝试创建/更新")
        
        # 准备上传数据
        import base64
        content_bytes = content.encode('utf-8')
        content_base64 = base64.b64encode(content_bytes).decode('utf-8')
        
        upload_data = {
            "message": f"更新Cloudflare优选IP列表 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "content": content_base64
        }
        
        # 如果文件已存在，需要提供sha
        if file_sha:
            upload_data["sha"] = file_sha
        
        # 上传到 GitHub 仓库
        try:
            try:
                if file_sha:
                    # 更新文件
                    response = requests.put(
                        f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}",
                        json=upload_data,
                        headers={
                            "Authorization": f"token {github_token}",
                            "Accept": "application/vnd.github.v3+json"
                        },
                        timeout=30
                    )
                else:
                    # 创建文件
                    response = requests.put(
                        f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}",
                        json=upload_data,
                        headers={
                            "Authorization": f"token {github_token}",
                            "Accept": "application/vnd.github.v3+json"
                        },
                        timeout=30
                    )
            except ImportError as e:
                # SSL模块不可用，静默切换到curl
                if "SSL module is not available" in str(e):
                    response = curl_request(
                        f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}",
                        method='PUT',
                        data=upload_data,
                        headers={
                            "Authorization": f"token {github_token}",
                            "Accept": "application/vnd.github.v3+json"
                        },
                        timeout=30
                    )
                else:
                    raise
            
            if response and response.status_code in [200, 201]:
                result = response.json()
                file_url = result.get('content', {}).get('html_url', '')
                
                # 尝试获取默认分支
                default_branch = "main"  # 默认使用main分支
                try:
                    try:
                        repo_response = requests.get(
                            f"https://api.github.com/repos/{owner}/{repo}",
                            headers={
                                "Authorization": f"token {github_token}",
                                "Accept": "application/vnd.github.v3+json"
                            },
                            timeout=10
                        )
                        if repo_response.status_code == 200:
                            repo_data = repo_response.json()
                            default_branch = repo_data.get('default_branch', 'main')
                    except:
                        pass
                except:
                    pass
                
                raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{default_branch}/{file_path}"
                
                print("\n" + "=" * 70)
                print(" ✅ 上传成功！")
                print("=" * 70)
                print(f"  仓库地址: https://github.com/{owner}/{repo}")
                if file_url:
                    print(f"  文件地址: {file_url}")
                print(f"  原始文件地址: {raw_url}")
                print(f"  上传数量: {upload_count} 个IP")
                print("=" * 70)
                
                print(f"\n💡 提示:")
                print(f"   - 您可以使用原始文件地址直接访问IP列表")
                print(f"   - 文件格式为换行符分隔，每行一个 IP:端口#地区名-速度MB/s")
                print(f"   - 您可以在GitHub上管理这个仓库")
                
                # 返回上传配置信息
                return {
                    "upload_method": "github",
                    "repo_info": f"{owner}/{repo}",
                    "github_token": github_token,
                    "file_path": file_path,
                    "upload_count": upload_count
                }
            elif response and response.status_code == 401:
                print(f"❌ 认证失败！请检查：")
                print(f"   1. GitHub Token 是否正确")
                print(f"   2. Token 是否具有 repo 权限")
            elif response and response.status_code == 404:
                print(f"❌ 仓库不存在或无权限！请检查：")
                print(f"   1. 仓库路径是否正确: {owner}/{repo}")
                print(f"   2. Token 是否有该仓库的写入权限")
            elif response:
                print(f"❌ 上传失败 (HTTP {response.status_code})")
                try:
                    error_detail = response.json()
                    print(f"   错误详情: {error_detail.get('message', '无详情')}")
                except:
                    pass
        except requests.exceptions.Timeout:
            print(f"❌ 请求超时，请检查网络连接")
            print(f"   建议：检查网络连接或稍后重试")
        except requests.exceptions.RequestException as e:
            print(f"❌ 网络错误: {e}")
            print(f"   建议：检查网络连接或GitHub API地址是否正确")
        except Exception as e:
            print(f"❌ 上传失败: {e}")
            print(f"   建议：检查配置是否正确，或联系技术支持")
        
    except Exception as e:
        print(f"❌ 读取测速结果失败: {e}")
        import traceback
        traceback.print_exc()
        return None


# ============================================================================
# WS 白名单自愈状态 + 采样预算反向推算
# ----------------------------------------------------------------------------
# 背景：WS_OK_CIDR_PREFIXES 只是某次实测的快照，Cloudflare 的拦截策略会漂移，
# 写死的白名单会静默过期。这里把每次上传前 WS 握手的真实结果回写到 /16 粒度：
#   - 连续 3 次失败 → 该 /16 熔断 7 天（到期后自动以最低权重重新参与采样复测）
#   - 采样预算按历史转化率反推（目标数 / 转化率 × 1.5），不再写死 1500
# ============================================================================
WS_STATE_FILE = "ws_prefix_state.json"
WS_RUN_STATS_FILE = "ws_run_stats.jsonl"
WS_COOLDOWN_DAYS = 7
WS_FAIL_STREAK_LIMIT = 3

# 本次运行候选池的统计，供上报阶段写进 run stats
_POOL_STATS = {"sampled": 0, "alive": 0}


def _ws_prefix_key(ip):
    """104.16.134.105 -> '104.16'"""
    return ".".join(str(ip).split(".")[:2])


def _load_ws_state():
    try:
        with open(WS_STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    if not isinstance(st, dict):
        st = {}
    st.setdefault("version", 1)
    st.setdefault("prefixes", {})
    return st


def _save_ws_state(st):
    st["updated"] = datetime.now().isoformat(timespec="seconds")
    tmp = WS_STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, WS_STATE_FILE)
    except Exception as e:
        print(f"⚠️  写入白名单状态失败: {e}")


_WS_STATE_LOCK = threading.Lock()


def record_ws_verify_result(ip, ok):
    """把一次 WS 握手验证结果记到对应 /16 上，失败可触发熔断

    并发验证时多个线程会同时读改写同一份 JSON，不加锁会互相覆盖，
    导致 ok/fail 计数少记（只影响历史评分，不影响本次选出的 IP）。
    """
    key = _ws_prefix_key(ip)
    if key + "." not in WS_OK_CIDR_PREFIXES:
        return  # 不在白名单内的段不参与评分
    with _WS_STATE_LOCK:
        st = _load_ws_state()
        rec = st["prefixes"].setdefault(key, {"ok": 0, "fail": 0, "streak": 0})
        now = time.time()
        if ok:
            rec["ok"] = rec.get("ok", 0) + 1
            rec["streak"] = 0
            rec["last_ok"] = int(now)
            rec.pop("cool_until", None)
        else:
            rec["fail"] = rec.get("fail", 0) + 1
            rec["streak"] = rec.get("streak", 0) + 1
            rec["last_fail"] = int(now)
            if rec["streak"] >= WS_FAIL_STREAK_LIMIT:
                rec["cool_until"] = int(now + WS_COOLDOWN_DAYS * 86400)
                print(f"🧊 {key}.x 连续 {rec['streak']} 次 WS 握手失败，熔断 {WS_COOLDOWN_DAYS} 天")
        _save_ws_state(st)


def _ws_prefix_weight(rec):
    """评分 = 0.2 底座 + 历史成功率，保证没熔断的段仍会被少量采样"""
    ok = rec.get("ok", 0)
    fail = rec.get("fail", 0)
    total = ok + fail
    rate = (ok / total) if total else 0.5
    return 0.2 + rate


def active_ws_prefixes():
    """当前可采样的 [(prefix, weight)]，已熔断的段被剔除"""
    st = _load_ws_state()
    now = time.time()
    out = []
    for p in WS_OK_CIDR_PREFIXES:
        key = p.rstrip(".")
        rec = st["prefixes"].get(key, {})
        cu = rec.get("cool_until")
        if cu and cu > now:
            continue
        out.append((key, _ws_prefix_weight(rec)))
    if not out:  # 全被熔断时不要把候选池搞空，退回全白名单等权
        out = [(p.rstrip("."), 1.0) for p in WS_OK_CIDR_PREFIXES]
    return out


def _ws_history_conversion(sample_n=20):
    """历史转化率中位数：通过 WS 验证的 IP 数 / 采样数"""
    vals = []
    try:
        with open(WS_RUN_STATS_FILE, "r", encoding="utf-8") as f:
            for line in f.readlines()[-sample_n:]:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                s = rec.get("sampled") or 0
                v = rec.get("verified_ok") or 0
                if s > 0:
                    vals.append(v / s)
    except Exception:
        return None
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def target_sample_size(target_uploads=10, default=1500):
    """采样预算 = 目标数 / 历史转化率 × 1.5，限制在 1200~6000"""
    conv = _ws_history_conversion()
    if not conv or conv <= 0:
        return default
    budget = int(math.ceil(target_uploads * 1.5 / conv))
    return max(1200, min(6000, budget))


def record_run_stats(**kw):
    """追加一行运行统计，供下次反推预算"""
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "sampled": _POOL_STATS.get("sampled", 0),
        "alive": _POOL_STATS.get("alive", 0),
    }
    rec.update(kw)
    try:
        with open(WS_RUN_STATS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def select_diverse_ips(ip_infos, count, per_24=2):
    """同一 /24 最多取 per_24 个，避免整批 IP 压在同一段位上一起失效

    第一轮按配额挑，挑不满再放宽配额补足（宁可用满名额，也不留空位）。
    """
    picked, seen, rest = [], {}, []
    for info in ip_infos:
        key = ".".join(str(info["ip"]).split(".")[:3])
        if seen.get(key, 0) >= per_24:
            rest.append(info)
            continue
        picked.append(info)
        seen[key] = seen.get(key, 0) + 1
        if len(picked) >= count:
            return picked
    for info in rest:
        if len(picked) >= count:
            break
        picked.append(info)
    return picked


def select_by_colo(ip_infos, count, per_colo=2):
    """同一个 colo（Cloudflare 机房）最多取 per_colo 个

    为什么不能只按 TTFB 排序取前 N：TTFB 最优的那批几乎全落在一个机房
    （实测全是 LAX），一旦该机房故障或路由劣化，10 个 IP 同时失效。
    实测本机可达 5 个 colo（LAX/AMS/LHR/FRA/CDG），TTFB 中位数差不到 150ms，
    混用几乎不损失速度，所以按机房配额分摊。
    挑不满时逐轮把上限 +1 放宽，实在凑不够才退回不限。
    """
    if not ip_infos:
        return []
    if not any(info.get('colo') for info in ip_infos):
        # colo 全部探测失败：退回只做 /24 去重
        return select_diverse_ips(ip_infos, count, per_24=2)

    for limit in range(max(1, per_colo), count + 1):
        picked, seen = [], {}
        for info in ip_infos:
            key = info.get('colo') or 'unknown'
            if seen.get(key, 0) >= limit:
                continue
            picked.append(info)
            seen[key] = seen.get(key, 0) + 1
            if len(picked) >= count:
                # 再叠一层 /24 去重，避免同段扎堆
                return select_diverse_ips(picked, count, per_24=2)
    return select_diverse_ips(picked, count, per_24=2)


# 候选池截断数量（0 = 不截断）。由 run_with_args 从 --top 赋值。
POOL_TOP_N = 0
# RTT 硬门槛（毫秒）：>0 时只保留 TCP 建连耗时不超过该值的 IP。
# 用途：CF 优选池里混着远端边缘（如新加坡 104.18.3x/4x，从北美测约 180ms），
# 按下载速度排序会把它们选出来，但端到端延迟比本地边缘高 300ms 左右，
# 握手和 TCP 慢启动被拖慢，日常上网反而更差。0 表示不限制。
POOL_MAX_RTT = 0.0
# 最近一次 measure_tcp_rtt 的结果 {ip: rtt_ms}，供 fast_select 等外部复用
POOL_RTT_MAP = {}

# 本地 colo 段：实测 104.16~104.19 从本机进来全部落 LAX；
# 104.20/21/24/25/27 与 172.66/67 落 AMS/LHR/FRA/CDG（少量 LAX）。
# 截断候选池时按这两组各占一半名额，否则纯按 RTT 排序会把远端 colo 全挤掉，
# 最终 10 个 IP 全在一个机房，该机房故障即全灭。
_SEG_GROUP_LOCAL = {"104.16", "104.17", "104.18", "104.19"}


def _seg_group(ip):
    return "local" if ip.rsplit(".", 2)[0] in _SEG_GROUP_LOCAL else "remote"


def _quota_by_seg(ranked, top_n, local_ratio=0.5):
    """按地区组配额截断候选池，某组不足时由另一组补齐

    ranked: [(ip, rtt_ms), ...] 已按 RTT 升序
    """
    local_quota = int(round(top_n * local_ratio))
    local = [x for x in ranked if _seg_group(x[0]) == "local"]
    remote = [x for x in ranked if _seg_group(x[0]) != "local"]
    picked = local[:local_quota] + remote[:top_n - local_quota]
    if len(picked) < top_n:
        used = {ip for ip, _ in picked}
        for item in ranked:
            if len(picked) >= top_n:
                break
            if item[0] not in used:
                picked.append(item)
                used.add(item[0])
    picked.sort(key=lambda x: x[1])
    return picked


def build_ws_verified_ip_list(max_per_seg=None):
    """生成只含 WS 可用网段的 Cloudflare IP 列表，写入 CLOUDFLARE_IP_FILE

    为什么要预筛：Cloudflare 全量 IPv4 网段中只有约 13% 能连上你的 Worker（error 1034），
    而 CloudflareST 测速是逐个 IP 跑握手+下载，喂给它必然被拒的 IP 会：
      1) 大幅拖慢测速（--thread 500 也压不住）
      2) 排名靠前的"高速IP"全是不可用的，污染最终结果

    这里直接把候选池限制在实测可用段（104.16/104.24）内，再叠加上传前的
    逐个握手验证作为第二道保险。

    失败时返回 False，此时调用方应退回"不过滤、只靠上传前验证"的保守模式。
    """
    import ipaddress
    import random
    import re
    import subprocess

    if not WS_OK_CIDR_PREFIXES:
        return False

    # 取官方发布的真实网段，再筛出白名单前缀对应的块。
    # 不随机造 IP：Cloudflare 段内并非所有地址都已分配，随机造会得到大量
    # 连接失败(curl 返回 000)的空地址，白白占用候选位。
    try:
        # 用 curl 而非 urllib：urllib 的 TLS 栈在部分网络环境下会握手失败
        proc = subprocess.run(
            ['/usr/bin/curl', '-s', '--connect-timeout', '10', '--max-time', '20',
             CLOUDFLARE_IP_URL],
            capture_output=True, text=True, timeout=30
        )
        all_cidrs = re.findall(r'\d+\.\d+\.\d+\.\d+/\d+', proc.stdout or '')
        if not all_cidrs:
            print("⚠️  未能解析到 Cloudflare 官方网段")
            return False
    except Exception as e:
        print(f"⚠️  获取 Cloudflare 官方网段失败: {e}")
        return False

    # 保留与白名单前缀相交的官方网段。
    # 注意：不能只看网段起点是否匹配—— 官方 172.64.0.0/13 覆盖 172.64~172.71，
    # 起点 "172.64." 不在白名单里，但块内的 172.66/172.67 是可用的。
    # 所以只要网段与任一白名单前缀**有交集**就整块保留，采样时再逐个地址过滤。
    kept_blocks = []
    for cidr in all_cidrs:
        net = ipaddress.ip_network(cidr, strict=False)
        if any(net.overlaps(ipaddress.ip_network(p + '0.0/16')) for p in WS_OK_CIDR_PREFIXES):
            kept_blocks.append(net)

    if not kept_blocks:
        print("⚠️  官方网段中未匹配到白名单前缀")
        return False

    # 采样：按 /16 配额分配，直接在白名单段内撒点。
    # 旧做法是在整块（/13、/14）里均匀撒再逐地址过滤，可三块 20 个 /16 里只有 11 个
    # 落在白名单，41.5% 的点被白白丢掉；按配额撒同样的预算能多拿约 40% 的有效候选。
    random.seed()
    if not max_per_seg:
        max_per_seg = target_sample_size()

    pairs = []
    for key, weight in active_ws_prefixes():
        pnet = ipaddress.ip_network(key + '.0.0/16')
        if any(pnet.subnet_of(b) for b in kept_blocks):
            pairs.append((key, weight))

    if not pairs:
        print("⚠️  白名单段与官方网段无交集")
        return False

    total_w = sum(w for _, w in pairs)
    ips = []
    for key, weight in pairs:
        quota = max(1, int(round(max_per_seg * weight / total_w)))
        base = int(ipaddress.ip_address(key + '.0.0'))
        for _ in range(quota):
            # 跳过 .0.0（网络号）和 .255.255（广播地址）
            ips.append(str(ipaddress.ip_address(base + random.randrange(1, 65535))))

    ips = list(dict.fromkeys(ips))
    if not ips:
        return False
    print(f"   采样预算: {max_per_seg} 个（{len(pairs)} 个 /16 按 WS 评分分配，实际撒点 {len(ips)}）")

    # TCP 探活：Cloudflare 段内并非所有地址都已分配，随机采样会命中大量空地址
    # （实测 172.66/172.67 只有 30~50% 可连，104.20 只有 20%）。
    # 不探活的话 CloudflareST 会把这些连不上的全部跳过，最终 "可用: 0"，
    # 看起来像线程不够/崩溃，实际是候选池无效。
    sampled_count = len(ips)
    alive = probe_tcp_alive(ips, port=443, timeout=3, workers=64)
    if not alive:
        print("⚠️  TCP 探活后无可用 IP，返回 False（退回全量模式）")
        return False

    ips = alive
    # 按建连延迟排序后截断（--top N）：下载测速是串行、单 IP 约 10 秒，
    # 把整个候选池都测一遍会把周任务拖到 1.5 小时；而实测低延迟那批的达标率
    # （约 55%）远高于全池（约 6%），砍掉高延迟尾巴对结果几乎无损。
    if (POOL_TOP_N or POOL_MAX_RTT > 0) and ips:
        ranked = measure_tcp_rtt(ips, port=443, timeout=2, workers=200)
        POOL_RTT_MAP.clear()
        POOL_RTT_MAP.update(ranked)
        # RTT 硬门槛：先切掉远端边缘（新加坡等），再按延迟排序截断。
        # 远端边缘带宽往往更大，纯按速度选会优先命中它们，但端到端高 300ms。
        if POOL_MAX_RTT > 0 and ranked:
            before = len(ranked)
            passed = [(ip, r) for ip, r in ranked if r <= POOL_MAX_RTT]
            if passed:
                print(f"   RTT 门槛 {POOL_MAX_RTT:.0f}ms: {before} -> {len(passed)} 个"
                      f"（{passed[0][1]:.0f}~{passed[-1][1]:.0f} ms）")
                ranked = passed
            else:
                print(f"⚠️  RTT 门槛 {POOL_MAX_RTT:.0f}ms 过滤后为空"
                      f"（最快也有 {ranked[0][1]:.0f}ms），忽略门槛")
        if not ranked:
            print("⚠️  延迟排序全部失败，保留原候选池")
        elif POOL_TOP_N and len(ranked) > POOL_TOP_N:
            picked = _quota_by_seg(ranked, POOL_TOP_N)
            n_local = sum(1 for ip, _ in picked if _seg_group(ip) == "local")
            print(f"   延迟排序: 保留 {len(picked)} 个"
                  f"（{picked[0][1]:.0f}~{picked[-1][1]:.0f} ms，"
                  f"美西段 {n_local} 个 / 欧洲等其他段 {len(picked) - n_local} 个），"
                  f"其余 {len(ranked) - len(picked)} 个不进入下载测速")
            ips = [ip for ip, _ in picked]
        else:
            if POOL_TOP_N:
                print(f"⚠️  延迟排序只测通 {len(ranked)} 个（少于 --top {POOL_TOP_N}），按实测结果全部保留")
            ips = [ip for ip, _ in ranked]
    _POOL_STATS.update(sampled=sampled_count, alive=len(alive))
    try:
        with open(CLOUDFLARE_IP_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(ips) + '\n')
        print(f"✅ 已生成 WS 可用候选池: {len(ips)} 个 IP（已通过 TCP 探活）")
        print(f"   来源: {' '.join(str(n) for n in kept_blocks)}")
        return True
    except Exception as e:
        print(f"⚠️  写入候选 IP 列表失败: {e}")
        return False


def measure_tcp_rtt(ips, port=443, timeout=2, workers=200):
    """并发测量每个 IP 的 TCP 建连耗时，返回 [(ip, rtt_ms), ...] 按延迟升序

    用途：给 --top 做截断排序。这里只关心"能不能连、连得多快"，
    不做 TLS/HTTP 握手，所以单次成本就是一次 TCP 三次握手。
    连不上的直接丢弃（这些 IP 送进下载测速也只会耗满超时窗口）。
    """
    import socket
    from concurrent.futures import ThreadPoolExecutor

    def _one(ip):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        t0 = time.perf_counter()
        try:
            sock.connect((ip, port))
            return ip, (time.perf_counter() - t0) * 1000
        except OSError:
            return ip, None
        finally:
            try:
                sock.close()
            except OSError:
                pass

    if not ips:
        return []

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(ips)))) as pool:
        results = list(pool.map(_one, ips))

    ranked = [(ip, rtt) for ip, rtt in results if rtt is not None]
    ranked.sort(key=lambda x: x[1])
    return ranked


def probe_tcp_alive(ips, port=443, timeout=3, workers=64):
    """并发探测 IP 的 TCP 端口是否可达，返回存活列表

    Cloudflare 的网段内并非所有地址都已分配，随机采样会大量命中空地址
    （实测 172.66/172.67 仅 30~50% 可连，104.20 仅 20%）。
    这些地址会让 CloudflareST 反复超时，拖慢甚至拖垮测速。

    用 xargs -P 并发跑 nc，避免 Python 逐个 socket 探测太慢。
    """
    import subprocess
    import tempfile

    if not ips:
        return []

    with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False) as f:
        f.write('\n'.join(ips) + '\n')
        listfile = f.name

    try:
        # 不用 sh -c 套一层（嵌套引号容易被转义破坏导致静默无输出），
        # 直接把 ip 作为 $1 传给内层 sh。
        cmd = (
            f'xargs -P {workers} -n1 /bin/sh -c '
            f'\'nc -z -G {timeout} "$1" {port} 2>/dev/null && echo "$1"\' _ < {listfile}'
        )
        proc = subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=len(ips) * timeout + 60)
        alive = [ln.strip() for ln in proc.stdout.splitlines()
                 if ln.strip() and not ln.startswith('/')]
    except Exception as e:
        print(f"⚠️  TCP 探活失败: {e}")
        return list(ips)
    finally:
        try:
            os.unlink(listfile)
        except OSError:
            pass

    total = len(ips)
    got = len(alive)
    if got < total:
        print(f"   TCP 探活: {got}/{total} 个地址可用（其余为未分配地址）")
    return alive


def verify_ip_usable(ip, port=443, timeout=8):
    """验证某个优选 IP 是否真能用作代理（WS 握手能否成功）

    Cloudflare 优选 IP 有个陷阱：测速脚本测的是 speed.cloudflare.com（CF 自家域名）
    的下载速度，任何 CF IP 都能测出高速；但真正要连的是 worker 域名，
    Cloudflare 边缘节点会对部分 IP 段返回 "error code: 1034" 拒绝服务。

    1034 的含义：Cloudflare 边缘拦截了对其它客户域名的访问，属于默认安全策略，
    请求根本没到 Worker 代码。所以换协议（xhttp）也没用，拦截在 HTTP 层之前。

    实测表现（2026-10-18）：
      - 173.245.58.83（测速31MB/s，排第一）→ 403 + error code: 1034
      - 104.17.8.140（测速 24MB/s）→ 101 Switching Protocols ✅

    这里用真实 WS 握手验证，期望返回 101。其它状态码都视为不可用。
    关键：必须加 --noproxy '*' 绕过系统代理，否则测的是代理出口而非该 IP 本身。
    """
    import subprocess
    try:
        result = subprocess.run(
            [
                '/usr/bin/curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
                '--noproxy', '*', '--http1.1',
                '--resolve', f'{WORKER_VERIFY_DOMAIN}:{port}:{ip}',
                '-H', f'Host: {WORKER_VERIFY_DOMAIN}',
                '-H', 'Upgrade: websocket',
                '-H', 'Connection: Upgrade',
                '-H', 'Sec-WebSocket-Version: 13',
                '-H', 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==',
                '--connect-timeout', str(timeout), '--max-time', str(timeout + 2),
                f'https://{WORKER_VERIFY_DOMAIN}{WORKER_VERIFY_PATH}',
            ],
            capture_output=True, text=True, timeout=timeout + 4
        )
        ok = result.stdout.strip() == '101'
        record_ws_verify_result(ip, ok)
        return ok
    except Exception:
        record_ws_verify_result(ip, False)
        return False


def measure_worker_ttfb(ip, port=443, uuid="node", timeout=8, rounds=2):
    """测量该 IP 到 Worker 的真实首字节时间（毫秒），多轮取最小值；失败返回 None

    为什么不能只看 TCP RTT：TCP 建连只覆盖第一段网络，TLS 握手（2 个往返）
    和边缘回源的耗时它完全看不到。实测存在 TCP 建连 84ms、完整请求却要 1100ms
    的 IP（新加坡 104.18.36.99），按 RTT 排序会把它排到第一位。
    TTFB 把建连 + 加密 + 请求 + Worker 响应全算进去，和用户实际感受到的首屏
    等待最接近，所以用来做上传前的最后一轮排序。
    """
    import subprocess
    best = None
    url = f"https://{WORKER_VERIFY_DOMAIN}/{uuid}/api/preferred-ips"
    for _ in range(max(1, rounds)):
        try:
            r = subprocess.run(
                [
                    '/usr/bin/curl', '-s', '-o', '/dev/null',
                    '-w', '%{time_starttransfer}', '--noproxy', '*',
                    '--resolve', f'{WORKER_VERIFY_DOMAIN}:{port}:{ip}',
                    '--connect-timeout', str(timeout),
                    '--max-time', str(timeout + 2),
                    url,
                ],
                capture_output=True, text=True, timeout=timeout + 4
            )
            v = float(r.stdout.strip() or 0) * 1000
            if v > 0 and (best is None or v < best):
                best = v
        except Exception:
            continue
    return best


def measure_ttfb_and_colo(ip, port=443, timeout=8, rounds=2):
    """一次请求同时取回 TTFB（毫秒）和真实落地机房；失败返回 (None, None)

    原先分两次跑：先用 Worker API 测 TTFB，再请求 /cdn-cgi/trace 查机房。
    两次请求的 TCP 建连 + TLS 握手完全重复，等于把最贵的一段付了两遍。
    /cdn-cgi/trace 由边缘直接返回，它的 time_starttransfer 同样包含建连、
    握手和边缘响应，跨机房的物理距离仍是主导项，用来做排序与原来等价。
    """
    import subprocess
    best_ttfb = None
    colo = None
    url = f"https://{WORKER_VERIFY_DOMAIN}/cdn-cgi/trace"
    marker = '---WBSPLIT---|'
    for _ in range(max(1, rounds)):
        try:
            r = subprocess.run(
                [
                    '/usr/bin/curl', '-s', '-w', f'\n{marker}%{{time_starttransfer}}',
                    '--noproxy', '*',
                    '--resolve', f'{WORKER_VERIFY_DOMAIN}:{port}:{ip}',
                    '--connect-timeout', str(timeout),
                    '--max-time', str(timeout + 2),
                    url,
                ],
                capture_output=True, text=True, timeout=timeout + 4
            )
            out = r.stdout or ''
            if marker not in out:
                continue
            body, _, tail = out.rpartition(marker)
            try:
                v = float(tail.strip() or 0) * 1000
            except ValueError:
                v = 0
            if colo is None:
                for line in body.splitlines():
                    if line.startswith('colo='):
                        colo = line.split('=', 1)[1].strip() or None
                        break
            if v > 0 and (best_ttfb is None or v < best_ttfb):
                best_ttfb = v
        except Exception:
            continue
    return best_ttfb, colo


def measure_colo(ip, port=443, timeout=8):
    """返回该 IP 实际落地的 Cloudflare 机房代码（LAX/AMS/LHR…），失败返回 None

    为什么不能按 IP 段判断：段号和机房不对应。实测 172.66 同一段里既有 LAX
    也有 AMS/法兰克福，104.21 会落到 AMS/LHR/FRA/CDG 四个机房。
    只有请求 /cdn-cgi/trace 才知道真实落点。
    """
    import subprocess
    try:
        r = subprocess.run(
            ['/usr/bin/curl', '-s', '--noproxy', '*',
             '--resolve', f'{WORKER_VERIFY_DOMAIN}:{port}:{ip}',
             '--connect-timeout', str(timeout), '--max-time', str(timeout + 2),
             f'https://{WORKER_VERIFY_DOMAIN}/cdn-cgi/trace'],
            capture_output=True, text=True, timeout=timeout + 4
        )
        for line in (r.stdout or '').splitlines():
            if line.startswith('colo='):
                return line.split('=', 1)[1].strip() or None
    except Exception:
        pass
    return None


def upload_to_cloudflare_api_cli(result_file="result.csv", worker_domain=None, uuid=None, upload_count=10, clear_existing=False, min_usable=0):
    """命令行模式：上报优选结果到 Cloudflare Workers API
    
    Args:
        result_file: 测速结果文件路径
        worker_domain: Worker域名
        uuid: UUID或路径
        upload_count: 上传IP数量
        clear_existing: 是否清空现有IP（默认: False）
        min_usable: WS 握手通过数的底线（默认: 0，不设底线）。低于该值就整次
            放弃，不清空也不上传。fast_select.py 传 5，避免 --clear 把线上池缩水。
    """
    print("\n" + "=" * 70)
    print(" 命令行模式：Cloudflare Workers API 上报")
    print("=" * 70)
    
    # 检查结果文件是否存在
    if not os.path.exists(result_file):
        print(f"❌ 未找到测速结果文件: {result_file}")
        return
    
    # 构建 API URL
    api_url = f"https://{worker_domain}/{uuid}/api/preferred-ips"
    
    # 检查是否已有数据并决定是否清空
    should_clear = False
    if clear_existing:
        # 如果指定了清空选项，先检查现有数据
        print("\n🔍 正在检查现有优选IP...")
        try:
            try:
                response = requests.get(api_url, timeout=10)
            except ImportError as e:
                # SSL模块不可用，静默切换到curl
                if "SSL module is not available" in str(e):
                    response = curl_request(api_url, method='GET', timeout=10)
                else:
                    raise
            
            if response.status_code == 200:
                result = response.json()
                existing_count = result.get('count', 0)
                if existing_count > 0:
                    print(f"⚠️  发现已存在 {existing_count} 个优选IP")
                    should_clear = True
                else:
                    print("✅ 当前无数据，将直接添加")
            else:
                print("⚠️  无法获取现有数据状态，将尝试清空后添加")
                should_clear = True
        except Exception as e:
            print(f"⚠️  检查现有数据失败: {e}")
            print("将继续尝试清空后添加...")
            should_clear = True
    else:
        # 如果没有指定清空选项，检查现有数据但不清空
        print("\n🔍 正在检查现有优选IP...")
        try:
            try:
                response = requests.get(api_url, timeout=10)
            except ImportError as e:
                # SSL模块不可用，静默切换到curl
                if "SSL module is not available" in str(e):
                    response = curl_request(api_url, method='GET', timeout=10)
                else:
                    raise
            
            if response.status_code == 200:
                result = response.json()
                existing_count = result.get('count', 0)
                if existing_count > 0:
                    print(f"⚠️  发现已存在 {existing_count} 个优选IP")
                    print("💡 提示: 使用 --clear 参数可以在上传前清空现有IP，避免IP累积")
                else:
                    print("✅ 当前无数据，将直接添加")
        except Exception as e:
            print(f"⚠️  检查现有数据失败: {e}")
    
    # 读取测速结果（先读取，确认有数据后再清空）
    print("\n📊 正在读取测速结果...")
    try:
        best_ips = []
        with open(result_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # 安全获取数据，避免NoneType错误
                ip = (row.get('IP 地址') or '').strip()
                port = (row.get('端口') or '').strip()
                
                # 尝试多种可能的列名来获取速度
                speed = ''
                for speed_key in ['下载速度(MB/s)', '下载速度 (MB/s)', '下载速度']:
                    if speed_key in row and row[speed_key] is not None:
                        speed = str(row[speed_key]).strip()
                        break
                
                # 尝试多种可能的列名来获取延迟
                latency = ''
                for latency_key in ['平均延迟', '延迟', 'latency']:
                    if latency_key in row and row[latency_key] is not None:
                        latency = str(row[latency_key]).strip()
                        break
                
                # 获取地区码
                region_code = (row.get('地区码') or '').strip()
                
                # 如果IP地址中包含端口信息
                if ip and ':' in ip:
                    ip_parts = ip.split(':')
                    if len(ip_parts) == 2:
                        ip = ip_parts[0]
                        if not port:
                            port = ip_parts[1]
                
                # 设置默认端口
                if not port:
                    port = '443'
                
                if ip:
                    try:
                        speed_val = float(speed) if speed else 0
                        latency_val = latency if latency else 'N/A'
                        
                        # 获取地区中文名称
                        region_name = '未知地区'
                        if region_code and region_code in AIRPORT_CODES:
                            region_name = AIRPORT_CODES[region_code].get('name', region_code)
                        elif region_code:
                            region_name = region_code
                        
                        best_ips.append({
                            'ip': ip,
                            'port': int(port),
                            'speed': speed_val,
                            'latency': latency_val,
                            'region_code': region_code,
                            'region_name': region_name
                        })
                    except ValueError:
                        continue
        
        if not best_ips:
            print("❌ 未找到有效的测速结果")
            return
        
        # 限制上传数量
        upload_count = min(upload_count, len(best_ips))
        print(f"✅ 找到 {len(best_ips)} 个测速结果，将上传前 {upload_count} 个")
        stage_timer("上传:解析result.csv")

        # 上报前验证 IP 是否真能用作代理（WS 握手验证）
        # 测速速度高 ≠ 能当代理用：Cloudflare 边缘对部分 IP 段访问 Worker 域名
        # 会返回 1034 拒绝，这类 IP 测速分很高但实际连不通。
        print("\n🔬 正在验证 IP 代理可用性（WS 握手）...")
        usable_ips = []
        blocked_ips = []
        # 只验证前 3 倍名额（-dn 调到 50 后逐个验证全部 50 个太慢；
        # 排序已按速度降序，前 3 倍足够挑出 10 个可用的）
        verify_limit = min(len(best_ips), max(upload_count * 3, 20))
        verify_list = best_ips[:verify_limit]
        # 并发 8：curl 收到 101 后连接升级成 WS 长连接，它不会自己断开，
        # 只能一直挂到 --max-time（10 秒）。所以无论验证成功还是失败，单个
        # IP 都要耗满 10 秒，串行 20 个就是 200 秒——实测 203.8/20 个。
        # 这些请求分发到不同 anycast IP、每次只走几十字节，不占带宽，
        # 并发不改变单个 IP 的判定结果。
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=8) as _ex:
            verify_oks = list(_ex.map(
                lambda x: verify_ip_usable(x['ip'], x['port']), verify_list))
        for ip_info, ok in zip(verify_list, verify_oks):
            if ok:
                usable_ips.append(ip_info)
            else:
                blocked_ips.append(ip_info)

        if blocked_ips:
            print(f"⚠️  {len(blocked_ips)} 个 IP 被 Cloudflare 拒绝（error 1034，不能作为代理）:")
            for ip_info in blocked_ips[:5]:
                print(f"     - {ip_info['ip']} ({ip_info['speed']:.2f}MB/s, {ip_info['region_name']})")
            if len(blocked_ips) > 5:
                print(f"     ... 还有 {len(blocked_ips) - 5} 个")

        if not usable_ips:
            record_run_stats(candidates=len(best_ips), verified_ok=0)
            print("❌ 所有 IP 均无法连接Worker，放弃本次上传（不清空现有数据）")
            return
        stage_timer("上传:WS握手验证")

        # 通过数低于底线就整次放弃：不清空、不上传。
        # --clear 会先清空旧池再写新的，若只剩 2 个可用，线上池就从 10 个缩成 2 个，
        # 比"这周不更新"糟糕。min_usable=0 表示不设底线（默认，保持原行为）。
        if min_usable and len(usable_ips) < min_usable:
            record_run_stats(candidates=len(best_ips), verified_ok=len(usable_ips))
            print(f"❌ 仅 {len(usable_ips)} 个 IP 通过 WS 握手，少于底线 {min_usable} 个，"
                  f"放弃本次上传（不清空现有数据）")
            return

        # 按真实 TTFB 重排：速度排序只看吞吐，看不见 TLS 握手和回源的开销。
        # 实测 104.18.36.99（新加坡）TCP RTT 84ms 全场最低、速度 7.29MB/s 也过线，
        # 但 TTFB 1031~1227ms，比洛杉矶那批（613~943ms）慢一倍。纯按速度排会选中它。
        if usable_ips:
            probe_n = min(len(usable_ips), max(upload_count * 3, 20))
            print(f"\n⏱️  正在测量 TTFB + 落地机房（{probe_n} 个 × 2 轮，单次请求）...")
            import concurrent.futures as _cf
            probe_list = usable_ips[:probe_n]
            # 并发 16：这一步全是网络 IO 等待，不消耗本地带宽（每次只取几 KB），
            # 提高并发只压缩排队时间。TTFB 与机房合并到同一次请求后，
            # 每轮探测从"两次往返"变成"一次往返"。
            with _cf.ThreadPoolExecutor(max_workers=16) as _ex:
                probes = list(_ex.map(
                    lambda x: measure_ttfb_and_colo(x['ip'], x['port'], rounds=2),
                    probe_list))
            ttfb_map = {info['ip']: p[0] for info, p in zip(probe_list, probes)}
            colo_map = {info['ip']: p[1] for info, p in zip(probe_list, probes)}
            ranked = []
            for info in usable_ips:
                t = ttfb_map.get(info['ip'])
                info['ttfb'] = t
                info['colo'] = colo_map.get(info['ip'])
                # TTFB 失败的排到最后，同延迟下速度快的优先
                ranked.append((t if t is not None else 1e9, -info['speed'], info))
            ranked.sort(key=lambda x: (x[0], x[1]))
            usable_ips = [x[2] for x in ranked]
        stage_timer("上传:TTFB+机房探测")

        # 故障域收敛：同一个机房最多 per_colo 个（凑不满会自动放宽），再叠 /24 去重
        diverse_ips = select_by_colo(usable_ips, upload_count, per_colo=2)
        colo_summary = {}
        for info in diverse_ips:
            c = info.get('colo') or '未探测到'
            colo_summary[c] = colo_summary.get(c, 0) + 1
        print(f"   机房分布: " + "、".join(f"{k}×{v}" for k, v in colo_summary.items()))
        for info in diverse_ips:
            t = info.get('ttfb')
            ttfb_s = ('%.0fms' % t) if t else '失败'
            print(f"     {info['ip']:<16}{info['speed']:6.2f} MB/s  "
                  f"TTFB {ttfb_s:>7}  {info.get('colo') or '?':<4} {info['region_name']}")
        record_run_stats(candidates=len(best_ips), verified_ok=len(usable_ips),
                         diverse=len(diverse_ips))

        best_ips = diverse_ips
        upload_count = min(upload_count, len(best_ips))
        print(f"✅ {len(usable_ips)} 个 IP 通过验证，去重后上传前 {upload_count} 个")
        stage_timer("上传:机房分散选择")

        # 如果需要清空，先执行清空操作（在确认有数据可以上报之后）
        if should_clear:
            print("\n🗑️  正在清空现有数据...")
            try:
                try:
                    delete_response = requests.delete(
                        api_url,
                        json={"all": True},
                        headers={"Content-Type": "application/json"},
                        timeout=10
                    )
                except ImportError as e:
                    # SSL模块不可用，静默切换到curl
                    if "SSL module is not available" in str(e):
                        delete_response = curl_request(
                            api_url,
                            method='DELETE',
                            data={"all": True},
                            headers={"Content-Type": "application/json"},
                            timeout=10
                        )
                    else:
                        raise
                
                if delete_response.status_code == 200:
                    print("✅ 现有数据已清空")
                else:
                    print(f"⚠️  清空失败 (HTTP {delete_response.status_code})，继续尝试添加...")
            except Exception as e:
                print(f"⚠️  清空操作失败: {e}，继续尝试添加...")
        
        # 构建批量上报数据
        print("\n🚀 开始批量上报优选IP...")
        batch_data = []
        for ip_info in best_ips[:upload_count]:
            # 构建节点名称：地区名-速度MB/s
            region_name = ip_info.get('region_name', '未知地区')
            speed = ip_info['speed']
            name = build_node_name(region_name, speed)
            
            batch_data.append({
                "ip": ip_info['ip'],
                "port": ip_info['port'],
                "name": name
            })
        
        # 发送批量POST请求
        try:
            try:
                response = requests.post(
                    api_url,
                    json=batch_data,
                    headers={"Content-Type": "application/json"},
                    timeout=30
                )
            except ImportError as e:
                # SSL模块不可用，静默切换到curl备用方案
                if "SSL module is not available" in str(e):
                    response = curl_request(
                        api_url,
                        method='POST',
                        data=batch_data,
                        headers={"Content-Type": "application/json"},
                        timeout=30
                    )
                else:
                    raise
            
            # 处理响应
            if response and response.status_code == 200:
                result = response.json()
                if result.get('success'):
                    success_count = result.get('added', 0)
                    fail_count = result.get('failed', 0)
                    skipped_count = result.get('skipped', 0)
                    
                    print("\n" + "=" * 70)
                    print(" ✅ 批量上报完成！")
                    print("=" * 70)
                    print(f"  ✅ 成功添加: {success_count} 个")
                    if skipped_count > 0:
                        print(f"  ⚠️  跳过重复: {skipped_count} 个")
                    if fail_count > 0:
                        print(f"  ❌ 失败: {fail_count} 个")
                    print(f"  📊 总计: {upload_count} 个")
                    print("=" * 70)
                else:
                    print(f"❌ 批量上报失败: {result.get('error', '未知错误')}")
            elif response and response.status_code == 403:
                print(f"❌ 认证失败！请检查：")
                print(f"   1. UUID或者路径是否正确")
                print(f"   2. 是否在配置页面开启了 'API管理' 功能")
            elif response:
                print(f"❌ 批量上报失败 (HTTP {response.status_code})")
                try:
                    error_detail = response.json()
                    print(f"   错误详情: {error_detail.get('error', '无详情')}")
                except:
                    pass
                
        except requests.exceptions.Timeout:
            print(f"❌ 请求超时，请检查网络连接")
        except requests.exceptions.RequestException as e:
            print(f"❌ 网络错误: {e}")
        except Exception as e:
            print(f"❌ 请求失败: {e}")
        stage_timer("上传:清空+批量写入完成")
        
    except Exception as e:
        print(f"❌ 读取测速结果失败: {e}")
        import traceback
        traceback.print_exc()


def upload_to_github_cli(result_file="result.csv", repo_info=None, github_token=None, file_path="cloudflare_ips.txt", upload_count=10):
    """命令行模式：上传优选结果到 GitHub 公开仓库"""
    print("\n" + "=" * 70)
    print(" 命令行模式：GitHub 仓库上传")
    print("=" * 70)
    
    # 检查结果文件是否存在
    if not os.path.exists(result_file):
        print(f"❌ 未找到测速结果文件: {result_file}")
        return
    
    # 解析仓库信息
    if not repo_info or '/' not in repo_info:
        print("❌ 仓库格式不正确，应为 owner/repo")
        return
    
    repo_parts = repo_info.split('/', 1)
    owner = repo_parts[0]
    repo = repo_parts[1]
    
    # 读取测速结果
    print("\n📊 正在读取测速结果...")
    try:
        best_ips = []
        with open(result_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # 安全获取数据，避免NoneType错误
                ip = (row.get('IP 地址') or '').strip()
                port = (row.get('端口') or '').strip()
                
                # 尝试多种可能的列名来获取速度
                speed = ''
                for speed_key in ['下载速度(MB/s)', '下载速度 (MB/s)', '下载速度']:
                    if speed_key in row and row[speed_key] is not None:
                        speed = str(row[speed_key]).strip()
                        break
                
                # 尝试多种可能的列名来获取延迟
                latency = ''
                for latency_key in ['平均延迟', '延迟', 'latency']:
                    if latency_key in row and row[latency_key] is not None:
                        latency = str(row[latency_key]).strip()
                        break
                
                # 获取地区码
                region_code = (row.get('地区码') or '').strip()
                
                # 如果IP地址中包含端口信息
                if ip and ':' in ip:
                    ip_parts = ip.split(':')
                    if len(ip_parts) == 2:
                        ip = ip_parts[0]
                        if not port:
                            port = ip_parts[1]
                
                # 设置默认端口
                if not port:
                    port = '443'
                
                if ip:
                    try:
                        speed_val = float(speed) if speed else 0
                        latency_val = latency if latency else 'N/A'
                        
                        # 获取地区中文名称
                        region_name = '未知地区'
                        if region_code and region_code in AIRPORT_CODES:
                            region_name = AIRPORT_CODES[region_code].get('name', region_code)
                        elif region_code:
                            region_name = region_code
                        
                        best_ips.append({
                            'ip': ip,
                            'port': int(port),
                            'speed': speed_val,
                            'latency': latency_val,
                            'region_code': region_code,
                            'region_name': region_name
                        })
                    except ValueError:
                        continue
        
        if not best_ips:
            print("❌ 未找到有效的测速结果")
            return
        
        # 限制上传数量
        upload_count = min(upload_count, len(best_ips))
        print(f"✅ 找到 {len(best_ips)} 个测速结果，将上传前 {upload_count} 个")
        
        # 格式化数据为换行符分隔的格式（包含注释，和Cloudflare Workers API一样）
        print("\n🚀 开始上传到 GitHub 仓库...")
        content_lines = []
        for ip_info in best_ips[:upload_count]:
            # 构建节点名称：地区名-速度MB/s（和Cloudflare Workers API一样）
            region_name = ip_info.get('region_name', '未知地区')
            speed = ip_info['speed']
            name = build_node_name(region_name, speed)
            # 格式：IP:端口#地区名-速度MB/s（井号前后无空格）
            content_lines.append(f"{ip_info['ip']}:{ip_info['port']}#{name}")
        
        # 使用换行符连接所有行
        content = '\n'.join(content_lines)
        
        # 检查文件是否已存在
        print(f"\n🔍 正在检查文件是否存在...")
        file_sha = None
        try:
            try:
                check_response = requests.get(
                    f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}",
                    headers={
                        "Authorization": f"token {github_token}",
                        "Accept": "application/vnd.github.v3+json"
                    },
                    timeout=10
                )
                if check_response.status_code == 200:
                    file_data = check_response.json()
                    file_sha = file_data.get('sha', '')
                    print(f"⚠️  文件已存在，将更新文件")
                elif check_response.status_code == 404:
                    print(f"✅ 文件不存在，将创建新文件")
                else:
                    print(f"⚠️  无法检查文件状态，将尝试创建/更新")
            except ImportError as e:
                # SSL模块不可用，静默切换到curl
                if "SSL module is not available" in str(e):
                    check_response = curl_request(
                        f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}",
                        method='GET',
                        headers={
                            "Authorization": f"token {github_token}",
                            "Accept": "application/vnd.github.v3+json"
                        },
                        timeout=10
                    )
                    if check_response.status_code == 200:
                        file_data = check_response.json()
                        file_sha = file_data.get('sha', '')
                        print(f"⚠️  文件已存在，将更新文件")
                    elif check_response.status_code == 404:
                        print(f"✅ 文件不存在，将创建新文件")
                    else:
                        print(f"⚠️  无法检查文件状态，将尝试创建/更新")
                else:
                    raise
            except (requests.exceptions.ConnectionError, requests.exceptions.RequestException) as e:
                # 网络连接错误，尝试使用curl备用方案
                error_str = str(e)
                if "Can't assign requested address" in error_str or "Failed to establish" in error_str or "Max retries exceeded" in error_str:
                    print(f"⚠️  检测到网络连接问题，尝试使用curl备用方案...")
                    try:
                        check_response = curl_request(
                            f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}",
                            method='GET',
                            headers={
                                "Authorization": f"token {github_token}",
                                "Accept": "application/vnd.github.v3+json"
                            },
                            timeout=10
                        )
                        if check_response.status_code == 200:
                            file_data = check_response.json()
                            file_sha = file_data.get('sha', '')
                            print(f"⚠️  文件已存在，将更新文件")
                        elif check_response.status_code == 404:
                            print(f"✅ 文件不存在，将创建新文件")
                        else:
                            print(f"⚠️  无法检查文件状态，将尝试创建/更新")
                    except Exception as curl_e:
                        print(f"⚠️  curl备用方案也失败: {curl_e}，将尝试创建/更新")
                else:
                    raise
        except Exception as e:
            print(f"⚠️  检查文件状态失败: {e}，将尝试创建/更新")
        
        # 准备上传数据
        import base64
        content_bytes = content.encode('utf-8')
        content_base64 = base64.b64encode(content_bytes).decode('utf-8')
        
        upload_data = {
            "message": f"更新Cloudflare优选IP列表 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "content": content_base64
        }
        
        # 如果文件已存在，需要提供sha
        if file_sha:
            upload_data["sha"] = file_sha
        
        # 上传到 GitHub 仓库
        try:
            try:
                response = requests.put(
                    f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}",
                    json=upload_data,
                    headers={
                        "Authorization": f"token {github_token}",
                        "Accept": "application/vnd.github.v3+json"
                    },
                    timeout=30
                )
            except ImportError as e:
                # SSL模块不可用，静默切换到curl
                if "SSL module is not available" in str(e):
                    response = curl_request(
                        f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}",
                        method='PUT',
                        data=upload_data,
                        headers={
                            "Authorization": f"token {github_token}",
                            "Accept": "application/vnd.github.v3+json"
                        },
                        timeout=30
                    )
                else:
                    raise
            except (requests.exceptions.ConnectionError, requests.exceptions.RequestException) as e:
                # 网络连接错误，尝试使用curl备用方案
                error_str = str(e)
                if "Can't assign requested address" in error_str or "Failed to establish" in error_str or "Max retries exceeded" in error_str:
                    print(f"⚠️  检测到网络连接问题，尝试使用curl备用方案...")
                    try:
                        response = curl_request(
                            f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}",
                            method='PUT',
                            data=upload_data,
                            headers={
                                "Authorization": f"token {github_token}",
                                "Accept": "application/vnd.github.v3+json"
                            },
                            timeout=30
                        )
                    except Exception as curl_e:
                        print(f"❌ curl备用方案也失败: {curl_e}")
                        raise
                else:
                    raise
            
            if response and response.status_code in [200, 201]:
                result = response.json()
                file_url = result.get('content', {}).get('html_url', '')
                
                # 尝试获取默认分支
                default_branch = "main"  # 默认使用main分支
                try:
                    try:
                        repo_response = requests.get(
                            f"https://api.github.com/repos/{owner}/{repo}",
                            headers={
                                "Authorization": f"token {github_token}",
                                "Accept": "application/vnd.github.v3+json"
                            },
                            timeout=10
                        )
                        if repo_response.status_code == 200:
                            repo_data = repo_response.json()
                            default_branch = repo_data.get('default_branch', 'main')
                    except:
                        pass
                except:
                    pass
                
                raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{default_branch}/{file_path}"
                
                print("\n" + "=" * 70)
                print(" ✅ 上传成功！")
                print("=" * 70)
                print(f"  仓库地址: https://github.com/{owner}/{repo}")
                if file_url:
                    print(f"  文件地址: {file_url}")
                print(f"  原始文件地址: {raw_url}")
                print(f"  上传数量: {upload_count} 个IP")
                print("=" * 70)
            elif response and response.status_code == 401:
                print(f"❌ 认证失败！请检查：")
                print(f"   1. GitHub Token 是否正确")
                print(f"   2. Token 是否具有 repo 权限")
            elif response and response.status_code == 404:
                print(f"❌ 仓库不存在或无权限！请检查：")
                print(f"   1. 仓库路径是否正确: {owner}/{repo}")
                print(f"   2. Token 是否有该仓库的写入权限")
            elif response:
                print(f"❌ 上传失败 (HTTP {response.status_code})")
                try:
                    error_detail = response.json()
                    print(f"   错误详情: {error_detail.get('message', '无详情')}")
                except:
                    pass
        except requests.exceptions.Timeout:
            print(f"❌ 请求超时，请检查网络连接")
        except requests.exceptions.RequestException as e:
            print(f"❌ 网络错误: {e}")
        except Exception as e:
            print(f"❌ 上传失败: {e}")
        
    except Exception as e:
        print(f"❌ 读取测速结果失败: {e}")
        import traceback
        traceback.print_exc()


def detect_available_regions():
    """检测可用地区"""
    # 检查是否已有检测结果文件
    if os.path.exists("region_scan.csv"):
        print("发现已有的地区扫描结果文件")
        choice = input("是否需要重新扫描？[y/N]: ").strip().lower()
        if choice != 'y':
            print("使用已有检测结果...")
            # 直接读取已有文件
            available_regions = []
            region_counts = {}
            
            with open("region_scan.csv", 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    colo = (row.get('地区码') or '').strip()
                    if colo and colo != 'N/A':
                        region_counts[colo] = region_counts.get(colo, 0) + 1
            
            # 构建地区列表（按IP数量排序）
            for colo, count in sorted(region_counts.items(), key=lambda x: x[1], reverse=True):
                region_name = "未知地区"
                for code, info in AIRPORT_CODES.items():
                    if code == colo:
                        region_name = f"{info.get('name', '')} ({info.get('country', '')})"
                        break
                available_regions.append((colo, region_name, count))
            
            return available_regions
    
    print("正在检测各地区可用性...")
    
    # 获取系统信息
    os_type, arch_type = get_system_info()
    exec_name = download_cloudflare_speedtest(os_type, arch_type)
    
    # 构建检测命令 - 使用HTTPing模式快速检测
    if sys.platform == "win32":
        cmd = [exec_name]
    else:
        cmd = [f"./{exec_name}"]
    
    cmd.extend([
        "-dd",  # 禁用下载测速，只做延迟测试
        "-tl", "9999",  # 高延迟阈值
        "-f", CLOUDFLARE_IP_FILE,
        "-httping",  # 使用HTTPing模式获取地区码
        "-url", "https://jhb.ovh",
        "-o", "region_scan.csv"  # 输出到地区扫描文件
    ])
    
    try:
        print("运行地区检测...")
        print("正在扫描所有地区，请稍候（约需1-2分钟）...")
        print("=" * 50)
        
        # 直接运行命令，显示完整输出
        result = subprocess.run(cmd, timeout=120, encoding='utf-8', errors='replace')
        
        if result.returncode == 0 and os.path.exists("region_scan.csv"):
            # 读取检测结果
            available_regions = []
            region_counts = {}  # 统计每个地区的IP数量
            
            with open("region_scan.csv", 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    colo = (row.get('地区码') or '').strip()
                    if colo and colo != 'N/A':
                        # 统计IP数量
                        if colo not in region_counts:
                            region_counts[colo] = 0
                        region_counts[colo] += 1
            
            # 构建地区列表（按IP数量排序）
            for colo, count in sorted(region_counts.items(), key=lambda x: x[1], reverse=True):
                # 查找地区名称
                region_name = "未知地区"
                for code, info in AIRPORT_CODES.items():
                    if code == colo:
                        region_name = f"{info.get('name', '')} ({info.get('country', '')})"
                        break
                available_regions.append((colo, region_name, count))
            
            # 保留地区扫描结果文件，不删除
            print("地区扫描结果已保存到 region_scan.csv")
            
            return available_regions
        else:
            print("地区检测失败，使用默认地区列表")
            # 返回默认的主要地区
            default_regions = [
                ('HKG', '香港 (中国)', 0),
                ('SIN', '新加坡 (新加坡)', 0),
                ('NRT', '东京 (日本)', 0),
                ('ICN', '首尔 (韩国)', 0),
                ('LAX', '洛杉矶 (美国)', 0),
                ('FRA', '法兰克福 (德国)', 0),
                ('LHR', '伦敦 (英国)', 0)
            ]
            return default_regions
            
    except Exception as e:
        print(f"地区检测出错: {e}")
        # 返回默认地区
        default_regions = [
            ('HKG', '香港 (中国)', 0),
            ('SIN', '新加坡 (新加坡)', 0),
            ('NRT', '东京 (日本)', 0),
            ('ICN', '首尔 (韩国)', 0),
            ('LAX', '洛杉矶 (美国)', 0),
            ('FRA', '法兰克福 (德国)', 0),
            ('LHR', '伦敦 (英国)', 0)
        ]
        return default_regions

if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n\n用户取消操作")
        # Windows 系统添加暂停，避免窗口立即关闭
        if sys.platform == "win32":
            print("\n" + "=" * 60)
            input("按 Enter 键退出...")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 程序运行出错: {e}")
        print(f"   建议：")
        print(f"   1. 检查网络连接")
        print(f"   2. 确保有足够的磁盘空间")
        print(f"   3. 检查Python环境是否正常")
        print(f"   4. 如果问题持续，请联系技术支持")
        # Windows 系统添加暂停，避免窗口立即关闭
        if sys.platform == "win32":
            print("\n" + "=" * 60)
            input("按 Enter 键退出...")
        sys.exit(1)
