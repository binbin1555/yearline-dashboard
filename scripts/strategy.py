# -*- coding: utf-8 -*-
"""策略引擎 —— 与回测逐行一致的实现。

规则（v1.0）
-----------
创业板腿（信号取自 399006 创业板指）
  进场  收盘 > MA250  且  当日成交额 >= 前20日均额 x 1.3
  出场  收盘 < MA250（无缓冲、无连续日确认）
  止盈  段内涨幅（自本段进场执行日收盘价算起）首次 >= 80% -> 卖出一半，每段一次
  持仓收益来自 399673 创业板50（信号与标的分离，是刻意选择）

红利腿（信号取自 000922 中证红利）
  平时空仓；跌破自身 MA250 的 -4%/-7%/-10% 三档，每档买 1/3
  涨回 MA250 -> 三档一次性全清，回空仓
  持仓收益来自 H00922 中证红利全收益

优先级  创业板进场信号 > 创业板出场信号 > 止盈 > 网格
执行    信号按收盘价判定，T+1 收盘执行

!! 本文件与 app.js 必须逐行等价，改一个另一个必须同步改 !!
"""

MA_N = 250
VOL_N = 20
VOL_K = 1.3
TAKE_PROFIT = 0.80
TIERS = (0.96, 0.93, 0.90)
COST = 0.0002
CASH_ANNUAL = 0.015
TRADING_DAYS = 243


def _sma(xs, n, i):
    if i < n - 1:
        return None
    w = xs[i - n + 1:i + 1]
    return None if any(v is None for v in w) else sum(w) / n


def indicators(daily):
    """预计算 MA250 / 前20日均额 / 红利 MA250。"""
    cyb, amt, hl = daily["cyb_sig"], daily["cyb_amt"], daily["hl_sig"]
    n = len(daily["dates"])
    ma = [_sma(cyb, MA_N, i) for i in range(n)]
    mah = [_sma(hl, MA_N, i) for i in range(n)]
    av = [None] * n
    for i in range(n):
        if i >= VOL_N:
            w = amt[i - VOL_N:i]
            if not any(v is None for v in w):
                av[i] = sum(w) / VOL_N
    return ma, av, mah


def tiers_at(hl, mah, i):
    if mah[i] is None or hl[i] is None:
        return 0
    r = hl[i] / mah[i]
    return sum(1 for t in TIERS if r <= t)


def earliest_start(daily):
    """最早可用起点：信号可算 且 创业板50 有数据。"""
    ma, av, mah = indicators(daily)
    for i, d in enumerate(daily["dates"]):
        if ma[i] and av[i] and mah[i] and daily["cyb50"][i]:
            return d
    return None


def run(daily, start_date, capital=100000.0):
    """从 start_date 起重放策略。返回 dict。"""
    dates = daily["dates"]
    cyb, hl = daily["cyb_sig"], daily["hl_sig"]
    amt = daily["cyb_amt"]
    c50, htr = daily["cyb50"], daily["hl_tr"]
    ma, av, mah = indicators(daily)
    n = len(dates)

    def usable(i):
        return ma[i] and av[i] and mah[i] and c50[i]

    s = None
    for i, d in enumerate(dates):
        if d >= start_date and usable(i):
            s = i
            break
    if s is None:
        # 起算日晚于全部已有数据（比如设成了未来某天）：退回最后一个可用交易日，
        # 让每日任务继续跑而不是整个崩掉。
        for i in range(n - 1, -1, -1):
            if usable(i):
                s = i
                break
    if s is None:
        raise ValueError("数据不足，无法计算任何交易日")

    def ret(series, i):
        a, b = series[i], series[i - 1]
        return (a / b - 1) if (a and b) else 0.0

    r_cash = (1 + CASH_ANNUAL) ** (1.0 / TRADING_DAYS) - 1
    v_cyb, v_hl, v_cash = 0.0, 0.0, float(capital)
    state, pend, entry_px, took, tier = "HL", None, None, False, 0
    curve, events = [], []

    def rebalance(i):
        """把防守腿调到 tier/3 的目标红利权重。"""
        nonlocal v_hl, v_cash
        pool = v_hl + v_cash
        tgt = pool * (tier / 3.0)
        if v_hl < tgt:
            d = min(tgt - v_hl, v_cash)
            v_cash -= d
            v_hl += d * (1 - COST)
        elif v_hl > tgt:
            d = v_hl - tgt
            v_hl -= d
            v_cash += d * (1 - COST)

    for i in range(s, n):
        v_cyb *= 1 + ret(c50, i)
        v_hl *= 1 + ret(htr, i)
        v_cash *= 1 + r_cash

        if pend and i >= pend[1]:
            kind = pend[0]
            if kind == "CYB":
                v_cyb = (v_hl * (1 - COST) + v_cash) * (1 - COST)
                v_hl = v_cash = 0.0
                entry_px, took, tier, state = cyb[i], False, 0, "CYB"
                events.append({"date": dates[i], "action": "进场", "detail": "全仓买入创业板50"})
            elif kind == "HL":
                v_cash += v_cyb * (1 - COST)
                v_cyb = 0.0
                state = "HL"
                tier = max(tier, tiers_at(hl, mah, i))
                rebalance(i)
                events.append({"date": dates[i], "action": "出场",
                               "detail": "清仓创业板50" + ("，同时补足红利 %d/3 仓" % tier if tier else "，转入现金")})
            else:
                half = v_cyb / 2.0
                v_cyb -= half
                v_cash += half * (1 - COST)
                took = True
                tier = max(tier, tiers_at(hl, mah, i))
                rebalance(i)
                events.append({"date": dates[i], "action": "止盈",
                               "detail": "卖出一半创业板50（段内涨幅达 %d%%）" % int(TAKE_PROFIT * 100)})
            pend = None

        # 防守腿网格。注意：不限定 state == "HL" —— 止盈卖出的那一半进入防守腿后
        # 必须立刻参与网格，不能干等创业板腿出场。回测即此逻辑，改动会导致对不上账。
        if (v_hl + v_cash) > 1e-9 and not pend:
            want = 0 if (tier > 0 and hl[i] >= mah[i]) else max(tier, tiers_at(hl, mah, i))
            if want != tier:
                old = tier
                tier = want
                rebalance(i)
                events.append({"date": dates[i],
                               "action": "红利止盈" if want == 0 else "红利加仓",
                               "detail": ("红利涨回 MA250，三档全部清空" if want == 0
                                          else "红利买入至 %d/3 仓（原 %d/3）" % (want, old))})

        equity = v_cyb + v_hl + v_cash
        curve.append({"date": dates[i], "equity": equity, "state": state, "tier": tier,
                      "w_cyb": v_cyb / equity, "w_hl": v_hl / equity, "w_cash": v_cash / equity})

        # 信号判定（收盘后）
        if not pend:
            nxt = min(i + 1, n - 1)
            if state == "HL":
                if cyb[i] > ma[i] and av[i] and amt[i] >= VOL_K * av[i]:
                    pend = ("CYB", nxt, i)
            else:
                if cyb[i] < ma[i]:
                    pend = ("HL", nxt, i)
                elif not took and entry_px and cyb[i] / entry_px - 1 >= TAKE_PROFIT:
                    pend = ("HALF", nxt, i)

    peak, mdd = -1e18, 0.0
    for p in curve:
        peak = max(peak, p["equity"])
        mdd = min(mdd, p["equity"] / peak - 1)
    years = len(curve) / float(TRADING_DAYS)
    total = curve[-1]["equity"] / float(capital) - 1
    cagr = (curve[-1]["equity"] / float(capital)) ** (1 / years) - 1 if years > 0.08 else None

    return {"curve": curve, "events": events, "start": dates[s], "days": len(curve),
            "years": years, "capital": float(capital), "equity": curve[-1]["equity"],
            "total_return": total, "cagr": cagr, "max_drawdown": mdd,
            "state": curve[-1]["state"], "tier": curve[-1]["tier"],
            "weights": {k: curve[-1][k] for k in ("w_cyb", "w_hl", "w_cash")},
            # exec_date 为 None 表示信号刚发生在最后一个已知交易日，执行日尚未产生
            "pending": ({"action": pend[0],
                         "exec_date": (dates[pend[1]] if pend[1] > pend[2] else None)}
                        if pend else None),
            "entry_px": entry_px, "took_profit": took}


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def next_triggers(daily, res, names=None):
    """下一步会触发什么。**按接近程度降序**，第一条就是最该盯的那个。

    与 app.js 的 nextTriggers 必须保持一致：页面和推送要给出同样的排序，
    否则会出现「页面说先看创业板、推送却先列红利」的矛盾。
    """
    names = names or {}
    n_cyb = names.get("cyb", "创业板50")
    n_hl = names.get("hl", "红利")
    i = len(daily["dates"]) - 1
    ma, av, mah = indicators(daily)
    cyb, hl, amt = daily["cyb_sig"], daily["hl_sig"], daily["cyb_amt"]
    out = []

    if res["state"] == "CYB":
        buf = cyb[i] / ma[i] - 1
        out.append({"key": "cyb_exit", "label": "创业板出场",
                    "action": "清仓%s，转入%s腿" % (n_cyb, n_hl),
                    "cond": "收盘跌破 MA250 %.2f" % ma[i],
                    "short": "尚有 %.2f%% 缓冲" % (buf * 100),
                    "progress": 1 - _clamp(buf / 0.20)})
        if not res["took_profit"] and res["entry_px"]:
            tgt = res["entry_px"] * (1 + TAKE_PROFIT)
            g = cyb[i] / res["entry_px"] - 1
            out.append({"key": "cyb_half", "label": "创业板止盈一半",
                        "action": "卖出一半%s" % n_cyb,
                        "cond": "本段涨幅达 +80%%，即指数涨到 %.2f" % tgt,
                        "short": "本段已涨 %.1f%%" % (g * 100),
                        "progress": _clamp(g / TAKE_PROFIT)})
    else:
        # 进场需同时满足「站上均线」和「放量」，取更难的那个作为距离
        ratio = (amt[i] / av[i]) if av[i] else None
        price_prog = _clamp(cyb[i] / ma[i])
        vol_prog = _clamp(ratio / VOL_K) if ratio else 0.0
        vol_binding = vol_prog <= price_prog
        out.append({"key": "cyb_entry", "label": "创业板进场",
                    "action": "全仓买入%s" % n_cyb,
                    "cond": ("已站上 MA250 %.2f，只差放量" % ma[i]) if cyb[i] > ma[i]
                            else ("需站上 MA250 %.2f，且成交额达 1.30×" % ma[i]),
                    "short": ("量能 %.2f×／需 1.30×" % ratio) if vol_binding and ratio
                             else ("再涨 %.2f%%" % ((ma[i] / cyb[i] - 1) * 100)),
                    "progress": min(price_prog, vol_prog)})
        tier = res["tier"]
        if tier < 3 and mah[i]:
            t = TIERS[tier]
            price = mah[i] * t
            out.append({"key": "hl_buy", "label": "红利第 %d 档买入" % (tier + 1),
                        "action": "买入 1/3 仓%s" % n_hl,
                        "cond": "跌破 %.2f（MA250 %.0f%%）" % (price, -round((1 - t) * 100)),
                        "short": "再跌 %.2f%%" % abs((price / hl[i] - 1) * 100),
                        "progress": _clamp((mah[i] - hl[i]) / (mah[i] - price))})
        if tier > 0 and mah[i]:
            need = mah[i] / hl[i] - 1
            out.append({"key": "hl_sell", "label": "红利止盈",
                        "action": "清空全部%s仓位，回到现金" % n_hl,
                        "cond": "涨回 MA250 %.2f" % mah[i],
                        "short": "再涨 %.2f%%" % (need * 100),
                        "progress": _clamp(1 - need / 0.10)})

    out.sort(key=lambda x: -x["progress"])
    return out
