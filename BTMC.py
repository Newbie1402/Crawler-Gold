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
    <style>
        .sort-container {
            display: inline-flex;
            align-items: center;
            margin-left: 5px;
            position: relative;
        }

        .sort-button {
            background: none;
            border: none;
            cursor: pointer;
            padding: 3px;
            margin: 0 2px;
            border-radius: 3px;
            color: var(--primary-color);
            font-size: 0.85rem;
            display: flex;
            align-items: center;
            justify-content: center;
            width: 24px;
            height: 24px;
        }

        .sort-button:hover {
            background-color: var(--light-primary);
        }

        .sort-button.active {
            background-color: var(--primary-color);
            color: white;
        }

        .column-header {
            display: flex;
            align-items: center;
            justify-content: var(--justify, flex-start);
        }

        .filter-active {
            font-weight: 500;
            color: var(--primary-color);
            margin-right: 10px;
            margin-bottom: 10px;
            display: block;
        }

        .history-filter {
            margin-bottom: 15px;
        }

        .filter-wrapper {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }

        .filter-group {
            margin-bottom: 10px;
            border-bottom: 1px solid #eee;
            padding-bottom: 8px;
        }

        .filter-group-title {
            font-weight: 500;
            margin-bottom: 8px;
            color: #555;
            display: block;
        }

        .filter-buttons {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }

        .filter-btn {
            background-color: #f1f1f1;
            border: 1px solid var(--border-color);
            border-radius: 4px;
            padding: 6px 12px;
            cursor: pointer;
            font-size: 0.9rem;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            gap: 5px;
        }

        .filter-btn:hover {
            background-color: var(--light-primary);
        }

        .filter-btn.active {
            background-color: var(--primary-color);
            color: white;
        }

        .filter-icon {
            font-size: 0.8rem;
        }

        .filter-actions {
            display: flex;
            justify-content: flex-end;
            gap: 10px;
            margin-top: 10px;
        }

        .apply-btn, .clear-btn {
            background-color: var(--primary-color);
            color: white;
            border: none;
            border-radius: 4px;
            padding: 8px 16px;
            cursor: pointer;
            font-size: 0.9rem;
            transition: background-color 0.2s;
        }

        .apply-btn:hover, .clear-btn:hover {
            background-color: var(--light-primary);
        }
    </style>
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
                <!-- Thanh công cụ lọc và sắp xếp -->
                <div class="history-filter">
                    <span class="filter-active" id="active-filter">Sắp xếp mặc định</span>

                    <div class="filter-wrapper">
                        <div class="filter-group">
                            <span class="filter-group-title">Theo thời gian</span>
                            <div class="filter-buttons">
                                <button class="filter-btn" data-filter="time" data-value="desc" onclick="toggleFilter(this, 'time')">
                                    <span class="filter-icon">⏱️</span> Mới nhất
                                </button>

                                <button class="filter-btn" data-filter="time" data-value="asc" onclick="toggleFilter(this, 'time')">
                                    <span class="filter-icon">⏱️</span> Cũ nhất
                                </button>
                            </div>
                        </div>

                        <div class="filter-group">
                            <span class="filter-group-title">Theo loại vàng</span>
                            <div class="filter-buttons">
                                <button class="filter-btn" data-filter="goldType" data-value="Giá vàng Miếng" onclick="toggleFilter(this, 'goldType')">
                                    <span class="filter-icon">🪙</span> Vàng miếng
                                </button>

                                <button class="filter-btn" data-filter="goldType" data-value="Giá vàng Nhẫn" onclick="toggleFilter(this, 'goldType')">
                                    <span class="filter-icon">💍</span> Vàng nhẫn
                                </button>
                            </div>
                        </div>

                        <div class="filter-group">
                            <span class="filter-group-title">Theo giá mua vào</span>
                            <div class="filter-buttons">
                                <button class="filter-btn" data-filter="buy" data-value="asc" onclick="toggleFilter(this, 'buy')">
                                    <span class="filter-icon">💰</span> Rẻ nhất trước
                                </button>

                                <button class="filter-btn" data-filter="buy" data-value="desc" onclick="toggleFilter(this, 'buy')">
                                    <span class="filter-icon">💰</span> Đắt nhất trước
                                </button>
                            </div>
                        </div>

                        <div class="filter-group">
                            <span class="filter-group-title">Theo giá bán ra</span>
                            <div class="filter-buttons">
                                <button class="filter-btn" data-filter="sell" data-value="asc" onclick="toggleFilter(this, 'sell')">
                                    <span class="filter-icon">💸</span> Rẻ nhất trước
                                </button>

                                <button class="filter-btn" data-filter="sell" data-value="desc" onclick="toggleFilter(this, 'sell')">
                                    <span class="filter-icon">💸</span> Đắt nhất trước
                                </button>
                            </div>
                        </div>
                    </div>

                    <div class="filter-actions">
                        <button id="apply-filters" class="apply-btn" onclick="applyAllFilters()">Áp dụng</button>
                        <button id="clear-filters" class="clear-btn" onclick="clearAllFilters()">Xóa lọc</button>
                    </div>
                </div>

                <table id="history-table">
                    <thead>
                        <tr>
                            <th style="width: 22%; text-align: left;">
                                <div class="column-header">
                                    Thời gian
                                </div>
                            </th>
                            <th style="width: 28%; text-align: left;">Loại vàng</th>
                            <th style="width: 25%; text-align: right;" class="price-header">
                                <div class="column-header" style="--justify: flex-end">
                                    Mua vào
                                </div>
                            </th>
                            <th style="width: 25%; text-align: right;" class="price-header">
                                <div class="column-header" style="--justify: flex-end">
                                    Bán ra
                                </div>
                            </th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for item in history %}
                        <tr data-timestamp="{{ item['timestamp'] }}" data-buy="{{ item['Mua vào'] or 0 }}" data-sell="{{ item['Bán ra'] or 0 }}">
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
        // Lưu trữ bộ lọc đã chọn
        const activeFilters = {
            time: null,
            goldType: null,
            buy: null,
            sell: null
        };

        // Hàm bật/tắt bộ lọc
        function toggleFilter(button, filterType) {
            const filterValue = button.getAttribute('data-value');
            const isActive = button.classList.contains('active');

            // Nếu nút đang active, bỏ chọn nút đó và xóa bộ lọc tương ứng
            if (isActive) {
                button.classList.remove('active');
                activeFilters[filterType] = null;
            } else {
                // Bỏ chọn các nút khác cùng loại
                document.querySelectorAll(`.filter-btn[data-filter="${filterType}"]`).forEach(btn => {
                    btn.classList.remove('active');
                });

                // Chọn nút hiện tại và lưu giá trị lọc
                button.classList.add('active');
                activeFilters[filterType] = filterValue;
            }

            // Cập nhật nội dung bộ lọc đang chọn
            updateActiveFilterText();
        }

        // Hàm cập nhật văn bản hiển thị bộ lọc đang hoạt động
        function updateActiveFilterText() {
            const activeFilterText = document.getElementById('active-filter');
            const selectedFilters = [];

            // Kiểm tra từng loại bộ lọc và thêm vào mảng nếu đang hoạt động
            if (activeFilters.time) {
                selectedFilters.push(activeFilters.time === 'desc' ? 'Mới nhất trước' : 'Cũ nhất trước');
            }

            if (activeFilters.goldType) {
                selectedFilters.push(activeFilters.goldType === 'Giá vàng Miếng' ? 'Vàng miếng' : 'Vàng nhẫn');
            }

            if (activeFilters.buy) {
                selectedFilters.push('Mua vào: ' + (activeFilters.buy === 'asc' ? 'Rẻ nhất trước' : 'Đắt nhất trước'));
            }

            if (activeFilters.sell) {
                selectedFilters.push('Bán ra: ' + (activeFilters.sell === 'asc' ? 'Rẻ nhất trước' : 'Đắt nhất trước'));
            }

            // Cập nhật nội dung hiển thị
            if (selectedFilters.length > 0) {
                activeFilterText.textContent = 'Bộ lọc đang chọn: ' + selectedFilters.join(', ');
            } else {
                activeFilterText.textContent = 'Sắp xếp mặc định';
            }
        }

        // Hàm áp dụng tất cả các bộ lọc đang hoạt động
        function applyAllFilters() {
            const table = document.getElementById('history-table');
            const tbody = table.querySelector('tbody');
            const rows = Array.from(tbody.querySelectorAll('tr'));

            // Bắt đầu với tất cả các hàng
            let filteredRows = [...rows]; // Tạo một bản sao của mảng rows

            // Lọc theo loại vàng (nếu có)
            if (activeFilters.goldType) {
                filteredRows = filteredRows.filter(row => {
                    const goldTypeCell = row.querySelector('td:nth-child(2)');
                    return goldTypeCell.textContent.trim().includes(activeFilters.goldType);
                });
            }

            // Thiết lập các thuộc tính data-* cho mỗi hàng để đảm bảo rằng chúng được đọc chính xác
            rows.forEach(row => {
                if (!row.dataset.type) {
                    const typeCell = row.querySelector('td:nth-child(2)');
                    if (typeCell) {
                        const type = typeCell.textContent.trim().includes("Giá vàng Miếng") ?
                            "Giá vàng Miếng" : "Giá vàng Nhẫn";
                        row.dataset.type = type;
                    }
                }
            });

            // Sắp xếp dữ liệu dựa trên các bộ lọc đã chọn
            if (activeFilters.time || activeFilters.buy || activeFilters.sell) {
                filteredRows.sort((a, b) => {
                    // Nếu lọc theo thời gian được áp dụng
                    if (activeFilters.time) {
                        const timeA = parseFloat(a.dataset.timestamp);
                        const timeB = parseFloat(b.dataset.timestamp);

                        if (timeA !== timeB) {
                            return activeFilters.time === 'asc' ? timeA - timeB : timeB - timeA;
                        }
                    }

                    // Nếu lọc theo giá mua vào được áp dụng
                    if (activeFilters.buy) {
                        const buyA = parseFloat(a.dataset.buy);
                        const buyB = parseFloat(b.dataset.buy);

                        if (buyA !== buyB) {
                            return activeFilters.buy === 'asc' ? buyA - buyB : buyB - buyA;
                        }
                    }

                    // Nếu lọc theo giá bán ra được áp dụng
                    if (activeFilters.sell) {
                        const sellA = parseFloat(a.dataset.sell);
                        const sellB = parseFloat(b.dataset.sell);

                        if (sellA !== sellB) {
                            return activeFilters.sell === 'asc' ? sellA - sellB : sellB - sellA;
                        }
                    }

                    return 0;
                });
            }

            // Ẩn tất cả các hàng
            rows.forEach(row => row.style.display = 'none');

            // Hiển thị các hàng đã lọc và sắp xếp
            filteredRows.forEach(row => {
                row.style.display = '';
                tbody.appendChild(row); // Di chuyển hàng lên đầu bảng
            });

            // Thêm hiệu ứng highlight
            filteredRows.forEach(row => {
                row.classList.add('highlight');
                setTimeout(() => row.classList.remove('highlight'), 2000);
            });

            // Hiển thị thông báo nếu không có kết quả
            const noResultsMessage = document.getElementById('no-results-message');
            if (filteredRows.length === 0) {
                if (!noResultsMessage) {
                    const message = document.createElement('div');
                    message.id = 'no-results-message';
                    message.textContent = 'Không có kết quả phù hợp với bộ lọc đã chọn';
                    message.style.textAlign = 'center';
                    message.style.padding = '20px';
                    message.style.color = '#666';
                    table.parentNode.insertBefore(message, table.nextSibling);
                }
            } else if (noResultsMessage) {
                noResultsMessage.remove();
            }

            // Cập nhật văn bản hiển thị bộ lọc đang áp dụng
            updateActiveFilterText();
        }

        // Hàm xóa tất cả các bộ lọc
        function clearAllFilters() {
            // Bỏ chọn tất cả các nút lọc
            document.querySelectorAll('.filter-btn').forEach(btn => {
                btn.classList.remove('active');
            });

            // Đặt lại tất cả các bộ lọc về null
            for (const key in activeFilters) {
                activeFilters[key] = null;
            }

            // Cập nhật nội dung hiển thị
            updateActiveFilterText();

            // Hiển thị lại tất cả các hàng theo thứ tự mới nhất
            const table = document.getElementById('history-table');
            const tbody = table.querySelector('tbody');
            const rows = Array.from(tbody.querySelectorAll('tr'));

            // Sắp xếp theo thời gian mới nhất
            rows.sort((a, b) => {
                return parseFloat(b.dataset.timestamp) - parseFloat(a.dataset.timestamp);
            });

            // Hiển thị lại tất cả các hàng
            rows.forEach(row => {
                row.style.display = '';
                tbody.appendChild(row);
            });
        }

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

                    // Khởi tạo lại sau khi tải dữ liệu mới
                    initFilters();
                })
                .catch(error => {
                    console.error('Error:', error);
                    document.getElementById('status').textContent = 'Lỗi khi cập nhật';
                });
        }

        // Hàm khởi tạo khi trang tải xong
        function initFilters() {
            // Đặt lại tất cả bộ lọc về giá trị mặc định
            for (const key in activeFilters) {
                activeFilters[key] = null;
            }

            // Bỏ chọn tất cả các nút lọc
            document.querySelectorAll('.filter-btn').forEach(btn => {
                btn.classList.remove('active');
            });

            // Cập nhật nội dung hiển thị
            updateActiveFilterText();

            // Áp dụng sắp xếp mặc định (mới nhất trước)
            const timeNewestBtn = document.querySelector('.filter-btn[data-filter="time"][data-value="desc"]');
            if (timeNewestBtn) {
                toggleFilter(timeNewestBtn, 'time');
                applyAllFilters();
            }
        }

        // Sắp xếp mặc định khi tải trang
        window.addEventListener('load', initFilters);
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
