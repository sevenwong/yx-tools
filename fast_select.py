#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
并发下载测速选优（替代 CloudflareST 的串行下载测速，用于把整条流程压进 5 分钟）

与 cloudflare_speedtest.py 的关系：
  候选池、WS 握手验证、TTFB+机房探测、机房分散选择、上传函数全部直接复用
  cloudflare_speedtest.py 的实现，只有「下载测速」这一段换成了 curl 并发。

流程：
  1) 复用候选池：采样 -> TCP 探活 -> 按 RTT 筛选截断
  2) 第一轮并发测速：对全部候选并发下载，每个下满 --dur 秒（默认 2 秒）——这轮只排名
  3) 决赛圈串行复测：取并发最快的前 --pick×3 名，逐个单独下满 --serial-dur 秒
     （默认 5 秒）——这轮才是真实单流速度，用于判定速度门槛
  4) 速度门槛过滤（--min-speed，默认 6MB/s；注意门槛值需按新测法重新标定）
  5) TTFB + 落地机房探测：速度排序看不见 TLS/回源开销，按 TTFB 重排
  6) 机房分散选择：同一 colo 最多 2 个 + /24 去重，按 --pick 的 3 倍留候选
  7) 写出 result.csv（列名与 CloudflareST 一致，行数是候选数不是 10）+ 可选上传
  8) WS 握手验证（能否真当代理，CF 对部分 IP 段返回 1034）不在这里做，
     由上传函数 upload_to_cloudflare_api_cli 内部完成——它本来就会做，两边
     都做等于付两遍（实测 21s + 20.5s）。

测速按「固定时长」而不是「固定字节」：固定 10MB 时，快链路上 0.4 秒就下完，
全程都在 TCP 慢启动里，速度被压低一半以上，且链路越快压得越狠。固定 5 秒能把
慢启动那 1.7 秒摊成固定占比，快慢 IP 之间才可比（2026-11-02 实测同一 IP：
下 10MB 报 7.5、满 3 秒 17.6、满 5 秒 32.1、满 10 秒 37.5）。

硬性保证：--deadline 秒到点后不再派发新的测速任务；串行复测另有
--serial-budget 秒的额外预算，耗尽时保留全部候选（未复测的降级为替补，
不参与门槛判定），不会像旧版那样把 30 个砍到 10 个。

用法：
  python3 fast_select.py --top 200 --conc 8 --deadline 240
  python3 fast_select.py --top 200 --conc 8 --deadline 240 --upload api --clear \
      --worker-domain by.linkup6.xyz --uuid node
"""
import argparse
import concurrent.futures as cf
import csv
import subprocess
import sys
import time

import cloudflare_speedtest as m

DL_HOST = "speed.cloudflare.com"
DL_URL = f"https://{DL_HOST}/__down?bytes={{bytes}}"


def _run(cmd, timeout):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 3)
        return p.stdout.strip(), p.returncode
    except Exception:
        return "", -1


def measure_speed(ip, dur, total_bytes):
    """按固定时长下载测速，返回 (速度 MB/s, HTTP 状态码, 收到字节数)

    速度 = 实际收到字节 ÷ 实际耗时。公式与"固定字节"的旧测法相同，区别只在于
    哪个量固定：固定 10MB 时耗时随链路浮动，快链路 0.4 秒就下完，全程都在 TCP
    慢启动里，算出来的平均速度被压低一半以上；固定时长时字节随链路浮动，慢启动
    那 1.7 秒（RTT 200ms × 约 8 个往返）占固定时长的比例稳定，快慢 IP 之间才可比。

    2026-11-02 同一 IP、同一分钟实测（分钟级差异，非精确对照）：
        下 10MB 报 7.5MB/s ｜ 满 3 秒 17.6 ｜ 满 5 秒 32.1 ｜ 满 10 秒 37.5
    时长越短越偏低，5 秒是拐点（8 秒只多 7%，却多花 60% 时间）。

    ⚠️ 必须带 Referer: https://speed.cloudflare.com/ 这个头。裸 curl 请求 /__down
    会直接返回 429（响应体只有 1 字节），测出来的"速度"是假的；加上 Referer
    立刻 200 并正常出流量。CloudflareST 自带合规请求头，所以只有自己用 curl
    重写测速时才会撞上这个坑。
    """
    out, _rc = _run([
        "curl", "-s", "-o", "/dev/null", "--noproxy", "*",
        "--max-time", f"{dur:g}",
        "--resolve", f"{DL_HOST}:443:{ip}",
        "-H", f"Referer: https://{DL_HOST}/",
        "-w", "%{http_code} %{size_download} %{time_total}",
        DL_URL.format(bytes=total_bytes),
    ], dur)
    parts = out.split()
    if len(parts) != 3:
        return 0.0, "err", 0
    code, raw_size, raw_time = parts
    try:
        size = float(raw_size)
        elapsed = float(raw_time)
    except Exception:
        return 0.0, "err", 0
    # 被 --max-time 截断时 curl 退出码是 28，这是预期行为，不能当失败
    if code != "200":
        return 0.0, code, int(size)
    if elapsed <= 0:
        return 0.0, "err", int(size)
    return max(0.0, size / elapsed / 1024 / 1024), code, int(size)


def run_batch(ips, conc, dur, total_bytes, deadline):
    """并发按固定时长测一批，返回 ({ip: speed}, {状态码: 个数})；到 deadline 停止派发新任务"""
    result = {}
    codes = {}
    if not ips:
        return result, codes
    with cf.ThreadPoolExecutor(max_workers=conc) as ex:
        futs = {}
        it = iter(ips)
        # 先填满并发窗口
        for ip in it:
            if time.time() > deadline:
                break
            futs[ex.submit(measure_speed, ip, dur, total_bytes)] = ip
            if len(futs) >= conc:
                break
        while futs:
            done, _ = cf.wait(futs, return_when=cf.FIRST_COMPLETED)
            for fu in done:
                ip = futs.pop(fu)
                try:
                    sp, code, _b = fu.result()
                except Exception:
                    sp, code = 0.0, "err"
                result[ip] = sp
                codes[code] = codes.get(code, 0) + 1
                if time.time() <= deadline:
                    for nxt in it:
                        futs[ex.submit(measure_speed, nxt, dur, total_bytes)] = nxt
                        break
    return result, codes


# 注：WS 握手验证（能否真当代理，CF 对部分 IP 段返回 1034）不在这里做。
# upload_to_cloudflare_api_cli 内部本来就会对 result.csv 里的 IP 做一遍（并发 8，
# 每个 IP 必挂满 --max-time 约 10 秒），两边都做等于付两遍。2026-11-02 实测：
# 本脚本 21s + 上传函数 20.5s，同一批 IP 验了两遍。


def probe_ttfb(infos, limit, workers=16):
    """并发测 TTFB + 落地机房，回填 ttfb/colo，失败排最后"""
    head = infos[:limit]
    if not head:
        return infos
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        probes = list(ex.map(
            lambda x: m.measure_ttfb_and_colo(x["ip"], x["port"], rounds=2), head))
    tmap = {info["ip"]: p for info, p in zip(head, probes)}
    for info in infos:
        t, colo = tmap.get(info["ip"], (None, None))
        info["ttfb"] = t
        info["colo"] = colo or info.get("colo")

    def key(x):
        # TTFB 探测失败的排最后；同 TTFB 下速度快的优先
        return (x["ttfb"] if x.get("ttfb") is not None else 1e9, -x["speed"])

    return sorted(infos, key=key)


def main():
    ap = argparse.ArgumentParser(
        description="并发下载测速 + WS握手 + TTFB/机房分散，产出与 CloudflareST 同列名的 result.csv")
    ap.add_argument("--top", type=int, default=200, help="按 RTT 排序后保留的候选数")
    ap.add_argument("--max-rtt", type=float, default=0,
                    help="RTT 硬门槛（毫秒）：超过该值的 IP 不进下载测速；0=不限制")
    ap.add_argument("--conc", type=int, default=8, help="并发数")
    ap.add_argument("--dur", type=float, default=2.0,
                    help="第一轮并发测速每个 IP 的下载时长（秒）；这轮只用于排名，可短")
    ap.add_argument("--serial-dur", type=float, default=5.0,
                    help="串行复测每个 IP 的下载时长（秒）；这轮用于判定门槛，需跨过 TCP 慢启动")
    ap.add_argument("--bytes", type=int, default=400_000_000,
                    help="测速请求的字节数，需大于「时长×带宽」以保证被时长截断而不是下完")
    ap.add_argument("--deadline", type=int, default=240,
                    help="从开始测速算起的硬性上限（秒），到点收尾")
    ap.add_argument("--min-speed", type=float, default=6.0,
                    help="下载速度门槛 MB/s。注意：换成固定时长测法后，同一个 IP 的数字"
                         "会是旧测法（固定 10MB）的 2~3 倍，门槛需按新测法重新标定；0=不设门槛")
    ap.add_argument("--pick", type=int, default=10, help="最终写出/上传的 IP 数")
    ap.add_argument("--min-pick", type=int, default=5,
                    help="通过验证的 IP 少于此数就直接放弃本次上传（不清空现有池）")
    ap.add_argument("--serial-recheck", action="store_true", default=True,
                    help="对进入决赛圈的候选做一次串行复测，取真实单流速度（默认开）")
    ap.add_argument("--no-serial-recheck", dest="serial_recheck", action="store_false",
                    help="关闭串行复测，直接用并发速度值判定门槛")
    ap.add_argument("--serial-budget", type=int, default=180,
                    help="串行复测允许超出 --deadline 的额外秒数（30 个 × 5 秒 = 150 秒）")
    ap.add_argument("--per-colo", type=int, default=2, help="同一机房最多保留几个")
    ap.add_argument("--out", default="result.csv")
    ap.add_argument("--upload", default="none", choices=["none", "api"])
    ap.add_argument("--worker-domain", default=None)
    ap.add_argument("--uuid", default=None)
    ap.add_argument("--clear", action="store_true")
    args = ap.parse_args()

    t0 = time.time()

    # ---------- 1) 候选池（完全复用生产实现） ----------
    m.POOL_TOP_N = max(0, args.top)
    m.POOL_MAX_RTT = max(0.0, args.max_rtt)
    if not m.build_ws_verified_ip_list():
        print("❌ 候选池构建失败")
        return 1
    with open(m.CLOUDFLARE_IP_FILE) as f:
        cands = [ln.strip() for ln in f if ln.strip()]
    print(f"\n候选 {len(cands)} 个，池构建 {time.time() - t0:.0f}s")

    # ---------- 2) 第一轮并发测速：只为排名，每个 IP 下满 --dur 秒 ----------
    t1 = time.time()
    deadline = t1 + args.deadline
    speeds, codes = run_batch(cands, args.conc, args.dur, args.bytes, deadline)
    print(f"第一轮 {len(speeds)}/{len(cands)} 个出结果，{time.time() - t1:.0f}s"
          f"（每个 {args.dur:g} 秒，状态码 {codes}）")
    if codes.get("429"):
        print("⚠️  出现 429：请求头被拒（缺 Referer）或端点限流，本轮速度不可信")

    # 注：这里不再做"第二轮并发复测取最大值"。它测的前 N 名必然落在下面决赛圈里，
    # 而串行复测（真实单流）会把并发值整体覆盖，所以那一轮是纯浪费（实测 31 秒）。
    best = dict(speeds)

    # ---------- 3) 决赛圈串行复测：并发值只用来排序，门槛必须看真实单流速度 ----------
    # 并发 N 路共享同一条出口带宽，每个 IP 只分到 1/N，绝对值不可比；用并发值直接
    # 卡门槛会成批误杀。所以只对进入决赛圈的候选付一次串行代价，每个下满 --serial-dur 秒。
    pool_size = max(args.pick * 3, 20)
    head = sorted(best.items(), key=lambda kv: -kv[1])[:pool_size]
    head = [(ip, sp) for ip, sp in head if sp > 0]
    if not head:
        print("❌ 第一轮全部测不出速度")
        return 1

    measured = set()
    if args.serial_recheck:
        print(f"\n🔁 串行复测前 {len(head)} 名（每个 {args.serial_dur:g} 秒，"
              f"并发值仅用于排序，真实速度以串行为准）...")
        t15 = time.time()
        budget_end = deadline + args.serial_budget
        for i, (ip, _) in enumerate(head):
            if time.time() > budget_end:
                # 预算到点：保留全部候选，未复测的降级为"替补"（只用并发值），
                # 不参与门槛判定，但照样写进 CSV 供 WS 握手被拒时补位。
                print(f"   预算用完，第 {i + 1}~{len(head)} 个未做串行复测，"
                      f"保留为替补（并发值不参与门槛判定）")
                break
            real, _code, _b = measure_speed(ip, args.serial_dur, args.bytes)
            if real > 0:
                best[ip] = real
                measured.add(ip)
        print(f"✅ 串行复测完成 {len(measured)} 个，{time.time() - t15:.0f}s")
        head = sorted([(ip, best[ip]) for ip, _ in head], key=lambda kv: -kv[1])

    thr = max(0.0, args.min_speed)
    if args.serial_recheck:
        passed = [(ip, sp) for ip, sp in head if ip in measured and sp >= thr]
        # 未串行复测的只当替补，不判门槛（并发值偏低，判了必然全灭）
        backup = [(ip, sp) for ip, sp in head if ip not in measured]
    else:
        passed = [(ip, sp) for ip, sp in head if sp >= thr]
        backup = []
    print(f"测速总耗时 {time.time() - t1:.0f}s，候选 {len(head)} 个，"
          f"过 {thr:g}MB/s 门槛 {len(passed)} 个"
          + (f"（另有 {len(backup)} 个替补未复测）" if backup else ""))
    if not passed:
        print(f"❌ 没有 IP 达到速度门槛，最高的几个是：")
        for ip, sp in head[:5]:
            tag = "" if ip in measured or not args.serial_recheck else "（并发值，偏低）"
            print(f"     {ip:<16}{sp:6.2f} MB/s  RTT {m.POOL_RTT_MAP.get(ip, 0):.0f}ms{tag}")
        print("   （先看链路本身带宽，其次看 --top 截断是否把低带宽 IP 挑了出来；"
              "门槛是按固定时长测法标的，换测法后需重新标定）")
        return 1
    if len(passed) < args.pick:
        print(f"⚠️  过门槛的只有 {len(passed)} 个，少于目标 {args.pick} 个")

    infos = []
    for ip, sp in passed + backup:
        infos.append({
            "ip": ip,
            "port": 443,
            "speed": sp,
            "rtt": m.POOL_RTT_MAP.get(ip, 0),
            "ttfb": None,
            "colo": None,
            "region_name": "未知地区",
        })

    # ---------- 5) TTFB + 落地机房，按 TTFB 重排（决定 延迟列 与 地区码 列） ----------
    # 候选按 --pick 的 3 倍准备：上传函数会对 result.csv 里的每个 IP 做 WS 握手，
    # 被 1034 拒掉几个就少几个。只给 10 个时没有替补（生产脚本靠 -dn 20 天然留了
    # 20 个候选），给 30 个才有得补。
    cand_n = max(args.pick * 3, args.pick)
    probe_limit = min(len(infos), max(cand_n, 20))
    print(f"\n⏱️  正在测量 TTFB + 落地机房（前 {probe_limit} 个 × 2 轮）...")
    t3 = time.time()
    ranked = probe_ttfb(infos, probe_limit)
    print(f"✅ TTFB 探测完成，{time.time() - t3:.0f}s")

    # ---------- 6) 机房分散 + /24 去重（选出交给上传函数的候选，不是最终那 10 个） ----------
    cand = m.select_by_colo(ranked, cand_n, per_colo=args.per_colo)
    colo_summary = {}
    for info in cand:
        c = info.get("colo") or "未探测到"
        colo_summary[c] = colo_summary.get(c, 0) + 1
        info["region_name"] = m.AIRPORT_CODES.get(c, {}).get("name", c) if c else "未知地区"
    print(f"   候选 {len(cand)} 个，机房分布: "
          + "、".join(f"{k}×{v}" for k, v in colo_summary.items()))
    for info in cand[:args.pick]:
        ttfb_s = f"{info['ttfb']:.0f}ms" if info.get("ttfb") else "失败"
        print(f"     {info['ip']:<16}{info['speed']:6.2f} MB/s  "
              f"TTFB {ttfb_s:>7}  {(info.get('colo') or '?'):<4} {info['region_name']}")
    if len(cand) > args.pick:
        print(f"     ... 另 {len(cand) - args.pick} 个作为 WS 被拒时的替补")

    if len(cand) < args.pick:
        print(f"⚠️  只选出 {len(cand)} 个候选（目标 {args.pick} 个），"
              f"可能是候选不足或 RTT 门槛过严")

    # 第一道闸（粗筛）：候选数本身就不够时，连 CSV 都不用写、也不必去请求线上池。
    # 真正严格的判断在 WS 握手之后——那个只能放在上传函数里（见下方 min_usable），
    # 因为 WS 握手在那里做。过少的 IP 一旦配合 --clear 上传，会把线上池从 10 个
    # 缩到只剩这几个，比"本次不更新"糟糕得多，两道闸都是宁可整次放弃。
    if args.upload == "api" and len(cand) < args.min_pick:
        print(f"❌ 只有 {len(cand)} 个候选，少于 --min-pick {args.min_pick}，"
              f"放弃本次上传（不清空现有池）")
        return 1

    try:
        m.record_run_stats(candidates=len(infos), diverse=len(cand))
    except Exception:
        pass

    # ---------- 7) 写 result.csv（列名与 CloudflareST 一致） ----------
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["IP 地址", "端口", "已发送", "已接收", "丢包率",
                    "平均延迟", "下载速度(MB/s)", "地区码"])
        for info in cand:
            lat = info.get("ttfb") or info.get("rtt") or 0
            w.writerow([info["ip"], info["port"], 4, 4, 0.0,
                        f"{lat:.0f}", f"{info['speed']:.2f}", info.get("colo") or ""])

    print(f"\n总耗时 {time.time() - t0:.0f}s，写出 {len(cand)} 个候选到 {args.out}"
          f"（上传函数将从中挑 {args.pick} 个）")

    # ---------- 8) 上传 ----------
    if args.upload == "api":
        if not args.worker_domain or not args.uuid:
            print("❌ 上传需要 --worker-domain 与 --uuid")
            return 1
        m.upload_to_cloudflare_api_cli(
            result_file=args.out,
            worker_domain=args.worker_domain,
            uuid=args.uuid,
            # CSV 里是 cand_n 个候选，这里限定最终只写 10 个：上传函数会自己做
            # WS 握手 → TTFB → 机房分散，从候选里挑最好的 10 个。
            upload_count=args.pick,
            clear_existing=args.clear,
            # WS 握手在上传函数里做，所以"通过数少于底线就放弃"的严格判断
            # 也只能放在那里（本脚本上面的 --min-pick 只能按候选数提前退出）。
            min_usable=args.min_pick,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
