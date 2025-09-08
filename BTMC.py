import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, render_template_string

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
                # Định dạng lại thời gian cho dễ nhìn
                now = datetime.now()
                formatted_time = now.strftime("%d/%m/%Y %H:%M:%S")
                record = {
                    "dealer": "BTMC",
                    "type": gold_type,
                    "time": formatted_time,
                    "Mua vào": buy_price,
                    "Bán ra": sell_price
                }
                results.append(record)

    return results

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Bảng giá vàng Bảo Tín Minh Châu</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 0; padding: 0; background: #f9f9f9; }
        .container { width: 100vw; min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; }
        table { border-collapse: collapse; width: 90vw; max-width: 1400px; background: #fff; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
        th, td { border: 1px solid #ccc; padding: 16px 20px; text-align: center; font-size: 1.25em; }
        th { background: #f7f7f7; }
        caption { font-size: 2em; margin-bottom: 18px; font-weight: bold; }
    </style>
</head>
<body>
    <div class="container">
    <table>
        <caption>Bảng giá vàng Bảo Tín Minh Châu</caption>
        <tr>
            <th>Loại vàng</th>
            <th>Mua vào</th>
            <th>Bán ra</th>
            <th>Thời gian</th>
        </tr>
        {% for item in data %}
        <tr>
            <td>{{ item['type'] }}</td>
            <td>{{ "{:,.0f}".format(item['Mua vào']) if item['Mua vào'] else "" }}</td>
            <td>{{ "{:,.0f}".format(item['Bán ra']) if item['Bán ra'] else "" }}</td>
            <td>{{ item['time'] }}</td>
        </tr>
        {% endfor %}
    </table>
    </div>
</body>
</html>
"""

@app.route("/")
def index():
    data = crawl_btmc(debug=False)
    return render_template_string(HTML_TEMPLATE, data=data)

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "web":
        app.run(debug=True)
    else:
        data = crawl_btmc(debug=False)
        print("\nDữ liệu đã cào vàng Bảo Tín Minh Châu:")
        for item in data:
            print(item)

