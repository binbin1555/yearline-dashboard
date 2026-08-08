# -*- coding: utf-8 -*-
"""每日主流程：抓增量 -> 合并 -> 算状态 -> 写 JSON -> Bark 推送。

推送策略
--------
交易日          推送当日概况 + 需要执行的操作
非交易日        静默
数据抓取失败    无论何时都推告警（不静默）
数据长期停滞    超过 alert_stale_days 天没有新数据 -> 推告警

退出码：0 正常；1 数据异常（已发告警）。
"""
import datetime
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch as F                                                    # noqa: E402
import strategy as S                                                 # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAILY = os.path.join(ROOT, "data", "daily.json")
STATUS = os.path.join(ROOT, "data", "status.json")
CONFIG = os.path.join(ROOT, "config.json")

BARK_KEY = (os.environ.get("BARK_KEY") or "").strip()
# 注意：GitHub 未设置的 secret 会注入空字符串而非缺失，不能用 get(k, default)
BARK_HOST = ((os.environ.get("BARK_HOST") or "").strip() or "https://api.day.app").rstrip("/")


def today_bj():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date()


def bark(title, body, level="active", group="年线轮动", url=None):
    if not BARK_KEY:
        print("[bark] 未配置 BARK_KEY，跳过推送")
        return False
    payload = {"device_key": BARK_KEY, "title": title, "body": body,
               "level": level, "group": group, "isArchive": 1}
    if url:
        payload["url"] = url
    try:
        req = urllib.request.Request(
            BARK_HOST + "/push",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=30) as r:
            ok = json.loads(r.read().decode("utf-8", "ignore"))
        print("[bark] %s" % ok.get("message", ok))
        return True
    except Exception as exc:                                         # noqa: BLE001
        print("[bark] 推送失败: %r" % exc)
        return False


def pct(x, digits=2):
    # 零值不加正负号，避免出现 "+0.00%" 这种别扭写法（与 app.js 的 pct 保持一致）
    if abs(x) < 5e-5:
        return ("%." + str(digits) + "f%%") % 0.0
    return ("%+." + str(digits) + "f%%") % (x * 100)


def build_body(cfg, res, triggers, daily):
    names = cfg.get("names", {})
    n_cyb = names.get("cyb", "创业板50")
    n_hl = names.get("hl", "红利")
    w = res["weights"]

    if res["state"] == "CYB":
        pos = "%s %.0f%%" % (n_cyb, w["w_cyb"] * 100)
        if w["w_hl"] > 0.005:
            pos += " ／ %s %.0f%%" % (n_hl, w["w_hl"] * 100)
        if w["w_cash"] > 0.005:
            pos += " ／ 现金 %.0f%%" % (w["w_cash"] * 100)
    elif res["tier"] > 0:
        pos = "%s %d/3 仓（%.0f%%）／ 现金 %.0f%%" % (n_hl, res["tier"],
                                                     w["w_hl"] * 100, w["w_cash"] * 100)
    else:
        pos = "空仓（现金 100%）"

    lines = ["持仓：%s" % pos,
             "净值：%s（%s）" % ("{:,.0f}".format(res["equity"]), pct(res["total_return"]))]
    if res["cagr"] is not None:
        lines.append("年化：%s ／ 最大回撤：%s" % (pct(res["cagr"]), pct(res["max_drawdown"])))
    else:
        lines.append("最大回撤：%s（运行未满一月，年化暂不显示）" % pct(res["max_drawdown"]))

    # triggers 已按接近程度降序，第一条就是最该盯的
    if triggers:
        head = triggers[0]
        lines.append("")
        lines.append("【最接近触发】%s" % head["action"])
        lines.append("  %s（%s）" % (head["short"], head["cond"]))
        for t in triggers[1:3]:
            lines.append("· %s：%s → %s" % (t["label"], t["short"], t["action"]))
    lines.append("")
    lines.append("数据截至 %s" % daily["updated"])
    return "\n".join(lines)


def main():
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    daily = json.load(open(DAILY, encoding="utf-8"))
    today = today_bj()

    # 占位符没改的话当作未设置，避免推送里带一个点不开的死链
    page_url = (cfg.get("page_url") or "").strip()
    if not page_url.startswith("http") or "OWNER" in page_url or "YOUR_" in page_url:
        page_url = None

    # ---------- 抓取 ----------
    log, added, revised, fetch_ok = [], [], [], True
    now_bj = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    try:
        series, log = F.fetch_recent(cfg.get("lookback_days", 90))
        # 盘中保护：收盘结算前抓到的当日数据是实时值，不是收盘价，必须丢弃。
        # 正常 21:00 触发不受影响；只有手动/误触发时才会命中。
        if now_bj.hour < int(cfg.get("final_after_hour", 17)):
            key = today.strftime("%Y-%m-%d")
            dropped = any(key in v for v in series.values())
            for v in series.values():
                v.pop(key, None)
            if dropped:
                log.append("     丢弃 %s 的盘中数据（当前 %02d:%02d 早于收盘结算）"
                           % (key, now_bj.hour, now_bj.minute))
        added, revised = F.merge(daily, series)
    except F.AllSourcesFailed as exc:
        fetch_ok = False
        log.append("ALL SOURCES FAILED: %s" % exc)
    except Exception as exc:                                         # noqa: BLE001
        fetch_ok = False
        log.append("UNEXPECTED: %r" % exc)
    for line in log:
        print("  " + line)

    stale_days = (today - datetime.date(*map(int, daily["updated"].split("-")))).days
    stale = stale_days > int(cfg.get("alert_stale_days", 10))

    # ---------- 计算 ----------
    res = triggers = None
    try:
        start = cfg.get("start_date") or S.earliest_start(daily)
        res = S.run(daily, start, float(cfg.get("initial_capital", 100000)))
        triggers = S.next_triggers(daily, res, cfg.get("names"))
    except Exception as exc:                                         # noqa: BLE001
        print("  策略计算失败: %r" % exc)

    # ---------- 落盘 ----------
    json.dump(daily, open(DAILY, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    status = {
        "generated_at": now_bj.strftime("%Y-%m-%d %H:%M:%S+08:00"),
        "data_updated": daily["updated"],
        "stale_days": stale_days,
        "fetch_ok": fetch_ok,
        "added": added,
        "revised": revised,
        "log": log,
    }
    if res:
        status.update({
            "state": res["state"], "tier": res["tier"], "weights": res["weights"],
            "equity": res["equity"], "total_return": res["total_return"],
            "cagr": res["cagr"], "max_drawdown": res["max_drawdown"],
            "start": res["start"], "days": res["days"],
            "pending": res["pending"],
            "triggers": triggers,
            "recent_events": res["events"][-12:],
        })
    json.dump(status, open(STATUS, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # ---------- 推送 ----------
    # 1) 故障告警：任何时候都推
    if not fetch_ok or stale:
        why = "所有数据源均不可达" if not fetch_ok else "数据已停滞 %d 天" % stale_days
        bark("⚠️ 数据异常", "%s\n最新数据仍为 %s\n请检查接口是否改版或被限流。"
             % (why, daily["updated"]), level="timeSensitive", url=page_url)
        return 1

    # 1.5) 手动勾选「强制测试推送」：不管是不是交易日都发一条，用于验证 BARK_KEY
    if (os.environ.get("TEST_PUSH") or "").strip().lower() in ("1", "true", "yes"):
        if res:
            body = build_body(cfg, res, triggers, daily)
        else:
            body = "策略引擎未能计算，但数据抓取正常（截至 %s）。" % daily["updated"]
        ok = bark("✅ 测试推送 · 配置正常",
                  "能看到这条，说明 BARK_KEY、令牌、cron-job、Actions 全链路都通了。\n\n"
                  + body, level="timeSensitive", url=page_url)
        if not ok:
            print("  测试推送失败：BARK_KEY 可能未配置或填错")
            return 1
        print("  测试推送已发出")
        return 0

    # 2) 非交易日静默
    is_trading_day = daily["updated"] == today.strftime("%Y-%m-%d")
    if not is_trading_day:
        print("  非交易日（最新数据 %s ≠ 今日 %s），静默" % (daily["updated"], today))
        return 0
    if not res:
        bark("⚠️ 策略计算失败", "数据已更新至 %s，但策略引擎报错，请查看 Actions 日志。"
             % daily["updated"], level="timeSensitive", url=page_url)
        return 1

    # 3) 交易日推送
    acted = [e for e in res["events"] if e["date"] == daily["updated"]]
    pending = res["pending"]
    if pending:
        title = "🔴 明日需操作 · %s" % {"CYB": "进场", "HL": "出场", "HALF": "止盈一半"}[pending["action"]]
        head = {"CYB": "全仓买入创业板50", "HL": "清仓创业板50", "HALF": "卖出一半创业板50"}[pending["action"]]
        when = pending["exec_date"] or "下一个交易日"
        body = "【%s】于 %s 收盘执行\n\n%s" % (head, when,
                                              build_body(cfg, res, triggers, daily))
        bark(title, body, level="timeSensitive", url=page_url)
    elif acted:
        title = "🟠 今日已执行 · " + "／".join(e["action"] for e in acted)
        body = "\n".join("· %s" % e["detail"] for e in acted) + "\n\n" + \
               build_body(cfg, res, triggers, daily)
        bark(title, body, level="timeSensitive", url=page_url)
    else:
        bark("年线轮动 · %s" % daily["updated"],
             build_body(cfg, res, triggers, daily), level="passive", url=page_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
