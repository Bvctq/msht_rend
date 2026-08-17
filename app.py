import os
import json
import re
import urllib.parse
import time
import logging
import traceback
from datetime import datetime
from functools import wraps

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

# ==================== CONFIG ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

COOKIE = os.environ.get("SHOPEE_COOKIE", "")
PORT = int(os.environ.get("PORT", 5000))

# Session giữ connection pool — giảm latency đáng kể so với Vercel
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"
})

# ==================== HELPERS ====================
def clean_cookie(raw):
    return (raw or "").replace('"', "").replace("'", "").strip()

def resolve_url(url):
    try:
        if not url.startswith("http"):
            url = "https://" + url
        r = session.get(url, timeout=15, allow_redirects=True)
        return r.url
    except Exception as e:
        logger.warning(f"resolve_url failed: {e}")
        return url

def extract_ids(url):
    m = re.search(r"/(\d+)/(\d+)(?:\?|$|&)", url)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"[?&]item_id=(\d+)", url)
    if m:
        return None, m.group(1)
    m = re.search(r"-i\.(\d+)\.(\d+)", url)
    if m:
        return m.group(1), m.group(2)
    return None, None

def format_money(num):
    try:
        return f"₫{int(num):,}".replace(",", ".")
    except Exception:
        return "₫0"

def format_shopee_money(num):
    try:
        vnd = int(num) / 100000
        return f"₫{int(vnd):,}".replace(",", ".")
    except Exception:
        return "₫0"

def log_request(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        logger.info(f"→ {request.method} {request.path} | IP: {request.remote_addr}")
        try:
            return f(*args, **kwargs)
        except Exception as e:
            logger.error(f"✗ Error in {request.path}: {str(e)}\n{traceback.format_exc()}")
            return jsonify({"error": "Internal server error", "detail": str(e)}), 500
    return decorated

# ==================== API CONVERT ====================
@app.route("/api/convert", methods=["POST"])
@log_request
def convert():
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    sub = data.get("sub_id", "")

    if not url:
        return jsonify({"error": "Missing url"}), 400

    cookie = clean_cookie(COOKIE)
    if not cookie:
        logger.error("SHOPEE_COOKIE chưa được cấu hình trong Environment Variables")
        return jsonify({"error": "No cookie configured"}), 500

    api_url = url if url.startswith("http") else "https://" + url
    lp = [{"originalLink": api_url}]
    if sub:
        lp[0]["advancedLinkParams"] = {"subId1": str(sub)}

    payload = {
        "operationName": "batchGetCustomLink",
        "query": "query batchGetCustomLink($linkParams: [CustomLinkParam!], $sourceCaller: SourceCaller){batchCustomLink(linkParams: $linkParams, sourceCaller: $sourceCaller){shortLink longLink failCode}}",
        "variables": {"linkParams": lp, "sourceCaller": "CUSTOM_LINK_CALLER"}
    }
    headers = {
        "content-type": "application/json",
        "cookie": cookie,
        "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
    }

    logger.info(f"Calling Shopee batchCustomLink for sub_id={sub}")
    r = session.post(
        "https://affiliate.shopee.vn/api/v3/gql?q=batchCustomLink",
        headers=headers,
        json=payload,
        timeout=20
    )
    raw_text = r.text

    try:
        d = r.json()
    except Exception as e:
        logger.error(f"Shopee trả về non-JSON: {raw_text[:500]}")
        return jsonify({
            "error": "Shopee returned non-JSON",
            "http_status": r.status_code,
            "raw": raw_text[:1000]
        }), 500

    batch = d.get("data", {}).get("batchCustomLink", [])
    if not batch:
        logger.error(f"Shopee empty batch: {json.dumps(d)[:500]}")
        return jsonify({
            "error": "empty batch",
            "shopee_response": d,
            "http_status": r.status_code
        }), 500

    item = batch[0]
    fail_code = item.get("failCode")
    if fail_code != 0:
        logger.error(f"Shopee failCode={fail_code}, sub={sub}")
        return jsonify({
            "error": f"Shopee failCode {fail_code}",
            "shopee_response": d,
            "sub_id_used": sub
        }), 500

    sl = item.get("shortLink")
    if not sl:
        return jsonify({"error": "no shortLink", "shopee_response": d}), 500

    logger.info(f"✓ Convert OK: {sl[:50]}...")
    return jsonify({
        "success": True,
        "affiliate_url": sl,
        "short_link": sl,
        "sub_id": sub or None
    })

# ==================== API COMMISSION ====================
@app.route("/api/commission", methods=["GET"])
@log_request
def commission():
    raw_url = request.args.get("url", "")
    item_id = request.args.get("item_id", "")

    if not item_id:
        is_short = any(x in raw_url for x in ["s.shopee.vn", "shp.ee", "vn.shp.ee"])
        resolved = resolve_url(raw_url) if is_short else raw_url
        shopid, itemid = extract_ids(resolved)
        item_id = itemid

    if not item_id:
        return jsonify({"success": False, "debug": "Cannot extract item_id from URL"}), 200

    try:
        r = session.get(
            f"https://data.addlivetag.com/product-data/product-data.php?item_id={item_id}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=15
        )
        d = r.json()

        if d.get("status") != "success":
            return jsonify({"success": False, "debug": "addlivetag API error", "detail": d}), 200

        info = d.get("productInfo", {})
        if not info:
            return jsonify({"success": False, "debug": "Empty productInfo"}), 200

        product_name = info.get("productName", "Sản phẩm Shopee")
        image = info.get("imageUrl", "")
        price_val = info.get("price", 0)
        price_str = format_money(price_val) if price_val else ""

        seller_com = info.get("sellerComFinal", 0)
        if seller_com is None or not info.get("hasSellerCommission", False):
            seller_com = 0

        user_cashback = seller_com // 2
        platform_fee = seller_com - user_cashback

        seller_rate = info.get("sellerRatePercent", 0)
        seller_rate_str = f"{seller_rate}%" if seller_rate else "~5%"

        return jsonify({
            "success": True,
            "item_id": item_id,
            "product_name": product_name,
            "image": image,
            "price": price_str,
            "seller_commission_rate": seller_rate_str,
            "seller_commission": format_money(seller_com),
            "estimated_commission": format_money(seller_com),
            "estimated_cashback": format_money(user_cashback),
            "user_cashback": format_money(user_cashback),
            "platform_fee": format_money(platform_fee),
            "cashback_percent": 50,
            "data_source": info.get("dataSource", "unknown")
        })

    except Exception as e:
        logger.error(f"Commission error: {e}")
        return jsonify({"success": False, "debug": str(e)}), 200

# ==================== API ORDERS ====================
@app.route("/api/orders", methods=["GET"])
@log_request
def orders():
    sub_id = request.args.get("sub_id")
    if not sub_id:
        return jsonify({"error": "Missing sub_id"}), 400

    qs = urllib.parse.urlencode({
        "page_size": request.args.get("page_size", "20"),
        "page_num": request.args.get("page_num", "1"),
        "sub_id": sub_id,
        "purchase_time_s": request.args.get("start", ""),
        "purchase_time_e": request.args.get("end", ""),
        "version": "1"
    })
    cookie = clean_cookie(COOKIE)
    headers = {
        "content-type": "application/json",
        "cookie": cookie,
        "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
    }

    try:
        r = session.get(
            f"https://affiliate.shopee.vn/api/v3/report/list?{qs}",
            headers=headers,
            timeout=20
        )
        d = r.json()
        if d.get("code") != 0:
            logger.error(f"Shopee orders error: {d}")
            return jsonify({"error": "Shopee error", "detail": d}), 500

        data = d.get("data") or {}
        checkout_list = data.get("list") or []

        out = []
        for checkout in checkout_list:
            net_comm_str = str(checkout.get("affiliate_net_commission") or "0")
            try:
                net_comm = int(float(net_comm_str))
            except Exception:
                net_comm = 0

            total_items = 0
            for order in (checkout.get("orders") or []):
                total_items += len(order.get("items") or [])

            comm_per_item = net_comm // total_items if total_items > 0 else 0
            remainder = net_comm - (comm_per_item * total_items)

            checkout_status = checkout.get("checkout_status", "")
            conversion_status = checkout.get("conversion_status", 1)

            purchase_ts = checkout.get("purchase_time", 0)
            purchase_dt = ""
            if purchase_ts:
                try:
                    purchase_dt = datetime.fromtimestamp(purchase_ts).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    purchase_dt = ""

            item_idx = 0
            for order in (checkout.get("orders") or []):
                order_sn = order.get("order_sn", "")
                order_status = order.get("order_status", "")

                if order_status == "CANCEL" or checkout_status == "Invalid" or conversion_status == 3:
                    mapped_status = "cancelled"
                elif order_status == "COMPLETED" or conversion_status == 2:
                    mapped_status = "confirmed"
                else:
                    mapped_status = "pending"

                for item in (order.get("items") or []):
                    item_comm = comm_per_item + (1 if item_idx == 0 and remainder > 0 else 0)
                    item_idx += 1

                    user_cashback = item_comm // 2

                    actual = item.get("actual_amount", 0)
                    price = item.get("item_price", 0)
                    amount_val = actual if actual else price

                    out.append({
                        "order_sn": order_sn,
                        "item_id": str(item.get("item_id", "")),
                        "product_name": item.get("item_name", ""),
                        "amount": format_shopee_money(amount_val),
                        "commission": format_shopee_money(item_comm),
                        "cashback": format_shopee_money(user_cashback),
                        "status": mapped_status,
                        "purchase_time": purchase_dt,
                        "shop_name": item.get("shop_name", ""),
                        "image": item.get("img_code", "")
                    })

        return jsonify({
            "success": True,
            "sub_id": sub_id,
            "page_num": data.get("page_num", 1),
            "page_size": data.get("page_size", 20),
            "total_count": data.get("total_count", 0),
            "orders": out
        })
    except Exception as e:
        logger.error(f"Orders exception: {e}")
        return jsonify({"error": str(e)}), 500

# ==================== API TEST REPORT ====================
@app.route("/api/test-report", methods=["GET"])
@log_request
def test_report():
    try:
        cookie = clean_cookie(COOKIE)
        if not cookie:
            return jsonify({
                "alive": False,
                "error": "Chưa có SHOPEE_COOKIE trong Environment Variables"
            }), 200

        sub_id = request.args.get("sub_id", "addsub")
        end = int(time.time())
        start = end - (7 * 24 * 3600)

        qs = urllib.parse.urlencode({
            "page_size": "20",
            "page_num": "1",
            "sub_id": sub_id,
            "purchase_time_s": start,
            "purchase_time_e": end,
            "version": "1"
        })

        headers = {
            "content-type": "application/json",
            "cookie": cookie,
            "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"
        }

        r = session.get(
            f"https://affiliate.shopee.vn/api/v3/report/list?{qs}",
            headers=headers,
            timeout=15
        )

        try:
            data = r.json()
        except Exception:
            return jsonify({
                "alive": False,
                "error": "Shopee trả về không phải JSON",
                "raw_status": r.status_code,
                "raw_body": r.text[:500]
            }), 200

        shopee_code = data.get("code")
        total = (data.get("data") or {}).get("total_count", 0)
        lst = (data.get("data") or {}).get("list")

        if shopee_code == 0 and total > 0:
            return jsonify({
                "alive": True,
                "http_code": r.status_code,
                "shopee_code": shopee_code,
                "total_checkouts": total,
                "message": f"API hoạt động. Có {total} checkout.",
                "sample": lst[0] if lst else None
            })
        elif shopee_code == 0:
            return jsonify({
                "alive": True,
                "http_code": r.status_code,
                "shopee_code": shopee_code,
                "total_checkouts": 0,
                "message": "API hoạt động nhưng không có đơn hàng (sub_id chưa có đơn hoặc sai thời gian)."
            })
        else:
            return jsonify({
                "alive": False,
                "http_code": r.status_code,
                "shopee_code": shopee_code,
                "message": f"Cookie hết hạn hoặc bị chặn. Code: {shopee_code}",
                "raw": data
            })
    except Exception as e:
        logger.error(f"Test report exception: {e}")
        return jsonify({"alive": False, "error": f"Exception: {str(e)}"}), 200

# ==================== HEALTH CHECKS ====================
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "OK",
        "service": "SaleVN API on Render",
        "timestamp": datetime.now().isoformat(),
        "cookie_configured": bool(COOKIE),
        "cookie_length": len(COOKIE)
    })

@app.route("/api/health", methods=["GET"])
def api_health():
    return health()

# ==================== MAIN ====================
if __name__ == "__main__":
    logger.info(f"🚀 SaleVN API starting on 0.0.0.0:{PORT}")
    logger.info(f"🔑 Cookie configured: {bool(COOKIE)} (length: {len(COOKIE)})")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
