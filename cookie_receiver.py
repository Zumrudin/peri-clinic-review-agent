"""
REST сервер для приёма куки от локального скрипта на Windows.
Запускается через PM2 на VPS.
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import json
import os
from datetime import datetime

app = FastAPI()

COOKIES_FILE = "/root/review-agent/data/yandex_cookies.json"


class CookiesPayload(BaseModel):
    session_id: str
    session_id2: str
    secret: str  # простой секретный токен для защиты эндпоинта


SECRET_TOKEN = os.getenv("COOKIE_SECRET", "change_me_in_env")


@app.post("/update-cookies")
def update_cookies(payload: CookiesPayload):
    if payload.secret != SECRET_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid secret")

    data = {
        "Session_id": payload.session_id,
        "sessionid2": payload.session_id2,
        "updated_at": datetime.now().isoformat()
    }

    os.makedirs(os.path.dirname(COOKIES_FILE), exist_ok=True)
    with open(COOKIES_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"[{datetime.now()}] Куки обновлены")
    return {"status": "ok", "updated_at": data["updated_at"]}


@app.get("/cookies-status")
def cookies_status(secret: str):
    if secret != SECRET_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid secret")

    if not os.path.exists(COOKIES_FILE):
        return {"status": "no_cookies"}

    with open(COOKIES_FILE) as f:
        data = json.load(f)

    return {"status": "ok", "updated_at": data.get("updated_at")}


@app.get("/health")
def health():
    return {"status": "ok"}
