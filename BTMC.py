import re
import requests
import json
import os
import logging
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('gold_crawler')

# Biến toàn cục để lưu trữ dữ liệu giá vàng mới nhất
current_gold_data = []
HISTORY_FILE = "btmc_history.json"
HISTORY_DAYS = 7

# Khai báo class GoldPriceScheduler để quản lý việc lên lịch tự động
class GoldPriceScheduler:
    def __init__(self, crawl_function, update_function):
        self.scheduler = BackgroundScheduler()
        self.crawl_function = crawl_function
        self.update_function = update_function
        self.running = False

    def start(self):
        if not self.running:
            # Lên lịch chạy hàng ngày vào các thời điểm: 8h sáng, 12h trưa, 16h chiều, 20h tối
            self.scheduler.add_job(
                self._fetch_and_update_data,
                CronTrigger(hour='8,12,16,20'),
                id='fetch_gold_price_scheduled',
                name='Tự động cào dữ liệu giá vàng theo lịch'
            )

            # Thêm job chạy ngay khi khởi động ứng dụng
            self.scheduler.add_job(
                self._fetch_and_update_data,
                id='fetch_gold_price_startup',
                name='Cào dữ liệu giá vàng khi khởi động'
            )

            self.scheduler.start()
            self.running = True
            logger.info("Scheduler đã được khởi động thành công")

    def stop(self):
        if self.running:
            self.scheduler.shutdown()
            self.running = False
            logger.info("Scheduler đã dừng")

    def _fetch_and_update_data(self):
        try:
            logger.info("Bắt đầu cào dữ liệu giá vàng...")
            global current_gold_data
            data = self.crawl_function()
            current_gold_data = data
            self.update_function(data)
            logger.info(f"Đã cập nhật thành công dữ liệu giá vàng. Số bản ghi: {len(data)}")
        except Exception as e:
            logger.error(f"Lỗi khi cập nhật dữ liệu giá vàng: {str(e)}")

def make_session(retries=3, backoff_factor=0.3, status_forcelist=(500,502,504)):
    s = requests.Session()
    retry = Retry(total=retries, backoff_factor=backoff_factor,
                  status_forcelist=status_forcelist,
                  allowed_methods=frozenset(['GET','POST']))
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

def parse_price_from_text(text):
    if not text:
        return None
    m = re.search(r'[\d\.]+', text)
    if not m:
        return None
    s = m.group(0).replace('.', '')   # "133.100" -> "133100"
    try:
        return float(s) * 1000
    except:
        return None

def crawl_btmc(debug=False):
    url = "https://giavang.org/trong-nuoc/bao-tin-minh-chau/"
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0 Safari/537.36")
    }

    sess = make_session()
    resp = sess.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    results = []

    boxes = soup.select("div.gold-price-box")

    for bi, box in enumerate(boxes, start=1):
        rows = box.select("div.row")
        for ri, row in enumerate(rows, start=1):
            if ri == 1:
                gold_type = "Giá vàng Miếng"
            else:
                gold_type = "Giá vàng Nhẫn"

            cols = row.select("div.col-6")
            if len(cols) < 1:
                continue

            buy_price = None
            sell_price = None

            for ci, col in enumerate(cols, start=1):
                label_tag = col.select_one("span.gold-price-label")
                price_tag = col.select_one("span.gold-price")

                label_text = label_tag.get_text(strip=True) if label_tag else f"col_{ci}"
                raw_price_text = price_tag.get_text(" ", strip=True) if price_tag else ""
                parsed_price = parse_price_from_text(raw_price_text)

                ulabel = label_text.upper()
                if "MUA" in ulabel:
                    buy_price = parsed_price
                elif "BÁN" in ulabel or "BAN" in ulabel:
                    sell_price = parsed_price
                else:
                    if ci == 1:
                        buy_price = parsed_price
                    elif ci == 2:
                        sell_price = parsed_price

            if buy_price is not None or sell_price is not None:
                now = datetime.now()
                formatted_time = now.strftime("%d/%m/%Y %H:%M:%S")
                record = {
                    "dealer": "BTMC",
                    "type": gold_type,
                    "time": formatted_time,
                    "timestamp": now.timestamp(),
                    "Mua vào": buy_price,
                    "Bán ra": sell_price
                }
                results.append(record)

    return results

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        try:
            history = json.load(f)
        except Exception:
            history = []
    cutoff = datetime.now() - timedelta(days=HISTORY_DAYS)
    filtered = [item for item in history if item.get("timestamp", 0) >= cutoff.timestamp()]
    return filtered

def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def update_history(new_data):
    history = load_history()
    for item in new_data:
        similar = [h for h in history if h["type"] == item["type"]]
        if not similar or abs(item["timestamp"] - similar[-1]["timestamp"]) > 60:
            history.append(item)
    cutoff = datetime.now() - timedelta(days=HISTORY_DAYS)
    history = [item for item in history if item.get("timestamp", 0) >= cutoff.timestamp()]
    save_history(history)
    return history

def get_price_trend(current, history, key):
    prev = None
    for item in reversed(history):
        if item["type"] == current["type"] and item != current:
            prev = item
            break
    if prev is None or prev[key] is None or current[key] is None:
        return {"symbol": "▬", "percent": 0}

    if current[key] > prev[key]:
        percent = ((current[key] - prev[key]) / prev[key]) * 100
        return {"symbol": "▲", "percent": round(percent, 2)}
    elif current[key] < prev[key]:
        percent = ((prev[key] - current[key]) / prev[key]) * 100
        return {"symbol": "▼", "percent": round(percent, 2)}
    else:
        return {"symbol": "▬", "percent": 0}

app = Flask(__name__, static_url_path='/static')

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Giá vàng Bảo Tín Minh Châu</title>
    <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="{{ url_for('static', filename='styles.css') }}">
</head>
<body>
    <div class="navbar">
        <h1>Giá vàng Bảo Tín Minh Châu</h1>
        <div class="nav-actions">
            <span id="status" class="status">Dữ liệu đã cập nhật</span>
            <button id="refresh" class="refresh" onclick="refreshData()">
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16">
                    <path d="M8 3a5 5 0 1 0 4.546 2.914.5.5 0 0 1 .908-.417A6 6 0 1 1 8 2z"/>
                    <path d="M8 4.466V.534a.25.25 0 0 1 .41-.192l2.36 1.966c.12.1.12.284 0 .384L8.41 4.658A.25.25 0 0 1 8 4.466z"/>
                </svg>
                Làm mới
            </button>
        </div>
    </div>

    <div class="container">
        <div class="card">
            <div class="card-header">
                <h2>Bảng giá vàng hiện tại</h2>
            </div>
            <div class="card-body">
                <table>
                    <thead>
                        <tr>
                            <th style="width: 35%; text-align: left;">Loại vàng</th>
                            <th style="width: 25%; text-align: right;" class="price-header">Mua vào</th>
                            <th style="width: 25%; text-align: right;" class="price-header">Bán ra</th>
                            <th style="width: 15%; text-align: center;">Thời gian</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for item in data %}
                        <tr>
                            <td style="text-align: left;">
                                <div class="gold-type">
                                    {% if item['type'] == "Giá vàng Miếng" %}
                                    <svg class="gold-icon" viewBox="0 0 24 24" fill="currentColor">
                                        <path d="M12 4L4 8l8 4 8-4-8-4z" fill="#E6A817"/>
                                        <path d="M4 12l8 4 8-4M4 16l8 4 8-4" fill="#E6A817"/>
                                    </svg>
                                    <span class="gold-bar">{{ item['type'] }}</span>
                                    {% else %}
                                    <svg class="gold-icon" viewBox="0 0 24 24" fill="currentColor">
                                        <circle cx="12" cy="12" r="8" fill="none" stroke="#9C6644" stroke-width="2"/>
                                        <circle cx="12" cy="12" r="4" fill="#9C6644"/>
                                    </svg>
                                    <span class="gold-ring">{{ item['type'] }}</span>
                                    {% endif %}
                                </div>
                            </td>
                            <td class="price" style="text-align: right;">
                                <div class="price-cell">
                                    <div class="price-value">{{ "{:,.0f}".format(item['Mua vào']) if item['Mua vào'] else "N/A" }}</div>
                                    <span class="trend {% if trends[item['type']]['buy']['symbol'] == '▲' %}trend-up{% elif trends[item['type']]['buy']['symbol'] == '▼' %}trend-down{% else %}trend-same{% endif %}">
                                        {{ trends[item['type']]['buy']['symbol'] }}
                                        {% if trends[item['type']]['buy']['percent'] != 0 %}
                                        <span class="percent">({{ trends[item['type']]['buy']['percent'] }}%)</span>
                                        {% endif %}
                                    </span>
                                </div>
                            </td>
                            <td class="price" style="text-align: right;">
                                <div class="price-cell">
                                    <div class="price-value">{{ "{:,.0f}".format(item['Bán ra']) if item['Bán ra'] else "N/A" }}</div>
                                    <span class="trend {% if trends[item['type']]['sell']['symbol'] == '▲' %}trend-up{% elif trends[item['type']]['sell']['symbol'] == '▼' %}trend-down{% else %}trend-same{% endif %}">
                                        {{ trends[item['type']]['sell']['symbol'] }}
                                        {% if trends[item['type']]['sell']['percent'] != 0 %}
                                        <span class="percent">({{ trends[item['type']]['sell']['percent'] }}%)</span>
                                        {% endif %}
                                    </span>
                                </div>
                            </td>
                            <td style="text-align: center;">{{ item['time'].split(' ')[1] }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

        <div class="card">
            <div class="card-header">
                <h2>Lịch sử giá vàng 7 ngày gần nhất</h2>
            </div>
            <div class="card-body">
                <table>
                    <thead>
                        <tr>
                            <th style="width: 22%; text-align: left;">Thời gian</th>
                            <th style="width: 28%; text-align: left;">Loại vàng</th>
                            <th style="width: 25%; text-align: right;" class="price-header">Mua vào</th>
                            <th style="width: 25%; text-align: right;" class="price-header">Bán ra</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for item in history %}
                        <tr>
                            <td style="text-align: left;">{{ item['time'] }}</td>
                            <td style="text-align: left;">
                                <div class="gold-type">
                                    {% if item['type'] == "Giá vàng Miếng" %}
                                    <svg class="gold-icon" viewBox="0 0 24 24" fill="currentColor">
                                        <path d="M12 4L4 8l8 4 8-4-8-4z" fill="#E6A817"/>
                                        <path d="M4 12l8 4 8-4M4 16l8 4 8-4" fill="#E6A817"/>
                                    </svg>
                                    <span class="gold-bar">{{ item['type'] }}</span>
                                    {% else %}
                                    <svg class="gold-icon" viewBox="0 0 24 24" fill="currentColor">
                                        <circle cx="12" cy="12" r="8" fill="none" stroke="#9C6644" stroke-width="2"/>
                                        <circle cx="12" cy="12" r="4" fill="#9C6644"/>
                                    </svg>
                                    <span class="gold-ring">{{ item['type'] }}</span>
                                    {% endif %}
                                </div>
                            </td>
                            <td class="price" style="text-align: right; padding-right: 12px;">
                                <div class="price-cell">
                                    <div class="price-value" style="min-width: 100px;">{{ "{:,.0f}".format(item['Mua vào']) if item['Mua vào'] else "N/A" }}</div>
                                </div>
                            </td>
                            <td class="price" style="text-align: right; padding-right: 12px;">
                                <div class="price-cell">
                                    <div class="price-value" style="min-width: 100px;">{{ "{:,.0f}".format(item['Bán ra']) if item['Bán ra'] else "N/A" }}</div>
                                </div>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

        <p class="timestamp">Cập nhật lần cuối: {{ data[0]['time'] if data else "N/A" }}</p>
    </div>

    <script>
        function refreshData() {
            document.getElementById('status').textContent = 'Đang cập nhật...';
            fetch(window.location.href)
                .then(response => response.text())
                .then(html => {
                    const parser = new DOMParser();
                    const newDoc = parser.parseFromString(html, 'text/html');

                    document.querySelector('.container').innerHTML = newDoc.querySelector('.container').innerHTML;
                    document.getElementById('status').textContent = 'Dữ liệu đã cập nhật';

                    // Highlight new data
                    document.querySelectorAll('tr').forEach(row => {
                        row.classList.add('highlight');
                    });
                })
                .catch(error => {
                    console.error('Error:', error);
                    document.getElementById('status').textContent = 'Lỗi khi cập nhật';
                });
        }
    </script>

    <footer>
        <p>&copy; 2025 website được code bởi Newbie và được dùng chỉ cho mục đích cá nhân vui lòng không thương mại hoá</a></p>
    </footer>
</body>
</html>
"""

@app.route('/')
def index():
    try:
        # Sử dụng biến toàn cục hoặc lấy dữ liệu mới
        global current_gold_data
        data = current_gold_data

        # Nếu chưa có dữ liệu (lần đầu chạy), thì thực hiện cào dữ liệu
        if not data:
            data = crawl_btmc()
            current_gold_data = data

        # Lấy lịch sử giá vàng
        history = load_history()

        # Tạo từ điển xu hướng giá cho từng loại vàng
        trends = {}
        for item in data:
            if item["type"] not in trends:
                trends[item["type"]] = {"buy": {"symbol": "▬", "percent": 0}, "sell": {"symbol": "▬", "percent": 0}}
            trends[item["type"]]["buy"] = get_price_trend(item, history, "Mua vào")
            trends[item["type"]]["sell"] = get_price_trend(item, history, "Bán ra")

        return render_template_string(HTML_TEMPLATE, data=data, history=history, trends=trends)
    except Exception as e:
        logger.error(f"Lỗi khi hiển thị trang: {str(e)}")
        return f"Lỗi: {str(e)}"

if __name__ == "__main__":
    # Khởi tạo và bắt đầu scheduler
    scheduler = GoldPriceScheduler(crawl_btmc, update_history)
    scheduler.start()

    try:
        app.run(debug=True)
    finally:
        # Dừng scheduler khi tắt ứng dụng
        scheduler.stop()
