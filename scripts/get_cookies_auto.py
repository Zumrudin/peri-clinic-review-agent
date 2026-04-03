"""
Автоматическое обновление куки Яндекс — Mac и Windows.

Принцип:
  1. Первый запуск: открывает браузер, ты входишь вручную, профиль сохраняется.
  2. Все следующие запуски: браузер запускается СКРЫТНО, использует сохранённый
     профиль (уже залогинен), обновляет куки без участия пользователя.

Установка:
  pip3 install playwright requests
  python3 -m playwright install chromium

Первый запуск (вручную один раз):
  python3 get_cookies_auto.py

Автоматический запуск каждые 2 дня:
  Mac:     следуй инструкции ниже — launchd
  Windows: следуй инструкции ниже — Task Scheduler

─── Инструкция для Mac (launchd) ────────────────────────────────────────────
1. Сохрани этот скрипт в ~/Documents/peri_clinic/get_cookies_auto.py
2. Создай файл ~/Library/LaunchAgents/ru.periclinic.cookies.plist со содержимым:

<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ru.periclinic.cookies</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/ИМЯ_ПОЛЬЗОВАТЕЛЯ/Documents/peri_clinic/get_cookies_auto.py</string>
        <string>--auto</string>
    </array>
    <key>StartInterval</key>
    <integer>172800</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/tmp/periclinic_cookies.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/periclinic_cookies_err.log</string>
</dict>
</plist>

3. Замени ИМЯ_ПОЛЬЗОВАТЕЛЯ на своё (узнать: whoami в терминале)
4. Активируй: launchctl load ~/Library/LaunchAgents/ru.periclinic.cookies.plist

─── Инструкция для Windows (Task Scheduler) ─────────────────────────────────
1. Открой Task Scheduler → Create Basic Task
2. Trigger: Daily, повторять каждые 2 дня
3. Action: Start a program
   Program: python
   Arguments: C:\\путь\\до\\get_cookies_auto.py --auto
4. OK
─────────────────────────────────────────────────────────────────────────────
"""
import sys
import json
import requests
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright

# ─── Настройки ───────────────────────────────────────────────
YANDEX_LOGIN    = "periclinic.ai@yandex.ru"
YANDEX_PASSWORD = "270914-Kamilla"

SERVER_URL   = "http://89.22.233.73:8765/update-cookies"
SECRET_TOKEN = "periclinic2024secret"

# Telegram — для уведомлений при ошибках (можно оставить пустым)
TELEGRAM_TOKEN   = "8353884447:AAG3kImyhYlHhDac8qGGMeRC4-0VovBtY1M"
TELEGRAM_CHAT_ID = "385578542"

# Файл сохранённого профиля браузера (создаётся рядом со скриптом)
PROFILE_FILE = Path(__file__).parent / "browser_profile.json"
# ─────────────────────────────────────────────────────────────

AUTO_MODE = "--auto" in sys.argv  # скрытный запуск без окна


def send_telegram(text: str):
    """Отправляет уведомление в Telegram при ошибке."""
    if not TELEGRAM_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10
        )
    except Exception:
        pass


def is_mac():
    return sys.platform == "darwin"


def get_user_agent():
    if is_mac():
        return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"


def get_cookies_headless() -> tuple:
    """
    Скрытный режим: загружает сохранённый профиль, заходит на Яндекс,
    извлекает свежие куки без участия пользователя.
    """
    if not PROFILE_FILE.exists():
        raise FileNotFoundError("Профиль не найден — сначала запусти скрипт без --auto")

    print("🤖 Запускаю браузер в фоне (скрытный режим)...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=get_user_agent(),
            storage_state=str(PROFILE_FILE),
        )
        page = context.new_page()

        # Заходим на Яндекс Бизнес — это освежает сессию
        page.goto("https://yandex.ru/sprav/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

        cookies = context.cookies()
        session_id  = next((c["value"] for c in cookies if c["name"] == "Session_id"), None)
        session_id2 = next((c["value"] for c in cookies if c["name"] == "sessionid2"), None)

        # Сохраняем обновлённый профиль
        context.storage_state(path=str(PROFILE_FILE))
        browser.close()

    if not session_id:
        raise RuntimeError("Session_id не найден в сохранённом профиле — сессия истекла")

    return session_id, session_id2


def get_cookies_manual() -> tuple:
    """
    Ручной режим: открывает видимый браузер, логинится автоматически
    (или вручную если не получилось), сохраняет профиль.
    """
    print("🌐 Открываю браузер для входа...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent=get_user_agent(),
            # Загружаем старый профиль если есть
            storage_state=str(PROFILE_FILE) if PROFILE_FILE.exists() else None,
        )
        page = context.new_page()

        print("Перехожу на страницу входа...")
        page.goto("https://passport.yandex.ru/auth", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        # Пробуем автоматический вход
        try:
            page.locator("input[type='text'], input[type='email'], input:not([type])").first.fill(YANDEX_LOGIN)
            page.keyboard.press("Enter")
            page.wait_for_timeout(3000)
            page.wait_for_selector("input[type='password']", timeout=8000)
            page.locator("input[type='password']").fill(YANDEX_PASSWORD)
            page.keyboard.press("Enter")
            page.wait_for_timeout(4000)
            print("✅ Автологин выполнен")
        except Exception:
            print("⚠️  Автологин не удался. Войди вручную в открывшемся браузере.")
            print("   После входа нажми Enter здесь...")
            input()

        cookies = context.cookies()
        session_id  = next((c["value"] for c in cookies if c["name"] == "Session_id"), None)
        session_id2 = next((c["value"] for c in cookies if c["name"] == "sessionid2"), None)

        if not session_id:
            print("Session_id не найден. Войди вручную и нажми Enter...")
            input()
            cookies = context.cookies()
            session_id  = next((c["value"] for c in cookies if c["name"] == "Session_id"), None)
            session_id2 = next((c["value"] for c in cookies if c["name"] == "sessionid2"), None)

        # Сохраняем профиль для будущих автозапусков
        context.storage_state(path=str(PROFILE_FILE))
        print(f"💾 Профиль браузера сохранён: {PROFILE_FILE}")

        browser.close()
    return session_id, session_id2


def send_cookies_to_server(session_id: str, session_id2: str):
    print(f"📡 Отправляю куки на сервер...")
    resp = requests.post(SERVER_URL, json={
        "session_id": session_id,
        "session_id2": session_id2 or "",
        "secret": SECRET_TOKEN,
    }, timeout=10)

    if resp.status_code == 200:
        data = resp.json()
        print(f"✅ Куки обновлены! ({data['updated_at']})")
        return True
    else:
        raise RuntimeError(f"Сервер вернул {resp.status_code}: {resp.text}")


if __name__ == "__main__":
    print("=" * 50)
    print(f"  Обновление куки Яндекс Бизнес")
    print(f"  Режим: {'автоматический' if AUTO_MODE else 'ручной'}")
    print("=" * 50)

    try:
        if AUTO_MODE:
            session_id, session_id2 = get_cookies_headless()
        else:
            session_id, session_id2 = get_cookies_manual()

        if not session_id:
            raise RuntimeError("Не удалось получить Session_id")

        print(f"Session_id:  {session_id[:20]}...")
        send_cookies_to_server(session_id, session_id2)

    except FileNotFoundError as e:
        print(f"\n⚠️  {e}")
        print("Запусти скрипт БЕЗ --auto для первоначальной настройки.")
        send_telegram(f"⚠️ Куки: профиль не найден. Запусти get_cookies_auto.py вручную.")
        sys.exit(1)

    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        if AUTO_MODE:
            # В автоматическом режиме — уведомляем в Telegram
            send_telegram(
                f"❌ Не удалось автоматически обновить куки Яндекса.\n"
                f"Ошибка: {e}\n\n"
                f"Запусти вручную: python3 get_cookies_auto.py"
            )
        sys.exit(1)

    if not AUTO_MODE:
        input("\nНажми Enter для выхода...")
