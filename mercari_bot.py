import time
import sqlite3
import requests
import re
import os
import threading
from playwright.sync_api import sync_playwright

# === НАСТРОЙКИ ===
CONFIG = {
    "TG_TOKEN": "8823354327:AAGk-w0NZ8tj57flOCCTdthwdZi0vEFI-2o",
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
                if photo_url:
                    resp = requests.post(
                        f"https://api.telegram.org/bot{CONFIG['TG_TOKEN']}/sendPhoto",
                        json={
                            "chat_id": chat_id,
                            "caption": text,
                            "parse_mode": "HTML",
                            "photo": photo_url,
                        },
                        timeout=15,
                    )
                else:
                    resp = None

                # Если фото не удалось отправить (например, Telegram не смог скачать картинку),
                # шлём обычным сообщением, чтобы юзер хотя бы получил уведомление.
                if resp is None or not resp.ok:
                    if resp is not None:
                        print(f"   [tg sendPhoto {chat_id}] {resp.status_code} {resp.text[:200]}")
                    fallback = requests.post(
                        f"https://api.telegram.org/bot{CONFIG['TG_TOKEN']}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": text,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": False,
                        },
                        timeout=15,
                    )
                    if not fallback.ok:
                        print(f"   [tg sendMessage {chat_id}] {fallback.status_code} {fallback.text[:200]}")
            except Exception as e:
                print(f"   [tg err {chat_id}] {e}")

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

                    # Несколько источников цены: спец-селекторы → aria-label → весь текст карточки
                    yen = 0
                    for sel in (
                        '[data-testid="price"]',
                        '[class*="price" i]',
                        'span:has-text("¥")',
                        'span:has-text("円")',
                    ):
                        el = item.query_selector(sel)
                        if el:
                            yen = self.extract_yen_price(el.inner_text())
                            if yen > 0:
                                break

                    if yen <= 0:
                        aria = item.get_attribute('aria-label') or ''
                        yen = self.extract_yen_price(aria)

                    if yen <= 0:
                        yen = self.extract_yen_price(item.inner_text())

                    if yen > 0:
                        rub = round(yen * CONFIG["YEN_TO_RUB"])
                        price_str = f"{rub:,} ₽ <i>(¥{yen:,})</i>"
                    else:
                        print(f"   ⚠ Цена не найдена для {item_id}, шлю с пометкой")
                        price_str = "не определена (см. на сайте)"

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


def tg_api(method: str, **payload):
    """Хелпер: вызывает Telegram Bot API и логирует ошибки целиком (а не молча глотает)."""
    url = f"https://api.telegram.org/bot{CONFIG['TG_TOKEN']}/{method}"
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            print(f"[tg {method}] HTTP {resp.status_code}: {resp.text[:300]}")
        else:
            data = resp.json()
            if not data.get("ok"):
                print(f"[tg {method}] API error: {data}")
        return resp
    except Exception as e:
        print(f"[tg {method}] exception: {e}")
        return None


def self_test():
    """Проверка токена + рассылка стартового сообщения. Если эти шаги падают —
    дальше нет смысла запускать парсер."""
    print("→ Проверяю токен через getMe…")
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{CONFIG['TG_TOKEN']}/getMe", timeout=15
        ).json()
    except Exception as e:
        print(f"❌ Не удалось дозвониться до api.telegram.org: {e}")
        return False
    if not r.get("ok"):
        print(f"❌ Токен невалиден или отозван: {r}")
        return False
    me = r.get("result", {})
    print(f"✅ Бот: @{me.get('username')} (id={me.get('id')})")

    # Стартовое сообщение в каждый чат — если не дойдёт, увидим почему.
    ok_any = False
    for chat_id in CONFIG["CHAT_IDS"]:
        resp = tg_api(
            "sendMessage",
            chat_id=chat_id,
            text=f"🚀 Mercari Bot перезапущен. /ping — проверить связь.",
        )
        if resp is not None and resp.ok:
            ok_any = True
            print(f"   ✓ стартовое сообщение отправлено в {chat_id}")
    if not ok_any:
        print("⚠ Ни в один CHAT_ID не удалось отправить стартовое сообщение.")
        print("   Возможные причины: 1) пользователь не написал боту /start;")
        print("                     2) chat_id указан неверно;")
        print("                     3) бот не добавлен в группу/канал.")
    return True


if __name__ == "__main__":
    bot = MercariBot()

    if not self_test():
        print("Останавливаюсь. Исправьте токен и перезапустите.")
        raise SystemExit(1)

    threading.Thread(target=bot.run_parser, daemon=True).start()
    print("🚀 Mercari Bot запущен!")
    print("Команды:\n   /ping      — проверить связь\n   /price     — меню с кнопками\n   /maxprice 25000 — установить цену\n   /reset     — очистить базу")

    offset = 0
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{CONFIG['TG_TOKEN']}/getUpdates?offset={offset}&timeout=10",
                timeout=20,
            ).json()
            if not r.get("ok"):
                # Самая частая причина «бот не отвечает на команды»: 409 Conflict —
                # другой инстанс одновременно опрашивает getUpdates.
                print(f"[getUpdates] API error: {r}")
                time.sleep(5)
                continue
            if r.get("ok"):
                for u in r.get("result", []):
                    offset = u["update_id"] + 1
                    if "message" in u:
                        text = u["message"].get("text", "").strip()
                        chat_id = u["message"]["chat"]["id"]
                        print(f"[update] chat_id={chat_id} text={text!r}")
                        if text in ("/start", "/ping"):
                            tg_api(
                                "sendMessage",
                                chat_id=chat_id,
                                text=f"🏓 Pong! Ваш chat_id: <code>{chat_id}</code>",
                                parse_mode="HTML",
                            )
                        elif text == "/price":
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
        except Exception as e:
            # Раньше тут было голое except: pass — из-за этого «бот молчит» было невозможно
            # диагностировать. Теперь видно сетевые/JSON-ошибки.
            print(f"[getUpdates] exception: {e}")
            time.sleep(5)
