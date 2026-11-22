"""
台股盤後分析機器人
- 依投資計畫書檢查持股（回撤分級、停損/目標價、配置上限、主動部位虧損）
- 掃描上市櫃股票，找出符合條件的候選名單
- 結果傳到 Telegram，並把紀錄存在 state/ 與 reports/

平常不需要改這個檔案，所有設定都在 config.yaml。
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "state" / "state.json"
NAV_HISTORY_PATH = ROOT / "state" / "nav_history.csv"
REPORT_PATH = ROOT / "reports" / "latest.md"
TZ = ZoneInfo("Asia/Taipei")

TWSE_DAY_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_PE_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
TPEX_DAY_ALL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TPEX_PE_ALL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"

WARNINGS: list[str] = []


# ---------------------------------------------------------------- 工具函式

def to_float(v) -> float:
    if v is None:
        return math.nan
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "--", "---", "N/A", "NaN"):
        return math.nan
    try:
        return float(s)
    except ValueError:
        return math.nan


def roc_to_date(s) -> date | None:
    """'1151120' 或 '115/11/20' -> date(2026, 11, 20)"""
    if s is None:
        return None
    digits = re.sub(r"\D", "", str(s))
    if len(digits) == 7:
        y, m, d = int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7])
    elif len(digits) == 8:
        y, m, d = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
    else:
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def pick(rec: dict, keys: list[str]):
    for k in keys:
        if k in rec and rec[k] not in (None, ""):
            return rec[k]
    return None


def fetch_json(url: str, tries: int = 3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"無法取得 {url}：{last}")


def pct(x: float, digits: int = 1) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{x:+.{digits}f}%"


def money(x: float) -> str:
    return f"{x:,.0f}"


def num(x: float, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{x:,.{digits}f}"


# ---------------------------------------------------------------- 市場資料

def load_twse() -> pd.DataFrame:
    day = fetch_json(TWSE_DAY_ALL)
    pe = fetch_json(TWSE_PE_ALL)
    rows = []
    for r in day:
        rows.append({
            "code": str(r.get("Code", "")).strip(),
            "name": str(r.get("Name", "")).strip(),
            "market": "TW",
            "close": to_float(r.get("ClosingPrice")),
            "volume": to_float(r.get("TradeVolume")),
            "date": roc_to_date(r.get("Date")),
        })
    df = pd.DataFrame(rows)
    pe_rows = [{
        "code": str(r.get("Code", "")).strip(),
        "pe": to_float(r.get("PEratio")),
        "yield": to_float(r.get("DividendYield")),
        "pb": to_float(r.get("PBratio")),
        "pe_date": roc_to_date(r.get("Date")),
    } for r in pe]
    pe_df = pd.DataFrame(pe_rows)
    return df.merge(pe_df, on="code", how="left")


def load_tpex() -> pd.DataFrame:
    day = fetch_json(TPEX_DAY_ALL)
    pe = fetch_json(TPEX_PE_ALL)
    rows = []
    for r in day:
        rows.append({
            "code": str(pick(r, ["SecuritiesCompanyCode", "Code", "代號"]) or "").strip(),
            "name": str(pick(r, ["CompanyName", "Name", "名稱"]) or "").strip(),
            "market": "TWO",
            "close": to_float(pick(r, ["Close", "ClosingPrice", "收盤"])),
            "volume": to_float(pick(r, ["TradingShares", "TradeVolume", "Volume", "成交股數"])),
            "date": roc_to_date(pick(r, ["Date", "日期"])),
        })
    df = pd.DataFrame(rows)
    pe_rows = [{
        "code": str(pick(r, ["SecuritiesCompanyCode", "Code", "代號"]) or "").strip(),
        "pe": to_float(pick(r, ["PriceEarningRatio", "PEratio", "本益比"])),
        "yield": to_float(pick(r, ["YieldRatio", "DividendYield", "殖利率"])),
        "pb": to_float(pick(r, ["PriceBookRatio", "PBratio", "股價淨值比"])),
        "pe_date": roc_to_date(pick(r, ["Date", "日期"])),
    } for r in pe]
    pe_df = pd.DataFrame(pe_rows)
    if df.empty or df["code"].eq("").all():
        raise RuntimeError("櫃買資料格式無法辨識")
    return df.merge(pe_df, on="code", how="left")


def load_market(include_otc: bool) -> pd.DataFrame:
    frames = [load_twse()]
    if include_otc:
        try:
            frames.append(load_tpex())
        except Exception as e:  # noqa: BLE001
            WARNINGS.append(f"上櫃資料抓取失敗，今天只掃描上市股票（{e}）")
    df = pd.concat(frames, ignore_index=True)
    df = df[df["code"] != ""].drop_duplicates("code", keep="first")
    return df.set_index("code")


def load_history(tickers: dict[str, str], period: str = "9mo") -> dict[str, pd.DataFrame]:
    """tickers: {代號: 'TW' 或 'TWO'} -> {代號: DataFrame(Close, Volume)}"""
    if not tickers:
        return {}
    import yfinance as yf

    symbols = {f"{c}.{m}": c for c, m in tickers.items()}
    out: dict[str, pd.DataFrame] = {}
    chunk = list(symbols.keys())
    for i in range(0, len(chunk), 80):
        part = chunk[i:i + 80]
        try:
            data = yf.download(part, period=period, interval="1d", group_by="ticker",
                               auto_adjust=False, threads=True, progress=False)
        except Exception as e:  # noqa: BLE001
            WARNINGS.append(f"歷史股價抓取失敗：{e}")
            continue
        for sym in part:
            try:
                sub = data[sym] if isinstance(data.columns, pd.MultiIndex) else data
                sub = sub[["Close", "Volume"]].dropna()
                if len(sub) > 0:
                    out[symbols[sym]] = sub
            except Exception:  # noqa: BLE001
                continue
        time.sleep(1)
    return out


# ---------------------------------------------------------------- 狀態

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def append_nav_history(row: dict) -> None:
    NAV_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if NAV_HISTORY_PATH.exists():
        hist = pd.read_csv(NAV_HISTORY_PATH, dtype={"date": str})
        hist = hist[hist["date"] != row["date"]]
        hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
    else:
        hist = pd.DataFrame([row])
    hist.sort_values("date").to_csv(NAV_HISTORY_PATH, index=False)


# ---------------------------------------------------------------- 持股檢查

def flow_key(f: dict) -> str:
    return f"{f.get('date')}|{f.get('amount')}|{f.get('note', '')}"


def check_portfolio(cfg: dict, market: pd.DataFrame, trade_date: date, state: dict) -> dict:
    rules = cfg.get("rules", {}) or {}
    holdings = cfg.get("holdings", []) or []
    cash = float(cfg.get("cash", 0) or 0)

    alerts: list[str] = []
    lines: list[str] = []

    # 缺價格的持股改用歷史股價補
    missing = {str(h["code"]): "TW" for h in holdings if str(h["code"]) not in market.index}
    extra_hist = {}
    if missing:
        extra_hist = load_history({c: "TW" for c in missing}, period="1mo")
        still = [c for c in missing if c not in extra_hist]
        if still:
            extra_hist.update(load_history({c: "TWO" for c in still}, period="1mo"))

    items = []
    for h in holdings:
        code = str(h["code"]).strip()
        shares = float(h.get("shares", 0) or 0)
        cost = float(h.get("cost", 0) or 0)
        kind = str(h.get("type", "core")).lower()
        if code in market.index and not math.isnan(market.at[code, "close"]):
            price = float(market.at[code, "close"])
            name = h.get("name") or market.at[code, "name"]
        elif code in extra_hist:
            price = float(extra_hist[code]["Close"].iloc[-1])
            name = h.get("name") or code
        else:
            WARNINGS.append(f"找不到 {code} 的收盤價，暫以成本計算")
            price = cost
            name = h.get("name") or code
        plan_cost = float(h.get("plan_cost") or cost)  # 納入計畫時的價格；計畫規則用它計算
        items.append({
            "code": code, "name": name, "shares": shares, "cost": cost, "type": kind,
            "price": price, "mv": shares * price, "cost_total": shares * plan_cost,
            "stop_loss": h.get("stop_loss"), "target": h.get("target"),
        })

    total = sum(i["mv"] for i in items) + cash
    core_mv = sum(i["mv"] for i in items if i["type"] != "active")
    active_mv = sum(i["mv"] for i in items if i["type"] == "active")
    active_cost = sum(i["cost_total"] for i in items if i["type"] == "active")

    # 個股停損 / 目標價 / 佔比
    single_max = float(rules.get("single_stock_max_pct", 10))
    for i in items:
        gain = (i["price"] / i["cost"] - 1) * 100 if i["cost"] else math.nan
        weight = i["mv"] / total * 100 if total else 0
        tag = "主動" if i["type"] == "active" else "核心"
        lines.append(f"{i['code']} {i['name']}（{tag}）收 {num(i['price'])}，損益 {pct(gain)}，佔 {weight:.1f}%")
        if i["stop_loss"] is not None and i["price"] <= float(i["stop_loss"]):
            alerts.append(f"🚨 {i['code']} {i['name']} 收 {num(i['price'])}，已跌破停損價 {num(float(i['stop_loss']))}")
        if i["target"] is not None and i["price"] >= float(i["target"]):
            alerts.append(f"🎯 {i['code']} {i['name']} 收 {num(i['price'])}，已達目標價 {num(float(i['target']))}，依原計畫檢查是否出場")
        if i["type"] == "active" and weight > single_max:
            alerts.append(f"⚠️ {i['code']} {i['name']} 佔總資產 {weight:.1f}%，超過單一個股上限 {single_max:.0f}%")

    # 配置
    active_pct = active_mv / total * 100 if total else 0
    core_pct = core_mv / total * 100 if total else 0
    cash_pct = cash / total * 100 if total else 0
    active_max = float(rules.get("active_max_pct", 20))
    if active_pct > active_max:
        alerts.append(f"⚠️ 主動部位佔 {active_pct:.1f}%，超過上限 {active_max:.0f}%，暫停新開倉")

    active_gain = (active_mv / active_cost - 1) * 100 if active_cost else math.nan
    active_loss_alert = float(rules.get("active_loss_alert_pct", 20))
    if active_cost and active_gain <= -active_loss_alert:
        alerts.append(f"🚨 主動部位自納入計畫起虧損 {active_gain:.1f}%，達 {active_loss_alert:.0f}% 停損線：暫停新開倉，完成書面檢討後才能恢復")

    # 淨值與回撤（排除追加/提領資金）
    key = trade_date.isoformat()
    base = state.get("base") if state.get("last_date") == key else None
    s = dict(base) if base else {k: v for k, v in state.items() if k != "base"}
    snapshot_before = json.loads(json.dumps(s))

    flows = cfg.get("cash_flows", []) or []
    applied = set(s.get("applied_flows", []))
    pending = 0.0
    for f in flows:
        k = flow_key(f)
        fd = f.get("date")
        fd = fd if isinstance(fd, date) else roc_to_date(fd)
        if k not in applied and fd is not None and fd <= trade_date:
            pending += float(f.get("amount", 0) or 0)
            applied.add(k)

    if not s.get("units"):
        units = total / 100 if total > 0 else 1.0
        peak = 100.0
    else:
        units = float(s["units"])
        nav_pre = (total - pending) / units if units else 100.0
        if pending and nav_pre > 0:
            units += pending / nav_pre
        peak = float(s.get("peak_nav", 100.0))

    nav = total / units if units else 100.0
    if nav > peak:
        peak = nav
    dd = (peak - nav) / peak * 100 if peak else 0.0

    l1 = float(rules.get("drawdown_level1", 10))
    l2 = float(rules.get("drawdown_level2", 20))
    level = 2 if dd >= l2 else 1 if dd >= l1 else 0
    alert_state = s.get("alert", {"level": 0, "dd": 0.0})
    if dd <= 0.0001:
        alert_state = {"level": 0, "dd": 0.0}
    if level > alert_state.get("level", 0):
        if level == 1:
            alerts.append(f"🟠 第一級回撤警戒：組合從高點回落 {dd:.1f}%。核心定期投入照常，主動部位暫停新開倉，7 天內查明原因並記錄")
        else:
            alerts.append(f"🔴 第二級回撤警戒：組合從高點回落 {dd:.1f}%。核心定期投入照常，14 天內完成完整審查並記錄")
        alert_state = {"level": level, "dd": dd}
    elif level >= 1 and dd >= float(alert_state.get("dd", 0)) + 10:
        alerts.append(f"🔴 回撤再擴大到 {dd:.1f}%，請重新執行風險檢查")
        alert_state = {"level": level, "dd": dd}

    new_state = {
        "units": units,
        "peak_nav": peak,
        "applied_flows": sorted(applied),
        "alert": alert_state,
        "last_date": key,
        "base": snapshot_before,
    }

    append_nav_history({
        "date": key, "total_value": round(total, 0), "nav": round(nav, 4),
        "drawdown_pct": round(dd, 2), "core_pct": round(core_pct, 1),
        "active_pct": round(active_pct, 1), "cash_pct": round(cash_pct, 1),
        "net_flow": pending,
    })

    summary = [
        f"總值 {money(total)} 元（現金 {money(cash)}）",
        f"淨值 {nav:.2f}，距高點回撤 {dd:.1f}%（警戒 {l1:.0f}% / {l2:.0f}%）",
        f"核心 {core_pct:.0f}%｜主動 {active_pct:.0f}%｜現金 {cash_pct:.0f}%",
    ]
    if active_cost:
        summary.append(f"主動部位（自納入計畫起）{pct(active_gain)}")

    return {"alerts": alerts, "summary": summary, "lines": lines, "state": new_state,
            "held": {i["code"] for i in items}}


# ---------------------------------------------------------------- 觀察清單

def check_watchlist(cfg: dict, market: pd.DataFrame) -> list[str]:
    hits = []
    for w in cfg.get("watchlist", []) or []:
        code = str(w.get("code", "")).strip()
        if code not in market.index:
            continue
        price = market.at[code, "close"]
        name = market.at[code, "name"]
        below = w.get("buy_below")
        if below is not None and not math.isnan(price) and price <= float(below):
            note = f"（{w['note']}）" if w.get("note") else ""
            hits.append(f"👀 {code} {name} 收 {num(price)}，已到觀察價 {num(float(below))}{note}")
    return hits


# ---------------------------------------------------------------- 全市場掃描

def scan_market(cfg: dict, market: pd.DataFrame, held: set[str]) -> dict:
    sc = cfg.get("scan", {}) or {}
    df = market.copy()
    df = df[~df["close"].isna()]

    pattern = r"^[0-9A-Z]{4,6}$" if sc.get("include_etf", False) else r"^[1-9]\d{3}$"
    df = df[df.index.to_series().str.match(pattern)]

    prefixes = [str(p) for p in (sc.get("code_prefixes") or [])]
    if prefixes:
        df = df[df.index.to_series().apply(lambda c: any(c.startswith(p) for p in prefixes))]
    excl = {str(c) for c in (sc.get("exclude") or [])}
    df = df[~df.index.isin(excl)]

    df = df[(df["close"] >= float(sc.get("min_price", 0))) & (df["close"] <= float(sc.get("max_price", 1e9)))]
    df = df[df["volume"] >= float(sc.get("min_volume_lots_today", 0)) * 1000]

    if sc.get("pe_max") is not None:
        df = df[(df["pe"] > 0) & (df["pe"] <= float(sc["pe_max"]))]
    if sc.get("yield_min") is not None:
        df = df[df["yield"] >= float(sc["yield_min"])]
    if sc.get("pb_max") is not None:
        df = df[(df["pb"] > 0) & (df["pb"] <= float(sc["pb_max"]))]

    prefiltered = len(df)
    df = df.sort_values("yield", ascending=False).head(int(sc.get("prefilter_limit", 150)))

    need_tech = any([sc.get("above_ma60"), sc.get("ma60_rising"),
                     sc.get("max_above_ma20_pct") is not None, sc.get("min_avg_volume_lots_20d")])
    tech_ok = True
    results = []
    hist = load_history({c: df.at[c, "market"] for c in df.index}) if need_tech and len(df) else {}
    if need_tech and len(df) and not hist:
        tech_ok = False
        WARNINGS.append("歷史股價抓不到，今天的候選名單沒有經過均線與均量篩選")

    funnel = {"checked": len(df), "history": 0, "ma60": 0, "rising": 0, "ma20": 0, "volume": 0}
    for code, r in df.iterrows():
        info = {"code": code, "name": r["name"], "close": r["close"], "pe": r["pe"],
                "yield": r["yield"], "pb": r["pb"], "held": code in held, "notes": []}
        h = hist.get(code)
        if need_tech and tech_ok:
            if h is None or len(h) < 80:
                continue
            funnel["history"] += 1
            close = h["Close"]
            ma20 = close.rolling(20).mean()
            ma60 = close.rolling(60).mean()
            last_ma20, last_ma60 = float(ma20.iloc[-1]), float(ma60.iloc[-1])
            prev_ma60 = float(ma60.iloc[-21])
            avg_vol = float(h["Volume"].tail(20).mean()) / 1000
            px = float(r["close"])
            if sc.get("above_ma60") and px <= last_ma60:
                continue
            funnel["ma60"] += 1
            if sc.get("ma60_rising") and last_ma60 <= prev_ma60:
                continue
            funnel["rising"] += 1
            if sc.get("max_above_ma20_pct") is not None and px > last_ma20 * (1 + float(sc["max_above_ma20_pct"]) / 100):
                continue
            funnel["ma20"] += 1
            if sc.get("min_avg_volume_lots_20d") and avg_vol < float(sc["min_avg_volume_lots_20d"]):
                continue
            funnel["volume"] += 1
            info["dist_ma20"] = (px / last_ma20 - 1) * 100
            info["avg_vol"] = avg_vol
        results.append(info)

    results.sort(key=lambda x: (-(x["yield"] if not math.isnan(x["yield"]) else 0)))
    return {"results": results[: int(sc.get("top_n", 15))], "total_matched": len(results),
            "prefiltered": prefiltered, "tech_ok": tech_ok and need_tech, "funnel": funnel}


# ---------------------------------------------------------------- 訊息

def build_message(trade_date: date, port: dict, watch: list[str], scan: dict | None) -> str:
    out = [f"📊 盤後日報 {trade_date.isoformat()}", ""]

    out.append("【需要處理】")
    todo = port["alerts"] + watch
    out += todo if todo else ["今天沒有觸發任何規則"]
    out.append("")

    out.append("【組合狀況】")
    out += port["summary"]
    out.append("")

    if port["lines"]:
        out.append("【持股】")
        out += port["lines"]
        out.append("")

    if scan is not None:
        out.append(f"【市場掃描】符合條件 {scan['total_matched']} 檔（基本面初選 {scan['prefiltered']} 檔）")
        if not scan["results"]:
            out.append("今天沒有符合全部條件的股票")
        f = scan.get("funnel")
        if f and scan.get("tech_ok"):
            out.append(f"篩選過程：取殖利率前 {f['checked']} 檔 → 有歷史股價 {f['history']} → 站上季線 {f['ma60']} → 季線上彎 {f['rising']} → 未追高 {f['ma20']} → 均量足夠 {f['volume']}")
        for i, r in enumerate(scan["results"], 1):
            extra = f"，距月線 {pct(r['dist_ma20'])}" if "dist_ma20" in r else ""
            held = "（已持有）" if r["held"] else ""
            out.append(f"{i}. {r['code']} {r['name']}{held} 收 {num(r['close'])}｜殖利率 {num(r['yield'], 1)}%｜本益比 {num(r['pe'], 1)}｜淨值比 {num(r['pb'], 2)}{extra}")
        out.append("候選只是研究清單：依計畫書第五章寫好買進理由與出場條件後才可下單。")
        out.append("")

    if WARNINGS:
        out.append("【系統提醒】")
        out += [f"・{w}" for w in WARNINGS]

    return "\n".join(out).strip()


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("（沒有設定 Telegram，只輸出到畫面）")
        return
    parts, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > 3500:
            parts.append(buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        parts.append(buf)
    for p in parts:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": p, "disable_web_page_preview": True},
                          timeout=30)
        if r.status_code != 200:
            print(f"Telegram 傳送失敗：{r.status_code} {r.text}")
            sys.exit(1)


# ---------------------------------------------------------------- 主程式

def main() -> None:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    force = os.environ.get("FORCE_RUN", "").lower() in ("1", "true", "yes")
    today = datetime.now(TZ).date()

    try:
        market = load_market(bool((cfg.get("scan") or {}).get("include_otc", True)))
    except Exception as e:  # noqa: BLE001
        msg = f"⚠️ 盤後分析失敗：抓不到證交所資料，明天會再試一次。\n（{e}）"
        print(msg)
        send_telegram(msg)
        sys.exit(1)

    dates = market["date"].dropna()
    trade_date = max(dates) if len(dates) else today
    if trade_date != today and not force:
        print(f"今天（{today}）沒有新的交易資料（最新為 {trade_date}），可能是休市，不發送。")
        return
    pe_dates = market["pe_date"].dropna()
    if len(pe_dates) and max(pe_dates) != trade_date:
        WARNINGS.append(f"本益比資料日期為 {max(pe_dates)}，還沒更新到今天")

    state = load_state()
    port = check_portfolio(cfg, market, trade_date, state)
    watch = check_watchlist(cfg, market)
    scan = scan_market(cfg, market, port["held"]) if (cfg.get("scan") or {}).get("enabled", True) else None

    text = build_message(trade_date, port, watch, scan)
    print(text)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(text + "\n", encoding="utf-8")
    save_state(port["state"])
    send_telegram(text)


if __name__ == "__main__":
    main()
