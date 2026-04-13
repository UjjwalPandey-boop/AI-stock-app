from flask import Flask, render_template, request, jsonify
import yfinance as yf
from datetime import datetime, timedelta
from typing import Optional
import time
import json
import urllib.request
import urllib.parse
import pandas as pd
from io import StringIO
import os
import random
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
@app.route("/")
def home():
    return render_template("index.html")


API_PATHS = {
    '/get_stock_data',
    '/portfolio/add',
    '/portfolio/list',
    '/portfolio/remove',
    '/config/twelvedata',
}

# Simple in-memory portfolio store (resets when server restarts).
# Structure: { "AAPL": {"buy_price": 150.0, "shares": 2.0} }
PORTFOLIO = {}

CURRENCIES = {'USD', 'INR', 'EUR', 'GBP'}
CURRENCY_SYMBOLS = {'USD': '$', 'INR': '₹', 'EUR': '€', 'GBP': '£'}

FX_CACHE = {
    'timestamp': 0.0,
    'rates': {'USD': 1.0},
}
FX_TTL_SECONDS = 30 * 60
TWELVEDATA_API_KEY_RUNTIME = ""


def normalize_currency(value: Optional[str]) -> str:
    ccy = (value or 'USD').upper().strip()
    return ccy if ccy in CURRENCIES else 'USD'


def fetch_usd_fx_rates() -> dict:
    """
    Fetch USD-based FX rates from a free endpoint.
    Uses: open.er-api.com (no key).
    """
    url = "https://open.er-api.com/v6/latest/USD"
    with urllib.request.urlopen(url, timeout=6) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    if data.get("result") != "success":
        raise RuntimeError("FX API returned non-success")
    rates = data.get("rates") or {}
    if not isinstance(rates, dict) or "USD" not in rates:
        raise RuntimeError("FX API returned invalid rates")
    return rates


def fetch_history_from_yahoo_chart(stock_symbol: str, range_value: str = "6mo", interval: str = "1d"):
    """
    Direct Yahoo chart API fallback (without yfinance wrapper state).
    Returns DataFrame with Close column or empty DataFrame on failure.
    """
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_symbol}"
        f"?range={range_value}&interval={interval}&includePrePost=false&events=div%2Csplits"
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read().decode("utf-8")
    payload = json.loads(raw)

    chart = (payload.get("chart") or {})
    err = chart.get("error")
    if err:
        raise RuntimeError(str(err))
    results = chart.get("result") or []
    if not results:
        return pd.DataFrame()
    first = results[0] or {}
    timestamps = first.get("timestamp") or []
    quote_list = (((first.get("indicators") or {}).get("quote")) or [])
    if not quote_list:
        return pd.DataFrame()
    closes = (quote_list[0] or {}).get("close") or []
    if not timestamps or not closes:
        return pd.DataFrame()

    rows = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        try:
            dt = datetime.utcfromtimestamp(int(ts))
            rows.append((dt, float(close)))
        except Exception:
            continue

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["Date", "Close"]).set_index("Date")
    return df


def fetch_history_from_stooq(stock_symbol: str):
    """
    Stooq CSV fallback (no API key).
    For US stocks, Stooq commonly uses .US suffix.
    Returns DataFrame with Date index and Close column.
    """
    candidates = [stock_symbol.lower(), f"{stock_symbol.lower()}.us"]
    for sym in candidates:
        url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            csv_raw = resp.read().decode("utf-8", errors="ignore")
        if not csv_raw or "No data" in csv_raw:
            continue
        try:
            df = pd.read_csv(StringIO(csv_raw))
        except Exception:
            continue
        if df is None or df.empty or "Date" not in df.columns or "Close" not in df.columns:
            continue

        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
        df = df.dropna(subset=["Date", "Close"]).set_index("Date").sort_index()
        if not df.empty:
            return df
    return pd.DataFrame()


def fetch_history_from_twelvedata(stock_symbol: str):
    """
    Optional fallback using Twelve Data (requires API key in env var).
    Env: TWELVEDATA_API_KEY
    """
    api_key = (TWELVEDATA_API_KEY_RUNTIME or os.getenv("TWELVEDATA_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("TWELVEDATA_API_KEY not configured")

    query = urllib.parse.urlencode({
        "symbol": stock_symbol,
        "interval": "1day",
        "outputsize": 180,
        "apikey": api_key,
    })
    url = f"https://api.twelvedata.com/time_series?{query}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read().decode("utf-8")
    payload = json.loads(raw)

    if payload.get("status") == "error":
        raise RuntimeError(payload.get("message") or "Twelve Data API error")

    values = payload.get("values") or []
    if not values:
        return pd.DataFrame()

    rows = []
    for row in values:
        dt_raw = row.get("datetime")
        close_raw = row.get("close")
        if dt_raw is None or close_raw is None:
            continue
        try:
            dt = datetime.fromisoformat(str(dt_raw))
            close = float(close_raw)
            rows.append((dt, close))
        except Exception:
            continue

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["Date", "Close"]).set_index("Date").sort_index()
    return df


def generate_demo_history(stock_symbol: str, days: int = 126) -> pd.DataFrame:
    """
    Generate deterministic synthetic daily close prices so the UI remains usable
    when all external providers are blocked by network policy.
    """
    seed = sum(ord(ch) for ch in stock_symbol.upper()) % 100000
    rng = random.Random(seed)

    base_price = 80 + (seed % 220)  # 80..299
    drift = rng.uniform(-0.0007, 0.0012)
    vol = rng.uniform(0.008, 0.02)

    dates = pd.bdate_range(end=datetime.now(), periods=days)
    price = float(base_price)
    closes = []
    for i in range(days):
        cyc = 0.002 * (1 if (i // 14) % 2 == 0 else -1)
        shock = rng.gauss(0, vol)
        price = max(2.0, price * (1.0 + drift + cyc + shock))
        closes.append(price)

    return pd.DataFrame({"Close": closes}, index=dates)


@app.route('/config/twelvedata', methods=['POST'])
def config_twelvedata():
    global TWELVEDATA_API_KEY_RUNTIME
    payload = request.get_json(silent=True) or {}
    key = str(payload.get('api_key', '')).strip()
    # Allow clearing key by sending empty string.
    TWELVEDATA_API_KEY_RUNTIME = key
    return jsonify({'ok': True, 'configured': bool(TWELVEDATA_API_KEY_RUNTIME)})


def get_usd_fx_rates() -> dict:
    now = time.time()
    if FX_CACHE.get('rates') and now - float(FX_CACHE.get('timestamp', 0.0)) < FX_TTL_SECONDS:
        return FX_CACHE['rates']
    try:
        rates = fetch_usd_fx_rates()
        FX_CACHE['rates'] = rates
        FX_CACHE['timestamp'] = now
        return rates
    except Exception:
        # Fallback to last known rates (or USD-only) if the API fails.
        return FX_CACHE.get('rates') or {'USD': 1.0}


def convert_amount(amount: float, from_ccy: str, to_ccy: str, usd_rates: dict) -> float:
    from_ccy = normalize_currency(from_ccy)
    to_ccy = normalize_currency(to_ccy)
    if from_ccy == to_ccy:
        return float(amount)

    # usd_rates maps: 1 USD -> rate CCY
    rate_from = float(usd_rates.get(from_ccy) or 0.0)
    rate_to = float(usd_rates.get(to_ccy) or 0.0)
    if from_ccy != 'USD' and rate_from <= 0:
        raise RuntimeError(f"Missing FX rate for {from_ccy}")
    if to_ccy != 'USD' and rate_to <= 0:
        raise RuntimeError(f"Missing FX rate for {to_ccy}")

    amount_usd = float(amount) if from_ccy == 'USD' else (float(amount) / rate_from)
    return amount_usd if to_ccy == 'USD' else (amount_usd * rate_to)


def _is_api_request() -> bool:
    return request.path in API_PATHS or request.path.startswith('/portfolio')


def api_error(message: str, status_code: int = 500, symbol: Optional[str] = None):
    payload = {
        'error': message,
        'suggestions': [],
        'dates': [],
        'prices': [],
    }
    if symbol:
        payload['symbol'] = symbol
    return jsonify(payload), status_code


@app.errorhandler(HTTPException)
def handle_http_exception(e: HTTPException):
    # Return JSON for API endpoints; keep default behavior for the web page.
    if _is_api_request():
        return api_error(e.description or 'Request failed', e.code or 500)
    return e


@app.errorhandler(Exception)
def handle_unexpected_exception(e: Exception):
    # Ensure API never returns empty/non-JSON responses.
    if _is_api_request():
        return api_error('Internal server error', 500)
    return "Internal server error", 500


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_stock_data', methods=['POST'])
def get_stock_data():
    stock_symbol: Optional[str] = None
    try:
        payload = request.get_json(silent=True) or {}
        stock_symbol = str(payload.get('symbol', '')).upper().strip()
        target_currency = normalize_currency(payload.get('currency'))
        
        if not stock_symbol:
            return api_error('Please enter a stock symbol', 400)

        analysis = analyze_stock(stock_symbol, include_chart=True, target_currency=target_currency)
        if analysis.get('error'):
            return api_error(analysis['error'], analysis.get('status_code', 500), stock_symbol)
        return jsonify(analysis)
        
    except Exception as e:
        error_msg = str(e)
        if 'No data found' in error_msg or 'symbol may be delisted' in error_msg.lower():
            return api_error(
                f'Invalid stock symbol: {stock_symbol}. Please check the symbol and try again.',
                404,
                stock_symbol,
            )
        return api_error(f'Error fetching stock data: {error_msg}', 500, stock_symbol)

def generate_suggestion(growth_percentage, volatility, current_price, avg_price):
    """Generate basic investment suggestion based on stock performance"""
    suggestions = []
    
    # Growth-based suggestion
    if growth_percentage > 15:
        suggestions.append("Strong positive growth trend over the past 6 months.")
    elif growth_percentage > 5:
        suggestions.append("Moderate positive growth observed.")
    elif growth_percentage > -5:
        suggestions.append("Relatively stable performance.")
    elif growth_percentage > -15:
        suggestions.append("Moderate decline in value.")
    else:
        suggestions.append("Significant decline over the past 6 months.")
    
    # Volatility-based suggestion
    volatility_percentage = (volatility / avg_price) * 100 if avg_price > 0 else 0
    if volatility_percentage > 10:
        suggestions.append("High volatility detected - consider your risk tolerance.")
    elif volatility_percentage > 5:
        suggestions.append("Moderate volatility - relatively stable price movements.")
    else:
        suggestions.append("Low volatility - stable price movements.")
    
    # Price position suggestion
    price_deviation = ((current_price - avg_price) / avg_price) * 100 if avg_price > 0 else 0
    if price_deviation > 10:
        suggestions.append("Current price is significantly above 6-month average - may be overvalued.")
    elif price_deviation < -10:
        suggestions.append("Current price is below 6-month average - potential buying opportunity.")
    else:
        suggestions.append("Current price is near the 6-month average.")
    
    # Overall recommendation
    if growth_percentage > 10 and volatility_percentage < 8:
        suggestions.append("Overall: Consider this stock for moderate-risk growth portfolio.")
    elif growth_percentage < -10:
        suggestions.append("Overall: Exercise caution - significant decline may indicate underlying issues.")
    else:
        suggestions.append("Overall: Do your own research and consider consulting a financial advisor.")
    
    return suggestions


def analyze_stock(stock_symbol: str, include_chart: bool = True, target_currency: str = 'USD') -> dict:
    """
    Fetch and analyze stock for last ~6 months.
    Returns a JSON-ready dict. On failure returns {'error': ..., 'status_code': ...}.
    """
    ticker = yf.Ticker(stock_symbol)
    stock_info = {}
    try:
        stock_info = ticker.info or {}
    except Exception:
        stock_info = {}
    native_currency = normalize_currency(stock_info.get('currency') or 'USD')
    target_currency = normalize_currency(target_currency)

    # Use period-based history instead of absolute dates to avoid
    # system clock/date edge-cases returning empty data.
    # yfinance can sometimes throw provider/network related exceptions;
    # guard with fallbacks to avoid bubbling cryptic internal errors.
    hist = None
    history_errors = []
    attempted_sources = []
    td_configured = bool((TWELVEDATA_API_KEY_RUNTIME or os.getenv("TWELVEDATA_API_KEY") or "").strip())
    td_error = ""
    using_demo_data = False
    try:
        hist = ticker.history(period="1y", interval="1d", auto_adjust=False)
    except Exception as e:
        history_errors.append(f"yfinance_history_1y: {e}")

    if hist is None or hist.empty:
        try:
            attempted_sources.append("yfinance_history_6mo")
            hist = ticker.history(period="6mo", interval="1d", auto_adjust=False)
        except Exception as e:
            history_errors.append(f"yfinance_history_6mo: {e}")

    if hist is None or hist.empty:
        # Fallback path: yfinance download API
        try:
            attempted_sources.append("yfinance_download_6mo")
            fallback = yf.download(
                tickers=stock_symbol,
                period="6mo",
                interval="1d",
                progress=False,
                auto_adjust=False,
                group_by="column",
            )
            if isinstance(fallback, pd.DataFrame) and not fallback.empty:
                hist = fallback
        except Exception as e:
            history_errors.append(f"yfinance_download_6mo: {e}")

    if hist is None or hist.empty:
        # Fallback path: direct Yahoo chart API call
        try:
            attempted_sources.append("yahoo_chart_api_6mo")
            direct_hist = fetch_history_from_yahoo_chart(stock_symbol, range_value="6mo", interval="1d")
            if isinstance(direct_hist, pd.DataFrame) and not direct_hist.empty:
                hist = direct_hist
        except Exception as e:
            history_errors.append(f"yahoo_chart_api_6mo: {e}")

    if hist is None or hist.empty:
        # Fallback path: Stooq (free CSV endpoint)
        try:
            attempted_sources.append("stooq_csv")
            stooq_hist = fetch_history_from_stooq(stock_symbol)
            if isinstance(stooq_hist, pd.DataFrame) and not stooq_hist.empty:
                hist = stooq_hist
        except Exception as e:
            history_errors.append(f"stooq_csv: {e}")

    if hist is None or hist.empty:
        # Fallback path: Twelve Data (optional API key)
        try:
            attempted_sources.append("twelvedata_time_series")
            td_hist = fetch_history_from_twelvedata(stock_symbol)
            if isinstance(td_hist, pd.DataFrame) and not td_hist.empty:
                hist = td_hist
        except Exception as e:
            td_error = str(e)
            history_errors.append(f"twelvedata_time_series: {e}")

    if hist is None or hist.empty:
        error_hint = (
            "Unable to fetch data from available providers right now. "
            "Please try again in a minute."
        )
        if td_configured and td_error:
            error_hint = f"Twelve Data key appears invalid or blocked: {td_error}"
        if history_errors:
            combined = " | ".join(history_errors).lower()
            if "connect tunnel failed" in combined or "403" in combined:
                # Auto-fallback to synthetic data so the app remains usable offline/restricted.
                hist = generate_demo_history(stock_symbol, days=126)
                using_demo_data = True
            else:
                error_hint = (
                    "Network/provider blocked requests (HTTP 403) for market data endpoints. "
                    "Try another network/VPN or configure TWELVEDATA_API_KEY for a reliable fallback."
                )

        if hist is None or hist.empty:
            return {
                'error': f'No data found for symbol: {stock_symbol}. {error_hint}',
                'status_code': 404,
                'attempted_sources': attempted_sources,
            }

    closes = hist.get('Close')
    if closes is None or closes.dropna().empty:
        return {'error': f'No price data found for symbol: {stock_symbol}', 'status_code': 404}

    closes = closes.dropna()
    # Focus on roughly the last 6 months of trading sessions.
    closes = closes.tail(126)
    if closes.empty:
        return {'error': f'No recent price data found for symbol: {stock_symbol}', 'status_code': 404}

    start_price = float(closes.iloc[0])
    end_price = float(closes.iloc[-1])
    current_price = end_price

    growth_percentage = ((end_price - start_price) / start_price) * 100 if start_price else 0.0

    max_price = float(closes.max())
    min_price = float(closes.min())
    avg_price = float(closes.mean())
    volatility = float(closes.std()) if len(closes) > 1 else 0.0

    volatility_percentage = (volatility / avg_price) * 100 if avg_price > 0 else 0.0
    trend = detect_trend(hist)
    signal_action, signal_reasons = generate_signal(growth_percentage, volatility_percentage, trend)

    suggestions = generate_suggestion(growth_percentage, volatility, current_price, avg_price)

    usd_rates = get_usd_fx_rates()
    try:
        current_price_conv = convert_amount(current_price, native_currency, target_currency, usd_rates)
        start_price_conv = convert_amount(start_price, native_currency, target_currency, usd_rates)
        end_price_conv = convert_amount(end_price, native_currency, target_currency, usd_rates)
        max_price_conv = convert_amount(max_price, native_currency, target_currency, usd_rates)
        min_price_conv = convert_amount(min_price, native_currency, target_currency, usd_rates)
        avg_price_conv = convert_amount(avg_price, native_currency, target_currency, usd_rates)
        volatility_conv = convert_amount(volatility, native_currency, target_currency, usd_rates)
    except Exception:
        # If FX conversion fails, fall back to native currency amounts.
        target_currency = native_currency
        current_price_conv = current_price
        start_price_conv = start_price
        end_price_conv = end_price
        max_price_conv = max_price
        min_price_conv = min_price
        avg_price_conv = avg_price
        volatility_conv = volatility

    dates = []
    prices = []
    if include_chart:
        recent_data = hist.tail(30)
        try:
            dates = [date.strftime('%Y-%m-%d') for date in recent_data.index]
        except Exception:
            dates = []
        try:
            raw_prices = [float(x) for x in recent_data['Close'].tolist()]
            prices = [
                float(convert_amount(p, native_currency, target_currency, usd_rates))
                for p in raw_prices
            ]
        except Exception:
            prices = []

    return {
        'symbol': stock_symbol,
        'company_name': stock_info.get('longName', stock_symbol),
        'native_currency': native_currency,
        'currency': target_currency,
        'currency_symbol': CURRENCY_SYMBOLS.get(target_currency, '$'),
        'data_source': 'demo' if using_demo_data else 'live',
        'data_note': 'Using simulated demo market data due to provider/network restrictions.' if using_demo_data else '',
        'current_price': round(current_price_conv, 2),
        'start_price': round(start_price_conv, 2),
        'end_price': round(end_price_conv, 2),
        'growth_percentage': round(growth_percentage, 2),
        'max_price': round(max_price_conv, 2),
        'min_price': round(min_price_conv, 2),
        'avg_price': round(avg_price_conv, 2),
        'volatility': round(volatility_conv, 2),
        'volatility_percentage': round(volatility_percentage, 2),
        'trend': trend,
        'signal_action': signal_action,
        'signal_reasons': signal_reasons,
        'suggestions': suggestions,
        'dates': dates,
        'prices': prices,
    }


def position_suggestion(pnl_pct: float, signal_action: str) -> str:
    """A simple per-position suggestion that considers P/L + signal."""
    action = (signal_action or 'HOLD').upper()
    if pnl_pct >= 15 and action in {'SELL', 'HOLD'}:
        return "You’re up significantly — consider taking partial profits or tightening risk."
    if pnl_pct >= 8 and action == 'SELL':
        return "You’re in profit and the model leans SELL — consider booking gains."
    if pnl_pct <= -12 and action == 'SELL':
        return "You’re down and the model leans SELL — consider reducing exposure if risk is high."
    if pnl_pct <= -8 and action == 'BUY':
        return "Price is down but signal leans BUY — could be a dip-buy; manage risk carefully."
    return "Hold and monitor — keep an eye on trend changes and volatility."


@app.route('/portfolio/add', methods=['POST'])
def portfolio_add():
    payload = request.get_json(silent=True) or {}
    symbol = str(payload.get('symbol', '')).upper().strip()
    buy_price_raw = payload.get('buy_price', None)
    shares_raw = payload.get('shares', 1)
    buy_currency = normalize_currency(payload.get('currency'))

    if not symbol:
        return api_error('Please enter a stock symbol', 400)
    try:
        buy_price = float(buy_price_raw)
        if buy_price <= 0:
            raise ValueError("buy_price must be > 0")
    except Exception:
        return api_error('Please enter a valid buy price (number > 0).', 400, symbol)

    try:
        shares = float(shares_raw)
        if shares <= 0:
            raise ValueError("shares must be > 0")
    except Exception:
        return api_error('Please enter a valid quantity/shares (number > 0).', 400, symbol)

    PORTFOLIO[symbol] = {'buy_price': buy_price, 'buy_currency': buy_currency, 'shares': shares}
    return jsonify({'ok': True, 'symbol': symbol})


@app.route('/portfolio/remove', methods=['POST'])
def portfolio_remove():
    payload = request.get_json(silent=True) or {}
    symbol = str(payload.get('symbol', '')).upper().strip()
    if not symbol:
        return api_error('Please provide a symbol to remove.', 400)
    if symbol in PORTFOLIO:
        del PORTFOLIO[symbol]
    return jsonify({'ok': True, 'symbol': symbol})


@app.route('/portfolio/list', methods=['GET'])
def portfolio_list():
    target_currency = normalize_currency(request.args.get('currency'))
    usd_rates = get_usd_fx_rates()
    items = []
    total_cost = 0.0
    total_value = 0.0

    for symbol, pos in sorted(PORTFOLIO.items()):
        buy_price = float(pos.get('buy_price', 0.0))
        buy_ccy = normalize_currency(pos.get('buy_currency'))
        shares = float(pos.get('shares', 1.0))

        analysis = analyze_stock(symbol, include_chart=False, target_currency=target_currency)
        if analysis.get('error'):
            items.append({
                'symbol': symbol,
                'buy_price': round(convert_amount(buy_price, buy_ccy, target_currency, usd_rates), 2) if buy_price else 0.0,
                'buy_currency': target_currency,
                'shares': round(shares, 4),
                'error': analysis['error'],
                'current_price': None,
                'pnl_amount': None,
                'pnl_percent': None,
                'signal_action': 'HOLD',
                'signal_reasons': [],
                'position_suggestion': 'Unable to fetch live price right now.',
            })
            continue

        current_price = float(analysis.get('current_price') or 0.0)
        buy_price_target = convert_amount(buy_price, buy_ccy, target_currency, usd_rates) if buy_price else 0.0
        cost = buy_price_target * shares
        value = current_price * shares
        pnl_amount = value - cost
        pnl_percent = ((current_price - buy_price_target) / buy_price_target) * 100 if buy_price_target else 0.0

        total_cost += cost
        total_value += value

        signal_action = analysis.get('signal_action', 'HOLD')

        items.append({
            'symbol': symbol,
            'company_name': analysis.get('company_name', symbol),
            'buy_price': round(buy_price_target, 2),
            'buy_currency': target_currency,
            'shares': round(shares, 4),
            'current_price': round(current_price, 2),
            'pnl_amount': round(pnl_amount, 2),
            'pnl_percent': round(pnl_percent, 2),
            'signal_action': signal_action,
            'signal_reasons': analysis.get('signal_reasons', []),
            'position_suggestion': position_suggestion(pnl_percent, signal_action),
        })

    total_pnl_amount = total_value - total_cost
    total_pnl_percent = (total_pnl_amount / total_cost) * 100 if total_cost else 0.0

    return jsonify({
        'currency': target_currency,
        'currency_symbol': CURRENCY_SYMBOLS.get(target_currency, '$'),
        'items': items,
        'totals': {
            'cost': round(total_cost, 2),
            'value': round(total_value, 2),
            'pnl_amount': round(total_pnl_amount, 2),
            'pnl_percent': round(total_pnl_percent, 2),
        }
    })


def detect_trend(hist) -> str:
    """
    Detect simple trend using moving averages on recent closes.
    Returns: 'up', 'down', or 'sideways'
    """
    try:
        closes = hist['Close'].dropna()
        if len(closes) < 35:
            return 'sideways'

        recent = closes.tail(35)
        sma10 = recent.rolling(window=10).mean().iloc[-1]
        sma30 = recent.rolling(window=30).mean().iloc[-1]
        if sma10 != sma10 or sma30 != sma30 or sma30 == 0:  # NaN checks
            return 'sideways'

        ratio = sma10 / sma30
        if ratio > 1.01:
            return 'up'
        if ratio < 0.99:
            return 'down'
        return 'sideways'
    except Exception:
        return 'sideways'


def generate_signal(growth_percentage: float, volatility_percentage: float, trend: str):
    """
    Return (action, reasons) where action is BUY/SELL/HOLD.
    This is a simple heuristic (not financial advice).
    """
    reasons = []

    # Growth bucket
    if growth_percentage >= 12:
        reasons.append(f"Strong 6-month growth (+{growth_percentage:.1f}%).")
        growth_score = 2
    elif growth_percentage >= 4:
        reasons.append(f"Moderate 6-month growth (+{growth_percentage:.1f}%).")
        growth_score = 1
    elif growth_percentage <= -12:
        reasons.append(f"Strong 6-month decline ({growth_percentage:.1f}%).")
        growth_score = -2
    elif growth_percentage <= -4:
        reasons.append(f"Moderate 6-month decline ({growth_percentage:.1f}%).")
        growth_score = -1
    else:
        reasons.append(f"Flat 6-month performance ({growth_percentage:.1f}%).")
        growth_score = 0

    # Volatility bucket
    if volatility_percentage >= 12:
        reasons.append(f"High volatility (~{volatility_percentage:.1f}%).")
        vol_penalty = -2
    elif volatility_percentage >= 7:
        reasons.append(f"Moderate volatility (~{volatility_percentage:.1f}%).")
        vol_penalty = -1
    else:
        reasons.append(f"Low volatility (~{volatility_percentage:.1f}%).")
        vol_penalty = 0

    # Trend bucket
    if trend == 'up':
        reasons.append("Recent trend is upward (short-term MA above long-term MA).")
        trend_score = 2
    elif trend == 'down':
        reasons.append("Recent trend is downward (short-term MA below long-term MA).")
        trend_score = -2
    else:
        reasons.append("Recent trend is sideways (moving averages are close).")
        trend_score = 0

    # Combine scores
    score = growth_score + trend_score + vol_penalty

    # Hard guards for very high volatility
    if volatility_percentage >= 18 and abs(growth_percentage) < 20:
        return "HOLD", reasons + ["Very high volatility: prefer waiting for clearer price action."]

    if score >= 3:
        return "BUY", reasons + ["Signal: BUY (growth + trend outweigh risk)."]
    if score <= -3:
        return "SELL", reasons + ["Signal: SELL (decline + downtrend outweigh potential upside)."]
    return "HOLD", reasons + ["Signal: HOLD (mixed signals or insufficient edge)."]

if __name__ == '__main__':
    app.run(debug=True)
