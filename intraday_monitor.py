from datetime import datetime, timezone, timedelta
import os
import sqlite3
import pandas as pd
import requests

DB_PATH = 'tw_stock_history.db'
TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN', '你的_BOT_TOKEN')
TG_CHAT_ID = os.getenv('TG_CHAT_ID', '你的_CHAT_ID')

# 防接刀緩衝設定：盤中現價不可低於建議進場價超過 1.5% (0.985)
MAX_SLIPPAGE_RATIO = 0.985


def get_tw_now() -> datetime:
    """取得標準台灣時間 (UTC+8)，避免雲端伺服器時區差異"""
    tw_tz = timezone(timedelta(hours=8))
    return datetime.now(tw_tz)


def is_trading_hours(tw_now: datetime) -> bool:
    """檢查目前是否為台股盤中交易時段 (週一至週五 09:00 ~ 13:35)"""
    if tw_now.weekday() >= 5:
        return False
    current_hm = tw_now.hour * 100 + tw_now.minute
    return 900 <= current_hm <= 1335


def send_telegram_message(message: str):
    if not TG_BOT_TOKEN or TG_BOT_TOKEN == '你的_BOT_TOKEN' or not TG_CHAT_ID or TG_CHAT_ID == '你的_CHAT_ID':
        print(message)
        return
    url = f'https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage'
    try:
        res = requests.post(
            url,
            json={
                'chat_id': TG_CHAT_ID,
                'text': message,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            },
            timeout=10,
        )
        if res.status_code == 200:
            print('✅ Telegram 盤中通知發送成功！')
        else:
            print(f'❌ Telegram 發送失敗: {res.text}')
    except Exception as e:
        print(f'❌ 發送過程發生錯誤: {e}')


def fetch_realtime_quotes(stock_ids: list) -> dict:
    """透過證交所 MIS 即時報價接口抓取觀察名單最新成交價"""
    if not stock_ids:
        return {}
    ex_ch = '|'.join([f'tse_{sid}.tw' for sid in stock_ids])
    url = f'https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0'
    headers = {'User-Agent': 'Mozilla/5.0'}

    try:
        res = requests.get(url, headers=headers, timeout=10).json()
    except Exception as e:
        print(f'即時報價連線失敗: {e}')
        return {}

    quotes = {}
    for item in res.get('msgArray', []):
        sid = item.get('c')
        price_str = item.get('z')
        if not price_str or price_str == '-':
            b_prices = item.get('b', '').split('_')
            price_str = b_prices[0] if b_prices and b_prices[0] else item.get('y')
        try:
            quotes[sid] = float(price_str)
        except (ValueError, TypeError):
            continue
    return quotes


def check_entry_alerts():
    """檢查觀察名單中狀態為 PENDING 的標的，是否落在安全進場區間內"""
    tw_now = get_tw_now()
    now_str = tw_now.strftime('%Y-%m-%d %H:%M')

    # 1. 時間鎖定保護：非台股交易時段 (09:00~13:35) 直接略過，防止盤後誤觸發
    if not is_trading_hours(tw_now):
        print(f'[{now_str}] 目前非台股盤中交易時段 (09:00~13:35)，略過盤中監控。')
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watchlist_signals (
            signal_date TEXT,
            stock_id TEXT,
            stock_name TEXT,
            signal_close REAL,
            suggested_entry REAL,
            stop_loss REAL,
            status TEXT DEFAULT 'PENDING',
            triggered_date TEXT,
            triggered_price REAL,
            PRIMARY KEY (signal_date, stock_id)
        )
    """)

    df_pending = pd.read_sql_query(
        """
        SELECT * FROM watchlist_signals 
        WHERE status = 'PENDING' 
        ORDER BY signal_date DESC
        """,
        conn,
    )

    if df_pending.empty:
        print(f'[{now_str}] 目前無待監控的觀察標的。')
        conn.close()
        return

    stock_ids = df_pending['stock_id'].unique().tolist()
    quotes = fetch_realtime_quotes(stock_ids)

    triggered_alerts = []
    expired_count = 0

    for _, row in df_pending.iterrows():
        sid = row['stock_id']
        curr_price = quotes.get(sid)
        if curr_price is None:
            continue

        entry_price = float(row['suggested_entry'])
        stop_loss = float(row['stop_loss'])
        min_safe_entry = entry_price * MAX_SLIPPAGE_RATIO  # 最低可接受進場價 (不低於進場價 1.5%)

        safe_name = (
            str(row['stock_name'])
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
        )

        # 情況 A：盤中跳空或急殺跌破安全進場區間 (低於進場價 1.5% 以上或跌破停損) -> 標記為 INVALID 放棄接刀
        if curr_price < min_safe_entry or curr_price <= stop_loss:
            cursor.execute(
                """
                UPDATE watchlist_signals 
                SET status = 'INVALID', triggered_date = ?, triggered_price = ?
                WHERE signal_date = ? AND stock_id = ?
                """,
                (now_str, curr_price, row['signal_date'], sid),
            )
            expired_count += 1
            print(f'⚠️ {sid} {safe_name} 現價 {curr_price} 跌破安全接單區間 ({min_safe_entry:.2f})，已自動作廢避免接刀。')
            continue

        # 情況 B：盤中現價精準落在「建議進場價」與「安全下限 (-1.5%)」之間 -> 發送買進達標通知
        if min_safe_entry <= curr_price <= entry_price:
            cursor.execute(
                """
                UPDATE watchlist_signals 
                SET status = 'TRIGGERED', triggered_date = ?, triggered_price = ?
                WHERE signal_date = ? AND stock_id = ?
                """,
                (now_str, curr_price, row['signal_date'], sid),
            )
            triggered_alerts.append(
                f'🚨 <b>【安全進場價達標】{sid} {safe_name}</b>\n'
                f'   盤中現價: <code>{curr_price}</code> (建議進場: <code>{entry_price}</code>)\n'
                f'   防守停損: <code>{stop_loss}</code> | 訊號日: <code>{row["signal_date"]}</code>\n'
                f'   👉 <a href="https://tw.stock.yahoo.com/quote/{sid}">立即看盤</a>'
            )

    conn.commit()
    conn.close()

    if triggered_alerts:
        msg = f'🔔 <b>盤中到價監控通知 ({now_str})</b>\n\n' + '\n\n'.join(triggered_alerts)
        send_telegram_message(msg)
        print(f'✅ 已發送 {len(triggered_alerts)} 筆到價提醒！(另有 {expired_count} 檔因急跌作廢)')
    else:
        print(f'[{now_str}] 監控中 ({len(stock_ids)} 檔)，尚未觸及安全進場區間 (作廢 {expired_count} 檔)。')


if __name__ == '__main__':
    check_entry_alerts()
