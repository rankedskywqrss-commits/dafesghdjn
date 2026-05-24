import time
import sqlite3
import requests
import re
import os
import threading
from playwright.sync_api import sync_playwright

# === НАСТРОЙКИ ===
CONFIG = {
    "TG_TOKEN": "8626886513:AAHsIw586Jcw0D4DCClhb3fKCgoQwAjZ2eE",
    "CHAT_IDS": ["711777770", "6368693741"],
    "CHECK_INTERVAL": 120,
    # Курс японской иены к рублю. Уточняйте актуальное значение.
    "YEN_TO_RUB": 0.55,
}

KEYWORDS_FILE = "keywords.txt"


class MercariBot:
    def __init__(self):
        self.conn = sqlite3.connect("mercari_new.db", check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.cursor.execute('CREATE TABLE IF NOT EXISTS items (item_id TEXT PRIMARY KEY, title TEXT)')
        self.conn.commit()
        self.keywords = self.load_keywords()
        self.global_max_price = None

    def load_keywords(self):
        if not os.path.exists(KEYWORDS_FILE):
            with open(KEYWORDS_FILE, "w", encoding="utf-8") as f:
                f.write("vintage\nundercover\nmaison margiela")
            return ["vintage", "undercover", "maison margiela"]
        with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]

    def price_keyboard(self):
        return {
            "inline_keyboard": [
                [{"text": "До 5 000 ¥",    "callback_data": "pmax_5000"}],
                [{"text": "До 10 000 ¥",   "callback_data": "pmax_10000"}],
                [{"text": "До 20 000 ¥",   "callback_data": "pmax_20000"}],
                [{"text": "До 40 000 ¥",   "callback_data": "pmax_40000"}],
                [{"text": "🔓 Все цены",   "callback_data": "pmax_all"}],
            ]
        }

    def send_telegram(self, title, price_str, link, photo_url, keyword):
        clean_title = (title[:100] + '...') if len(title) > 100 else title
        text = (
            f"🔔 <b>Новый лот!</b>\n\n"
            f"🔍 Поиск: <code>{keyword}</code>\n"
            f"🏷 <b>{clean_title}</b>\n"
            f"💰 <b>Цена: {price_str}</b>\n\n"
            f"🔗 <a href='{link}'>Открыть объявление</a>"
        )
        for chat_id in CONFIG["CHAT_IDS"]:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{CONFIG['TG_TOKEN']}/sendPhoto",
                    json={
                        "chat_id": chat_id,
                        "caption": text,
                        "parse_mode": "HTML",
                        "photo": photo_url,
                    },
                    timeout=15,
                )
            except Exception:
                pass

    def reset_database(self):
        self.cursor.execute('DELETE FROM items')
        self.conn.commit()
        print("🗑 База очищена!")

    @staticmethod
    def extract_yen_price(text: str) -> int:
        """Достаёт цену в иенах из строки. Поддерживает форматы ¥1,200 / ￥1,200 / 1,200円."""
        if not text:
            return 0
        # Уберём метки распроданных лотов, чтобы не словить случайные числа
        cleaned = text.replace("SOLD", " ").replace("Sold", " ")
        m = re.search(r'[¥￥]\s*([\d,]+)', cleaned)
        if not m:
            m = re.search(r'([\d,]+)\s*円', cleaned)
        if not m:
            return 0
        try:
            return int(m.group(1).replace(',', ''))
        except ValueError:
            return 0

    def parse(self, page, keyword):
        max_price = self.global_max_price
        limit_text = f"до {max_price:,} ¥" if max_price and max_price != 999999 else "без лимита"
        print(f"🔎 {keyword} | Лимит: {limit_text}")
        try:
            url = (
                f"https://jp.mercari.com/search?keyword={keyword.replace(' ', '%20')}"
                f"&status=on_sale&sort=created_time&order=desc"
            )
            if max_price and max_price != 999999:
                url += f"&price_max={max_price}"

            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            items = (
                page.query_selector_all('div[data-testid="item-card"]')
                or page.query_selector_all('a[href^="/item/m"]')
            )

            found_new = 0
            for item in items[:10]:
                try:
                    link_el = item.query_selector('a[href^="/item/m"]') or item
                    href = link_el.get_attribute('href')
                    if not href:
                        continue

                    item_id = href.split('/')[-1]
                    self.cursor.execute('SELECT 1 FROM items WHERE item_id = ?', (item_id,))
                    if self.cursor.fetchone():
                        continue

                    img = item.query_selector('img')
                    title = (img.get_attribute('alt') or "Без названия").replace('Thumbnail of ', '').strip() if img else "Без названия"
                    img_url = img.get_attribute('src') if img else ""

                    # Сначала пробуем специальный селектор цены, потом весь текст карточки
                    price_el = (
                        item.query_selector('[data-testid="price"]')
                        or item.query_selector('[class*="price" i]')
                        or item.query_selector('span:has-text("¥")')
                    )
                    price_text = price_el.inner_text() if price_el else item.inner_text()
                    yen = self.extract_yen_price(price_text)

                    if yen <= 0:
                        print(f"   ⚠ Не удалось распарсить цену для {item_id}: {price_text[:80]!r}")
                        continue

                    rub = round(yen * CONFIG["YEN_TO_RUB"])
                    price_str = f"{rub:,} ₽ <i>(¥{yen:,})</i>"

                    full_link = "https://jp.mercari.com" + href
                    self.send_telegram(title, price_str, full_link, img_url, keyword)

                    self.cursor.execute('INSERT INTO items VALUES (?, ?)', (item_id, title))
                    self.conn.commit()
                    found_new += 1
                except Exception as e:
                    print(f"   [item err] {e}")
                    continue

            print(f"   → Новых: {found_new}")
        except Exception as e:
            print(f" [!] Ошибка: {e}")

    def run_parser(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = context.new_page()
            while True:
                for kw in self.keywords:
                    self.parse(page, kw)
                    time.sleep(8)
                time.sleep(CONFIG["CHECK_INTERVAL"])

    def send_price_menu(self, chat_id):
        requests.post(
            f"https://api.telegram.org/bot{CONFIG['TG_TOKEN']}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": (
                    "💰 Управление фильтром цены:\n\n"
                    "/maxprice 15000 — установить лимит\n"
                    "/maxprice 0 или /maxprice all — снять лимит\n\n"
                    "Или нажми кнопку ниже:"
                ),
                "reply_markup": self.price_keyboard(),
            },
        )


if __name__ == "__main__":
    bot = MercariBot()
    threading.Thread(target=bot.run_parser, daemon=True).start()
    print("🚀 Mercari Bot запущен!")
    print("Команды:\n   /price     — меню с кнопками\n   /maxprice 25000 — установить цену\n   /reset     — очистить базу")

    offset = 0
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{CONFIG['TG_TOKEN']}/getUpdates?offset={offset}&timeout=10"
            ).json()
            if r.get("ok"):
                for u in r.get("result", []):
                    offset = u["update_id"] + 1
                    if "message" in u:
                        text = u["message"].get("text", "").strip()
                        chat_id = u["message"]["chat"]["id"]
                        if text == "/price":
                            bot.send_price_menu(chat_id)
                        elif text.startswith("/maxprice"):
                            try:
                                value = text.split()[1]
                                if value.lower() in ["0", "all", "none"]:
                                    bot.global_max_price = 999999
                                    msg = "✅ Ограничение цены снято"
                                else:
                                    price = int(value)
                                    bot.global_max_price = price
                                    msg = f"✅ Установлен глобальный лимит: **до {price:,} ¥**"
                                requests.post(
                                    f"https://api.telegram.org/bot{CONFIG['TG_TOKEN']}/sendMessage",
                                    json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                                )
                            except Exception:
                                requests.post(
                                    f"https://api.telegram.org/bot{CONFIG['TG_TOKEN']}/sendMessage",
                                    json={"chat_id": chat_id, "text": "❌ Использование: /maxprice 25000"},
                                )
                        elif text == "/reset":
                            bot.reset_database()
                            requests.post(
                                f"https://api.telegram.org/bot{CONFIG['TG_TOKEN']}/sendMessage",
                                json={"chat_id": chat_id, "text": "✅ База очищена!"},
                            )
                    elif "callback_query" in u:
                        cq = u["callback_query"]
                        data = cq["data"]
                        chat_id = cq["message"]["chat"]["id"]
                        if data.startswith("pmax_"):
                            if data == "pmax_all":
                                bot.global_max_price = 999999
                                text = "✅ Фильтр цены снят"
                            else:
                                bot.global_max_price = int(data.replace("pmax_", ""))
                                text = f"✅ Лимит установлен: до {bot.global_max_price:,} ¥"
                            requests.post(
                                f"https://api.telegram.org/bot{CONFIG['TG_TOKEN']}/answerCallbackQuery",
                                json={"callback_query_id": cq["id"]},
                            )
                            requests.post(
                                f"https://api.telegram.org/bot{CONFIG['TG_TOKEN']}/sendMessage",
                                json={"chat_id": chat_id, "text": text},
                            )
        except Exception:
            time.sleep(5)
