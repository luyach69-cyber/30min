from datetime import datetime, timedelta
import os
import sqlite3
import time
import pandas as pd
import requests

# ==========================================
# 1. 全域設定區 (自動讀取 GitHub Secrets 環境變數)
# ==========================================
DB_PATH = 'tw_stock_history.db'
TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN', '你的_BOT_TOKEN')
TG_CHAT_ID = os.getenv('TG_CHAT_ID', '你的_CHAT_ID')

# 選股參數設定
LOOKBACK_DAYS = 3        # 觀察前期連跌與賣超的天數 (預設 3 天)
MIN_BUY_VOL = 500        # 轉買當日最低買超張數門檻 (張)
MIN_DAILY_VOL = 1000     # 最低成交量門檻，過濾冷門股 (張)
MIN_INST_RATIO = 0.15    # 籌碼集中度門檻：法人合計買超佔當日成交量比例 (0.15 = 15%)


# ==========================================
# 2. 資料庫初始化與結構升級模組
# ==========================================
def init_db():
    """初始化 SQLite 資料庫，包含歷史價量籌碼表與觀察追蹤紀錄表"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 表 1：全市場每日歷史價量與籌碼
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_chip_price (
            stock_id TEXT,
            stock_name TEXT,
            date TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            foreign_net REAL,
            trust_net REAL,
            total_inst_net REAL,
            PRIMARY KEY (stock_id, date)
        )
    """)
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_stock_date ON daily_chip_price (stock_id, date)'
    )

    # 表 2：盤後雷達選出標的與建議進場價追蹤表
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

    # 舊版資料庫相容性自動升級
    existing_cols = [
        row[1] for row in cursor.execute("PRAGMA table_info(daily_chip_price)").fetchall()
    ]
    for col, col_type in [
        ('stock_name', 'TEXT'),
        ('open', 'REAL'),
        ('high', 'REAL'),
        ('low', 'REAL'),
    ]:
        if col not in existing_cols:
            cursor.execute(f'ALTER TABLE daily_chip_price ADD COLUMN {col} {col_type}')

    conn.commit()
    conn.close()


def get_db_day_count() -> int:
    """查詢目前資料庫內已累積幾個交易日的資料"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(DISTINCT date) FROM daily_chip_price')
    count = cursor.fetchone()[0]
    conn.close()
    return count


# ==========================================
# 3. 盤後資料抓取與寫入模組
# ==========================================
def fetch_and_save_daily_market(date_str: str) -> bool:
    """抓取指定日期的全市場上市股票「完整OHLC價量、中文名稱」與「三大法人」並存入 SQLite"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    formatted_date = f'{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}'

    # --- A. 抓取全市場當日開高低收與成交量 (MI_INDEX) ---
    url_price = f'https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={date_str}&type=ALLBUT0999&response=json'
    try:
        res_price = requests.get(url_price, headers=headers, timeout=15).json()
    except Exception as e:
        print(f'[{formatted_date}] 抓取收盤價連線失敗: {e}')
        return False

    if res_price.get('stat') != 'OK':
        print(f'[{formatted_date}] 無收盤價資料（可能為假日或尚未公布）')
        return False

    price_table = None
    for table in res_price.get('tables', []):
        if '每日收盤行情' in table.get('title', ''):
            price_table = pd.DataFrame(table['data'], columns=table['fields'])
            break

    if price_table is None:
        print(f'[{formatted_date}] 找不到每日收盤行情表')
        return False

    # 只保留 4 碼純數字普通股
    price_table = price_table[price_table['證券代號'].str.match(r'^\d{4}$')].copy()
    price_table['stock_name'] = price_table['證券名稱'].str.strip()

    for orig_col, new_col in [
        ('開盤價', 'open'),
        ('最高價', 'high'),
        ('最低價', 'low'),
        ('收盤價', 'close'),
    ]:
        price_table[new_col] = pd.to_numeric(
            price_table[orig_col].str.replace(',', ''), errors='coerce'
        )

    price_table['volume'] = (
        pd.to_numeric(price_table['成交股數'].str.replace(',', ''), errors='coerce') / 1000
    ).round(1)

    df_price = price_table[
        ['證券代號', 'stock_name', 'open', 'high', 'low', 'close', 'volume']
    ].rename(columns={'證券代號': 'stock_id'})

    time.sleep(2.5)

    # --- B. 抓取全市場當日三大法人買賣超 (T86) ---
    url_inst = f'https://www.twse.com.tw/rwd/zh/fund/T86?date={date_str}&selectType=ALLBUT0999&response=json'
    try:
        res_inst = requests.get(url_inst, headers=headers, timeout=15).json()
    except Exception as e:
        print(f'[{formatted_date}] 抓取三大法人連線失敗: {e}')
        return False

    if res_inst.get('stat') != 'OK':
        print(f'[{formatted_date}] 無三大法人資料（可能尚未公布）')
        return False

    inst_table = pd.DataFrame(res_inst['data'], columns=res_inst['fields'])
    inst_table = inst_table[inst_table['證券代號'].str.match(r'^\d{4}$')].copy()

    for col, new_name in [
        ('外陸資買賣超股數(不含外資自營商)', 'foreign_net'),
        ('投信買賣超股數', 'trust_net'),
        ('三大法人買賣超股數', 'total_inst_net'),
    ]:
        inst_table[new_name] = (
            pd.to_numeric(inst_table[col].str.replace(',', ''), errors='coerce') / 1000
        ).round(1)

    df_inst = inst_table[
        ['證券代號', 'foreign_net', 'trust_net', 'total_inst_net']
    ].rename(columns={'證券代號': 'stock_id'})

    # --- C. 合併並寫入 SQLite ---
    df_merged = pd.merge(df_price, df_inst, on='stock_id', how='inner')
    df_merged['date'] = formatted_date
    df_merged = df_merged.dropna(subset=['close'])

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    records = df_merged[[
        'stock_id', 'stock_name', 'date', 'open', 'high', 'low',
        'close', 'volume', 'foreign_net', 'trust_net', 'total_inst_net'
    ]].values.tolist()

    cursor.executemany(
        """
        INSERT OR REPLACE INTO daily_chip_price 
        (stock_id, stock_name, date, open, high, low, close, volume, foreign_net, trust_net, total_inst_net)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        records,
    )
    conn.commit()
    conn.close()
    print(f'[{formatted_date}] 成功寫入 {len(records)} 檔股票資料至 SQLite！')
    return True


def backfill_history(days_back: int = 10):
    """自動往回抓取過去 N 個日曆天的交易日資料以建立基礎歷史庫"""
    print(f'🔄 偵測到歷史資料不足，自動開始回補過去 {days_back} 天內的交易日資料...')
    today = datetime.now()
    for i in range(days_back, 0, -1):
        target_date = today - timedelta(days=i)
        if target_date.weekday() >= 5:
            continue
        date_str = target_date.strftime('%Y%m%d')
        fetch_and_save_daily_market(date_str)
        time.sleep(3)
    print('✅ 歷史資料回補完成！\n')


# ==========================================
# 4. 台股跳動單位 (Tick Size) 進位修正工具
# ==========================================
def round_to_tw_tick(price: float) -> float:
    """將計算出的建議進場價對齊台股合法升降單位"""
    if price < 10:
        tick = 0.01
    elif price < 50:
        tick = 0.05
    elif price < 100:
        tick = 0.1
    elif price < 500:
        tick = 0.5
    elif price < 1000:
        tick = 1.0
    else:
        tick = 5.0
    return round(round(price / tick) * tick, 2)


# ==========================================
# 5. 全市場選股篩選與建議進場價計算引擎
# ==========================================
def run_market_screener(
    lookback_days: int = LOOKBACK_DAYS,
    min_buy_vol: int = MIN_BUY_VOL,
    min_daily_vol: int = MIN_DAILY_VOL,
    min_inst_ratio: float = MIN_INST_RATIO,
) -> pd.DataFrame:
    """從本地 SQLite 讀取最新交易日資料，執行選股並計算建議進場價與停損參考"""
    conn = sqlite3.connect(DB_PATH)
    recent_dates_df = pd.read_sql_query(
        f"""
        SELECT DISTINCT date FROM daily_chip_price 
        ORDER BY date DESC LIMIT {lookback_days + 1}
        """,
        conn,
    )

    if len(recent_dates_df) < lookback_days + 1:
        conn.close()
        return pd.DataFrame()

    min_date = recent_dates_df['date'].min()
    df_all = pd.read_sql_query(
        f"""
        SELECT * FROM daily_chip_price 
        WHERE date >= '{min_date}'
        ORDER BY stock_id, date ASC
        """,
        conn,
    )

    selected_stocks = []
    for stock_id, group in df_all.groupby('stock_id'):
        if len(group) < lookback_days + 1:
            continue

        group = group.reset_index(drop=True)
        prev_days = group.iloc[:-1]
        today = group.iloc[-1]

        price_falling = (
            prev_days['close'].iloc[-1] < prev_days['close'].iloc[0]
        ) and prev_days['close'].is_monotonic_decreasing
        price_stabilized = today['close'] >= prev_days['close'].iloc[-1]
        prev_inst_sell = (prev_days['foreign_net'].sum() < 0) and (
            prev_days['total_inst_net'].sum() < 0
        )
        today_inst_buy = (today['foreign_net'] >= min_buy_vol) and (
            today['total_inst_net'] >= min_buy_vol
        )
        liquid_enough = today['volume'] >= min_daily_vol

        # 新增條件 6：籌碼集中度過濾 (法人買超佔今日成交量 >= 15%，過濾牛皮大型股)
        inst_ratio = (today['total_inst_net'] / today['volume']) if today['volume'] > 0 else 0
        high_concentration = inst_ratio >= min_inst_ratio

        if all([price_falling, price_stabilized, prev_inst_sell, today_inst_buy, liquid_enough, high_concentration]):
            close_p = float(today['close'])
            open_p = float(today['open']) if pd.notna(today['open']) else close_p
            low_p = float(today['low']) if pd.notna(today['low']) else close_p

            pullback_1pct = close_p * 0.99
            k_midpoint = (open_p + close_p) / 2 if close_p > open_p else close_p * 0.995
            suggested_entry = round_to_tw_tick(min(pullback_1pct, k_midpoint))

            period_low = min(low_p, float(prev_days['low'].min())) if pd.notna(prev_days['low'].min()) else close_p * 0.95
            stop_loss = round_to_tw_tick(period_low * 0.98)

            stock_name = today['stock_name'] if pd.notna(today['stock_name']) else ''

            selected_stocks.append({
                'stock_id': stock_id,
                'stock_name': stock_name,
                'date': today['date'],
                'close': close_p,
                'suggested_entry': suggested_entry,
                'stop_loss': stop_loss,
                'foreign_net': today['foreign_net'],
                'total_inst_net': today['total_inst_net'],
                'volume': today['volume'],
                'inst_ratio_pct': round(inst_ratio * 100, 1),
            })

    if selected_stocks:
        cursor = conn.cursor()
        for item in selected_stocks:
            cursor.execute(
                """
                INSERT OR IGNORE INTO watchlist_signals
                (signal_date, stock_id, stock_name, signal_close, suggested_entry, stop_loss, status)
                VALUES (?, ?, ?, ?, ?, ?, 'PENDING')
                """,
                (
                    item['date'],
                    item['stock_id'],
                    item['stock_name'],
                    item['close'],
                    item['suggested_entry'],
                    item['stop_loss'],
                ),
            )
        conn.commit()

    conn.close()
    return pd.DataFrame(selected_stocks)


# ==========================================
# 6. 戰報格式化與 Telegram 推播模組 (HTML 模式)
# ==========================================
def format_screener_alert(df_hits: pd.DataFrame, date_str: str) -> str:
    """將篩選出的股票 DataFrame 轉換為包含中文名稱與建議進場價的戰報格式"""
    if df_hits.empty:
        return (
            f'📊 <b>【{date_str} 盤後起漲雷達】</b>\n'
            '今日無符合「連跌止跌＋法人轉買＋集中度≥15%」條件之標的。'
        )

    lines = [
        f'🎯 <b>【{date_str} 盤後起漲雷達】</b>',
        f'共篩選出 <b>{len(df_hits)}</b> 檔高集中度籌碼轉折標的（已加入盤中監控）：\n',
    ]

    for _, row in df_hits.iterrows():
        stock_id = row['stock_id']
        name = (
            str(row['stock_name'])
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
        )
        close = row['close']
        entry = row['suggested_entry']
        sl = row['stop_loss']
        foreign = int(row['foreign_net'])
        total_inst = int(row['total_inst_net'])
        vol = int(row['volume'])
        ratio_pct = row['inst_ratio_pct']

        item_text = (
            f'🔹 <b>{stock_id} {name}</b> | 收盤: <code>{close}</code>\n'
            f'   💡 建議進場: <code>{entry}</code> | 防守停損: <code>{sl}</code>\n'
            f'   外資: <code>+{foreign}張</code> | 法人合計: <code>+{total_inst}張</code> (佔量 <code>{ratio_pct}%</code>)\n'
            f'   成交量: <code>{vol}張</code> | 👉 <a href="https://tw.stock.yahoo.com/quote/{stock_id}">查看YahooK線</a>'
        )
        lines.append(item_text)

    return '\n'.join(lines)


def send_telegram_message(message: str):
    """發送文字訊息至指定的 Telegram 聊天室"""
    if not TG_BOT_TOKEN or TG_BOT_TOKEN == '你的_BOT_TOKEN' or not TG_CHAT_ID or TG_CHAT_ID == '你的_CHAT_ID':
        print('⚠️ 尚未設定 Telegram Token 或 Chat ID，略過推播。訊息預覽如下：\n')
        print(message)
        return

    url = f'https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage'
    payload = {
        'chat_id': TG_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True,
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            print('✅ Telegram 通知發送成功！')
        else:
            print(f'❌ Telegram 發送失敗: {res.text}')
    except Exception as e:
        print(f'❌ 發送過程發生錯誤: {e}')


# ==========================================
# 7. 主程式進入點
# ==========================================
if __name__ == '__main__':
    init_db()

    conn = sqlite3.connect(DB_PATH)
    missing_names = pd.read_sql_query(
        'SELECT COUNT(*) as c FROM daily_chip_price WHERE stock_name IS NULL OR stock_name = ""',
        conn,
    )['c'].iloc[0]
    conn.close()

    if get_db_day_count() < LOOKBACK_DAYS + 1 or missing_names > 0:
        backfill_history(days_back=10)

    today_str = datetime.now().strftime('%Y%m%d')
    fetch_and_save_daily_market(today_str)

    if get_db_day_count() >= LOOKBACK_DAYS + 1:
        conn = sqlite3.connect(DB_PATH)
        latest_date = pd.read_sql_query('SELECT MAX(date) as d FROM daily_chip_price', conn)['d'].iloc[0]
        conn.close()

        hits = run_market_screener()
        alert_msg = format_screener_alert(hits, latest_date)
        send_telegram_message(alert_msg)
