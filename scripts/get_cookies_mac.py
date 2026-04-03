"""
Скрипт для macOS: автологин в Яндекс и отправка куки на VPS.
Запускать на своём Mac когда придёт уведомление об истечении куки.

Установка (в терминале):
  pip3 install playwright requests
  python3 -m playwright install chromium

Запуск:
  python3 get_cookies_mac.py
"""
import sys
import requests
from playwright.sync_api import sync_playwright

# ─── Настройки ───────────────────────────────────────────────
YANDEX_LOGIN    = "periclinic.ai@yandex.ru"
YANDEX_PASSWORD = "270914-Kamilla"

SERVER_URL      = "http://89.22.233.73:8765/update-cookies"
SECRET_TOKEN    = "periclinic2024secret"
# ─────────────────────────────────────────────────────────────


def get_yandex_cookies():
    print("Открываю браузер...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # видимый браузер
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        print("Перехожу на страницу входа...")
        page.goto("https://passport.yandex.ru/auth", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        # Вводим логин
        page.locator("input[type='text'], input[type='email'], input:not([type])").first.fill(YANDEX_LOGIN)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)

        # Ждём поле пароля
        try:
            page.wait_for_selector("input[type='password']", timeout=10000)
            page.locator("input[type='password']").fill(YANDEX_PASSWORD)
            page.keyboard.press("Enter")
            page.wait_for_timeout(4000)
        except Exception:
            print("Поле пароля не найдено — возможно нужна ручная авторизация.")
            print("Войди вручную в браузере, затем нажми Enter в этом окне...")
            input()

        # Проверяем куки
        cookies = context.cookies()
        session_id  = next((c["value"] for c in cookies if c["name"] == "Session_id"), None)
        session_id2 = next((c["value"] for c in cookies if c["name"] == "sessionid2"), None)

        if not session_id:
            print("Session_id не найден. Попробуй войти вручную.")
            print("После входа нажми Enter...")
            input()
            cookies = context.cookies()
            session_id  = next((c["value"] for c in cookies if c["name"] == "Session_id"), None)
            session_id2 = next((c["value"] for c in cookies if c["name"] == "sessionid2"), None)

        browser.close()
        return session_id, session_id2


def send_cookies_to_server(session_id, session_id2):
    print(f"\nОтправляю куки на сервер {SERVER_URL}...")
    try:
        resp = requests.post(SERVER_URL, json={
            "session_id": session_id,
            "session_id2": session_id2 or "",
            "secret": SECRET_TOKEN
        }, timeout=10)

        if resp.status_code == 200:
            data = resp.json()
            print(f"✅ Куки успешно обновлены на сервере! ({data['updated_at']})")
        else:
            print(f"❌ Ошибка сервера: {resp.status_code} — {resp.text}")
    except requests.exceptions.ConnectionError:
        print("❌ Не удалось подключиться к серверу. Проверь что VPS доступен.")
    except Exception as e:
        print(f"❌ Ошибка: {e}")


if __name__ == "__main__":
    print("=" * 50)
    print("  Обновление куки Яндекс Бизнес")
    print("=" * 50)

    session_id, session_id2 = get_yandex_cookies()

    if session_id:
        print(f"\nSession_id:  {session_id[:20]}...")
        print(f"sessionid2:  {(session_id2 or '')[:20]}...")
        send_cookies_to_server(session_id, session_id2)
    else:
        print("❌ Не удалось получить куки.")
        sys.exit(1)
