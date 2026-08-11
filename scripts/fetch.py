# -*- coding: utf-8 -*-
"""多源降级抓取日线数据（仅用标准库）。

设计原则
--------
1. **宁可不更新，绝不用陈旧数据冒充新数据。** 某个交易日只有在全部必需序列
   都拿到时才会被写入；任何一个缺失，整天跳过。
2. 增量更新。仓库里预置了从可信环境抓好的全历史，日常只补最近几十天。
3. 多源降级。任一源失败自动换下一个；全部失败则抛 AllSourcesFailed。

必需序列
--------
cyb_sig  399006 创业板指 收盘（信号）
cyb_amt  399006 成交额，单位万元（信号）
cyb50    399673 创业板50 收盘（实际持仓收益）
hl_sig   000922 中证红利 收盘（网格信号）
hl_tr    H00922 中证红利全收益 收盘（实际持仓收益）
"""
import datetime
import http.cookiejar
import json
import ssl
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


class AllSourcesFailed(Exception):
    pass


def _get(url, timeout=45, opener=None, encoding="utf-8"):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json, text/plain, */*"})
    op = opener or urllib.request.build_opener(urllib.request.HTTPSHandler(context=_CTX))
    with op.open(req, timeout=timeout) as r:
        return r.read().decode(encoding, "ignore")


def _bj_date(ms):
    """雪球时间戳 -> 北京时间日期。绝不能用 localtime：CI 跑在 UTC。"""
    return (datetime.datetime(1970, 1, 1)
            + datetime.timedelta(milliseconds=ms)
            + datetime.timedelta(hours=8)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# 源 1：中证指数官网（000922 / H00922 的权威来源）
# --------------------------------------------------------------------------
def csindex(code, start, end):
    url = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
           "?indexCode=%s&startDate=%s&endDate=%s" % (code, start, end))
    j = json.loads(_get(url))
    if j.get("code") != "200":
        raise RuntimeError("csindex %s: %s" % (code, j.get("msg")))
    out = {}
    for r in j.get("data") or []:
        d = r["tradeDate"]
        if r.get("close"):
            out["%s-%s-%s" % (d[:4], d[4:6], d[6:])] = float(r["close"])
    if not out:
        raise RuntimeError("csindex %s: empty" % code)
    return out


# --------------------------------------------------------------------------
# 源 2：雪球（唯一能同时给出指数成交额的源）
# --------------------------------------------------------------------------
def _xq_opener():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),
                                     urllib.request.HTTPSHandler(context=_CTX))
    op.addheaders = [("User-Agent", UA)]
    op.open("https://xueqiu.com/hq", timeout=25).read()
    return op


def xueqiu(symbol, count=120, opener=None):
    op = opener or _xq_opener()
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    url = ("https://stock.xueqiu.com/v5/stock/chart/kline.json?symbol=%s&begin=%d"
           "&period=day&type=before&count=-%d&indicator=kline" % (symbol, now, count))
    j = json.loads(_get(url, opener=op))
    col = j["data"]["column"]
    ic, it, ia = col.index("close"), col.index("timestamp"), col.index("amount")
    out = {}
    for r in j["data"]["item"]:
        out[_bj_date(r[it])] = (float(r[ic]), r[ia])
    if not out:
        raise RuntimeError("xueqiu %s: empty" % symbol)
    return out


# --------------------------------------------------------------------------
# 源 3：新浪（收盘价兜底，无成交额）
# --------------------------------------------------------------------------
def sina(symbol, count=120):
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d" % (symbol, count))
    rows = json.loads(_get(url))
    out = {r["day"][:10]: float(r["close"]) for r in rows}
    if not out:
        raise RuntimeError("sina %s: empty" % symbol)
    return out


# --------------------------------------------------------------------------
# 源 4：腾讯（收盘价兜底，无成交额）
# --------------------------------------------------------------------------
def tencent(symbol, start, end):
    url = ("http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,320,"
           % (symbol, start, end))
    j = json.loads(_get(url))
    rows = j["data"][symbol].get("day") or j["data"][symbol].get("qfqday") or []
    out = {r[0]: float(r[2]) for r in rows}
    if not out:
        raise RuntimeError("tencent %s: empty" % symbol)
    return out


# --------------------------------------------------------------------------
# 汇总：抓取最近 lookback 个自然日的全部序列
# --------------------------------------------------------------------------
def fetch_recent(lookback_days=90):
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date()
    start = today - datetime.timedelta(days=lookback_days)
    s_compact, e_compact = start.strftime("%Y%m%d"), today.strftime("%Y%m%d")
    s_dash, e_dash = start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")

    series, log = {}, []

    def attempt(name, fn):
        try:
            v = fn()
            log.append("OK   %s (%d)" % (name, len(v)))
            return v
        except Exception as exc:                                    # noqa: BLE001
            log.append("FAIL %s: %s" % (name, type(exc).__name__))
            return None

    # 雪球一次会话复用，减少握手
    xq_op = None
    try:
        xq_op = _xq_opener()
        log.append("OK   xueqiu session")
    except Exception as exc:                                        # noqa: BLE001
        log.append("FAIL xueqiu session: %s" % type(exc).__name__)

    xq_cyb = attempt("xueqiu SZ399006", lambda: xueqiu("SZ399006", opener=xq_op)) if xq_op else None
    xq_c50 = attempt("xueqiu SZ399673", lambda: xueqiu("SZ399673", opener=xq_op)) if xq_op else None
    xq_hl = attempt("xueqiu SH000922", lambda: xueqiu("SH000922", opener=xq_op)) if xq_op else None

    # --- cyb_sig / cyb_amt：成交额只有雪球给，没有替代品 ---
    if xq_cyb:
        series["cyb_sig"] = {d: v[0] for d, v in xq_cyb.items()}
        series["cyb_amt"] = {d: int(v[1] / 1e4) for d, v in xq_cyb.items() if v[1]}
    else:
        alt = (attempt("sina sz399006", lambda: sina("sz399006"))
               or attempt("tencent sz399006", lambda: tencent("sz399006", s_dash, e_dash)))
        if alt:
            series["cyb_sig"] = alt
        # cyb_amt 无兜底 —— 缺了就不会有新交易日被写入，这是有意的

    # --- cyb50 ---
    if xq_c50:
        series["cyb50"] = {d: v[0] for d, v in xq_c50.items()}
    else:
        alt = attempt("tencent sz399673", lambda: tencent("sz399673", s_dash, e_dash))
        if alt:
            series["cyb50"] = alt

    # --- hl_sig：中证官网优先（权威） ---
    hl = attempt("csindex 000922", lambda: csindex("000922", s_compact, e_compact))
    if not hl and xq_hl:
        hl = {d: v[0] for d, v in xq_hl.items()}
        log.append("     hl_sig 降级至雪球")
    if not hl:
        hl = attempt("sina sh000922", lambda: sina("sh000922"))
    if hl:
        series["hl_sig"] = hl

    # --- hl_tr：全收益指数只有中证官网有 ---
    tr = attempt("csindex H00922", lambda: csindex("H00922", s_compact, e_compact))
    if tr:
        series["hl_tr"] = tr

    if not series:
        raise AllSourcesFailed("所有数据源均不可达")
    return series, log


# v2.0 起进场条件不再使用成交额，因此 cyb_amt 降级为可选。
# 它此前是唯一的单点故障（只有雪球提供指数成交额，没有备源），
# 降级后某天抓不到也不会阻塞整个交易日的数据写入。
REQUIRED = ("cyb_sig", "cyb50", "hl_sig", "hl_tr")
OPTIONAL = ("cyb_amt",)
ALL_KEYS = REQUIRED + OPTIONAL


def merge(daily, series):
    """把新抓到的序列并入 daily。

    REQUIRED 四个序列必须齐全，缺一个则整天跳过；
    OPTIONAL（成交额）缺失时写入 None，保证各列长度始终对齐。

    返回 (新增日期列表, 修订日期列表)。
    """
    idx = {d: i for i, d in enumerate(daily["dates"])}
    candidates = set()
    for k in REQUIRED:
        candidates |= set(series.get(k, {}))

    def norm(k, v):
        if v is None:
            return None
        return int(v) if k == "cyb_amt" else round(v, 2)

    added, revised = [], []
    for d in sorted(candidates):
        vals = {}
        complete = True
        for k in REQUIRED:
            v = series.get(k, {}).get(d)
            if v is None:
                complete = False
                break
            vals[k] = norm(k, v)
        if not complete:
            continue
        for k in OPTIONAL:
            vals[k] = norm(k, series.get(k, {}).get(d))

        if d in idx:                                   # 已有：仅修订明显偏差
            i = idx[d]
            changed = False
            for k in REQUIRED:
                old = daily[k][i]
                if old is None or abs(old - vals[k]) > max(0.02, abs(vals[k]) * 1e-4):
                    changed = True
                    break
            filled = False
            for k in OPTIONAL:
                # 可选列：只补空，不覆盖已有值。v3.0 起成交额重新参与进场判定，
                # 必须能在后续抓取中把之前缺失的补回来，否则会连续20天无法评估进场。
                if daily[k][i] is None and vals[k] is not None:
                    daily[k][i] = vals[k]
                    filled = True
            if changed:
                for k in REQUIRED:
                    daily[k][i] = vals[k]
                for k in OPTIONAL:
                    if vals[k] is not None:
                        daily[k][i] = vals[k]
            if changed or filled:
                revised.append(d)
        elif d > daily["dates"][-1]:                   # 新增：只接受更晚的日期
            daily["dates"].append(d)
            for k in ALL_KEYS:
                daily[k].append(vals[k])
            added.append(d)

    if added or revised:
        daily["updated"] = daily["dates"][-1]
    return added, revised
