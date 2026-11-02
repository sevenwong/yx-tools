#!/bin/bash
# 从 ~/.crontab.final（完整表）还原 crontab
#
# 背景：2026-10-19 01:00~01:25 做对照实验时，每次都用 `crontab /tmp/ct_xxx`
# （整表替换）安装实验用的单行任务，导致原表里的 3 个任务全部丢失：
#   1) 0 21 * * *   aria2 BT trackers 每日更新
#   2) 40 22 * * 5  Cloudflare 优选 IP 测速
#   3) 23 4 * * *   日志清理（删除 30 天前日志）
#
# 本脚本只做还原，不改任何参数。
# 用法：bash ~/GitProjects/yx-tools/apply_cron.sh

set -u

SRC="/Users/wangqiang/.crontab.final"
BAK="/tmp/crontab.before-restore.$(date +%Y%m%d-%H%M%S)"

if [ ! -f "${SRC}" ]; then
    echo "找不到 ${SRC} ，中止"
    exit 1
fi

echo "=== 即将安装的任务行（来自 ${SRC} ）==="
grep -vE "^\s*#|^\s*$" "${SRC}" | nl -ba
echo ""
echo "当前表会备份到 ${BAK} "
echo "接下来弹一次管理员密码框，输入后即安装；取消则什么都不改。"
echo ""

osascript <<APPLESCRIPT
do shell script "cp /var/at/tabs/wangqiang ${BAK} 2>/dev/null; sudo -u wangqiang crontab ${SRC}; launchctl kickstart -k system/com.vix.cron 2>/dev/null; echo '=== 安装后回读 ==='; crontab -l -u wangqiang" with administrator privileges
APPLESCRIPT

echo ""
echo "================================"
echo "回滚：sudo -u wangqiang crontab ${BAK} "
