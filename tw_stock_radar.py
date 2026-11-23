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
LOOKBACK_DAYS = 3  # 觀察前期連跌與賣超的天數 (預設 3 天)
MIN_BUY_VOL = 500  # 轉買當日最低買超張數門檻 (張)
MIN_DAILY_VOL = 1000  # 最低成交量門檻，過濾冷門股 (張)


# ==========================================
# 2. 資料庫初始化與狀態檢查模組
# ==========================================
def init_db():
  """初始化 SQLite 資料庫與資料表"""
  conn = sqlite3.connect(DB_PATH)
  cursor = conn.cursor()
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_chip_price (
            stock_id TEXT,
            date TEXT,
            close REAL,
            volume REAL,
            foreign_net REAL,
            trust_net REAL,
            total_inst_net REAL,
            PRIMARY KEY (stock_id, date)
        )
    """)
  cursor.execute(
      'CREATE INDEX IF NOT EXISTS idx_stock_date ON daily_chip_price (stock_id,'
      ' date)'
  )
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
  """抓取指定日期的全市場上市股票「價量」與「三大法人」並存入 SQLite

  參數: date_str 格式為 'YYYYMMDD' (例如 '20261123')
  回傳: True 代表成功寫入，False 代表當日無資料（假日或尚未公布）
  """
  headers = {
      'User-Agent': (
          'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
      )
  }
  formatted_date = f'{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}'

  # --- A. 抓取全市場當日收盤價與成交量 (MI_INDEX) ---
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
  price_table = price_table[
      price_table['證券代號'].str.match(r'^\d{4}$')
  ].copy()
  price_table['close'] = pd.to_numeric(
      price_table['收盤價'].str.replace(',', ''), errors='coerce'
  )
  price_table['volume'] = (
      pd.to_numeric(price_table['成交股數'].str.replace(',', ''), errors='coerce')
      / 1000
  ).round(1)
  df_price = price_table[['證券代號', 'close', 'volume']].rename(
      columns={'證券代號': 'stock_id'}
  )

  # 禮貌性暫停避免被證交所阻擋
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
        pd.to_numeric(inst_table[col].str.replace(',', ''), errors='coerce')
        / 1000
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
      'stock_id',
      'date',
      'close',
      'volume',
      'foreign_net',
      'trust_net',
      'total_inst_net',
  ]].values.tolist()

  cursor.executemany(
      """
        INSERT OR REPLACE INTO daily_chip_price 
        (stock_id, date, close, volume, foreign_net, trust_net, total_inst_net)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
      records,
  )
  conn.commit()
  conn.close()
  print(
      f'[{formatted_date}] 成功寫入 {len(records)} 檔股票資料至 SQLite！'
  )
  return True


def backfill_history(days_back: int = 10):
  """自動往回抓取過去 N 個日曆天的交易日資料以建立基礎歷史庫"""
  print(f'🔄 偵測到歷史資料不足，自動開始回補過去 {days_back} 天內的交易日資料...')
  today = datetime.now()
  for i in range(days_back, 0, -1):
    target_date = today - timedelta(days=i)
    # 跳過週六(5)、週日(6)
    if target_date.weekday() >= 5:
      continue
    date_str = target_date.strftime('%Y%m%d')
    fetch_and_save_daily_market(date_str)
    time.sleep(3)
  print('✅ 歷史資料回補完成！\n')


# ==========================================
# 4. 全市場選股篩選引擎
# ==========================================
def run_market_screener(
    lookback_days: int = LOOKBACK_DAYS,
    min_buy_vol: int = MIN_BUY_VOL,
    min_daily_vol: int = MIN_DAILY_VOL,
) -> pd.DataFrame:
  """從本地 SQLite 讀取最新交易日資料並執行選股邏輯"""
  conn = sqlite3.connect(DB_PATH)
  recent_dates_df = pd.read_sql_query(
      f"""
        SELECT DISTINCT date FROM daily_chip_price 
        ORDER BY date DESC LIMIT {lookback_days + 1}
        """,
      conn,
  )

  if len(recent_dates_df) < lookback_days + 1:
    print(
        f'⚠️ 資料庫目前僅有 {len(recent_dates_df)} 天資料，至少需要'
        f' {lookback_days + 1} 天才能執行比對！'
    )
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
  conn.close()

  selected_stocks = []
  for stock_id, group in df_all.groupby('stock_id'):
    if len(group) < lookback_days + 1:
      continue

    group = group.reset_index(drop=True)
    prev_days = group.iloc[:-1]
    today = group.iloc[-1]

    # 1. 前期股價連續下跌
    price_falling = (
        prev_days['close'].iloc[-1] < prev_days['close'].iloc[0]
    ) and prev_days['close'].is_monotonic_decreasing

    # 2. 今日股價止跌 (收盤價 >= 昨日收盤價)
    price_stabilized = today['close'] >= prev_days['close'].iloc[-1]

    # 3. 前期外資與三大法人累積賣超
    prev_inst_sell = (prev_days['foreign_net'].sum() < 0) and (
        prev_days['total_inst_net'].sum() < 0
    )

    # 4. 今日外資與三大法人同步轉買且達門檻
    today_inst_buy = (today['foreign_net'] >= min_buy_vol) and (
        today['total_inst_net'] >= min_buy_vol
    )

    # 5. 流動性過濾
    liquid_enough = today['volume'] >= min_daily_vol

    if all([
        price_falling,
        price_stabilized,
        prev_inst_sell,
        today_inst_buy,
        liquid_enough,
    ]):
      selected_stocks.append({
          'stock_id': stock_id,
          'date': today['date'],
          'close': today['close'],
          'foreign_net': today['foreign_net'],
          'total_inst_net': today['total_inst_net'],
          'volume': today['volume'],
      })

  return pd.DataFrame(selected_stocks)


# ==========================================
# 5. Telegram 戰報格式化與推播模組
# ==========================================
def format_screener_alert(df_hits: pd.DataFrame, date_str: str) -> str:
  """將篩選出的股票 DataFrame 轉換為手機好閱讀的戰報格式"""
  if df_hits.empty:
    return f'📊 *【{date_str} 盤後起漲雷達】*\n今日無符合「連跌止跌＋法人轉買」條件之標的。'

  lines = [
      f'🎯 *【{date_str} 盤後起漲雷達】*',
      f'共篩選出 *{len(df_hits)}* 檔籌碼轉折標的：\n',
  ]

  for _, row in df_hits.iterrows():
    stock_id = row['stock_id']
    close = row['close']
    foreign = int(row['foreign_net'])
    total_inst = int(row['total_inst_net'])
    vol = int(row['volume'])

    item_text = (
        f'🔹 *{stock_id}* | 收盤: `{close}`\n'
        f'   外資: `+{foreign}張` | 法人合計: `+{total_inst}張`\n'
        f'   成交量: `{vol}張` ([查看K線](https://tw.stock.yahoo.com/quote/{stock_id}))'
    )
    lines.append(item_text)

  return '\n'.join(lines)


def send_telegram_message(message: str):
  """發送文字訊息至指定的 Telegram 聊天室"""
  if (
      not TG_BOT_TOKEN
      or TG_BOT_TOKEN == '你的_BOT_TOKEN'
      or not TG_CHAT_ID
      or TG_CHAT_ID == '你的_CHAT_ID'
  ):
    print('⚠️ 尚未設定 Telegram Token 或 Chat ID，略過推播。訊息預覽如下：\n')
    print(message)
    return

  url = f'https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage'
  payload = {
      'chat_id': TG_CHAT_ID,
      'text': message,
      'parse_mode': 'Markdown',
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
# 6. 主程式進入點
# ==========================================
if __name__ == '__main__':
  init_db()

  # 若資料庫天數不足 4 天，自動回補過去 10 天內的交易日資料
  if get_db_day_count() < LOOKBACK_DAYS + 1:
    backfill_history(days_back=10)

  today_str = datetime.now().strftime('%Y%m%d')
  has_data = fetch_and_save_daily_market(today_str)

  # 只要資料庫天數足夠，就執行選股與推播 (方便手動測試時也能收到戰報)
  if get_db_day_count() >= LOOKBACK_DAYS + 1:
    # 取得資料庫最新一個交易日作為戰報標題日期
    conn = sqlite3.connect(DB_PATH)
    latest_date = pd.read_sql_query(
        'SELECT MAX(date) as d FROM daily_chip_price', conn
    )['d'].iloc[0]
    conn.close()

    hits = run_market_screener()
    alert_msg = format_screener_alert(hits, latest_date)
    send_telegram_message(alert_msg)
