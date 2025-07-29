import asyncio
import os
import string
import random
import logging

# 引入 Quart 相關依賴
from quart import Quart, request, jsonify
from quart_cors import cors

# 引入 httpx 作為異步 requests 的替代品
import httpx

# --- 1. 設定基礎日誌 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 2. 定義自訂異常 ---
class MailTmError(Exception):
    """MailTmClient 的基礎異常類別"""
    pass

class AccountCreationError(MailTmError):
    """帳號創建失敗時引發"""
    pass

class MessageTimeoutError(MailTmError):
    """等待訊息超時時引發"""
    pass

# --- 3. 隨機字串產生器 ---
def generate_random_username(length=10):
    letters = string.ascii_lowercase + string.digits
    return ''.join(random.choice(letters) for _ in range(length))

# --- 4. 重構後的異步核心類別 ---
class MailTmClient:
    """
    一個用於與 api.smtp.dev (Mail.tm) 互動的異步客戶端。
    """
    def __init__(self, api_key: str, base_url: str = "https://api.smtp.dev"):
        if not api_key:
            raise ValueError("API key 不可為空")

        self.base_url = base_url
        # 使用 httpx.AsyncClient 進行異步請求
        self.session = httpx.AsyncClient(headers={
            "X-API-KEY": api_key,
            "Accept": "application/json"
        }, timeout=20.0) # 增加超時時間

        self.address: str | None = None
        self.password: str | None = None
        self.account_id: str | None = None
        self.mailbox_id: str | None = None

    async def _request(self, method: str, endpoint: str, **kwargs) -> dict | list | None:
        """統一的異步請求處理方法"""
        url = f"{self.base_url}/{endpoint}"
        try:
            response = await self.session.request(method, url, **kwargs)
            response.raise_for_status()
            if response.status_code == 204:
                return None
            return response.json()
        except httpx.HTTPStatusError as e:
            logging.error(f"HTTP 錯誤: {e.response.status_code} - {e.response.text}")
            raise MailTmError(f"API 請求失敗: {e.response.text}") from e
        except httpx.RequestError as e:
            logging.error(f"請求發生錯誤: {e}")
            raise MailTmError(f"網路連線錯誤: {e}") from e

    @classmethod
    async def create_new_account(
            cls,
            api_key: str,
            domain: str = "vvvcx.me",
            password: str = "thisispassword",
            max_retries: int = 3
    ) -> 'MailTmClient':
        """工廠方法：異步創建一個全新的臨時信箱"""
        instance = cls(api_key)
        for attempt in range(max_retries):
            try:
                username = generate_random_username()
                address = f"{username}@{domain}"
                logging.info(f"嘗試 {attempt + 1}/{max_retries}: 創建信箱 {address}...")

                account_data = await instance._create_account(address, password)
                instance.account_id = account_data["id"]
                instance.address = address
                instance.password = password
                logging.info(f"帳號創建成功, ID: {instance.account_id}")

                mailboxes = account_data.get("mailboxes", [])
                inbox = next((mb for mb in mailboxes if mb.get("path") == "INBOX"), None)
                if not inbox:
                    raise MailTmError("在帳號中未找到 INBOX")
                instance.mailbox_id = inbox["id"]
                logging.info(f"找到 INBOX, Mailbox ID: {instance.mailbox_id}")

                return instance

            except MailTmError as e:
                logging.warning(f"創建過程中發生錯誤: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(3)
                else:
                    raise AccountCreationError(f"創建帳號失敗，已達最大重試次數: {e}") from e
        raise AccountCreationError("未知錯誤導致帳號創建失敗。")

    async def _create_account(self, address: str, password: str) -> dict:
        """私有方法：呼叫 API 創建一個帳號"""
        payload = {"address": address, "password": password}
        headers = {"Content-Type": "application/json"}
        return await self._request("POST", "accounts", json=payload, headers=headers)

    async def get_latest_message(self) -> dict | None:
        """異步獲取收件匣中的最新一封郵件"""
        if not self.account_id or not self.mailbox_id:
            raise MailTmError("帳號資訊未初始化，無法獲取訊息。")
        endpoint = f"accounts/{self.account_id}/mailboxes/{self.mailbox_id}/messages"
        messages = await self._request("GET", endpoint)
        if messages:
            latest_message = messages[0]
            logging.info(f"收到新訊息: {latest_message.get('intro')}")
            return latest_message
        return None

    async def wait_for_message(self, timeout: int = 60, interval: int = 5) -> dict:
        """異步等待並獲取最新郵件"""
        start_time = asyncio.get_event_loop().time()
        logging.info(f"開始等待郵件，最長等待 {timeout} 秒...")
        while asyncio.get_event_loop().time() - start_time < timeout:
            message = await self.get_latest_message()
            if message:
                return message
            await asyncio.sleep(interval)
        raise MessageTimeoutError(f"在 {timeout} 秒內未收到任何郵件。")

    async def delete_account(self):
        """異步刪除帳號"""
        if not self.account_id:
            logging.warning("沒有可供刪除的 account_id。")
            return
        logging.info(f"正在刪除帳號: {self.account_id}")
        await self._request("DELETE", f"accounts/{self.account_id}")
        logging.info(f"帳號 {self.account_id} 刪除成功。")
        self.account_id = None
        self.address = None

    async def close_session(self):
        """關閉 httpx session"""
        await self.session.aclose()


# --- 5. Quart API 應用 ---
app = Quart(__name__)
# 允許所有來源的跨域請求，方便前端測試
app = cors(app, allow_origin="*")

# 從環境變數讀取 API KEY，這是部署的最佳實踐
MAILTM_API_KEY = os.environ.get("MAILTM_API_KEY")

@app.before_serving
async def startup():
    if not MAILTM_API_KEY:
        logging.error("重大錯誤: 環境變數 MAILTM_API_KEY 未設定!")
        # 在實際應用中，你可能希望應用程式無法啟動
        # raise ValueError("MAILTM_API_KEY environment variable not set.")

@app.route("/")
async def index():
    return jsonify({"status": "ok", "message": "MailTm API wrapper is running."})

@app.route("/api/create_account", methods=["POST"])
async def api_create_account():
    """API 端點：創建一個新的臨時信箱"""
    if not MAILTM_API_KEY:
        return jsonify({"error": "Server is not configured with an API key."}), 500

    client = None
    try:
        # 使用工廠方法創建實例
        client = await MailTmClient.create_new_account(api_key=MAILTM_API_KEY)
        response_data = {
            "message": "Account created successfully",
            "address": client.address,
            "password": client.password,
            "accountId": client.account_id,
            "mailboxId": client.mailbox_id
        }
        return jsonify(response_data), 201 # 201 Created
    except AccountCreationError as e:
        logging.error(f"API - 無法創建帳號: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if client:
            await client.close_session()

@app.route("/api/wait_for_message", methods=["POST"])
async def api_wait_for_message():
    """API 端點：為指定的帳號等待郵件"""
    if not MAILTM_API_KEY:
        return jsonify({"error": "Server is not configured with an API key."}), 500

    data = await request.get_json()
    if not data or "accountId" not in data or "mailboxId" not in data:
        return jsonify({"error": "Missing 'accountId' or 'mailboxId' in request body"}), 400

    account_id = data["accountId"]
    mailbox_id = data["mailboxId"]
    timeout = data.get("timeout", 60)
    interval = data.get("interval", 5)

    client = MailTmClient(api_key=MAILTM_API_KEY)
    client.account_id = account_id
    client.mailbox_id = mailbox_id

    try:
        message = await client.wait_for_message(timeout=int(timeout), interval=int(interval))
        return jsonify(message), 200
    except MessageTimeoutError as e:
        return jsonify({"error": str(e)}), 408 # 408 Request Timeout
    except MailTmError as e:
        return jsonify({"error": str(e)}), 500
    finally:
        await client.close_session()

@app.route("/api/delete_account/<string:account_id>", methods=["DELETE"])
async def api_delete_account(account_id: str):
    """API 端點：刪除指定的帳號"""
    if not MAILTM_API_KEY:
        return jsonify({"error": "Server is not configured with an API key."}), 500

    client = MailTmClient(api_key=MAILTM_API_KEY)
    client.account_id = account_id
    try:
        await client.delete_account()
        return jsonify({"message": f"Account {account_id} deleted successfully."}), 200
    except MailTmError as e:
        return jsonify({"error": str(e)}), 500
    finally:
        await client.close_session()

# Vercel 會使用它自己的伺服器，所以本地運行的 Hypercorn 部分可以移除或保留在 if __name__ == "__main__": 中供本地測試
if __name__ == "__main__":
    # 為了本地測試，你可以這樣運行:
    # 1. 在終端設置環境變數: export MAILTM_API_KEY='你的金鑰'
    # 2. 運行 python app.py
    if not MAILTM_API_KEY:
        print("請先設定環境變數 'MAILTM_API_KEY'")
    else:
        from hypercorn.asyncio import serve
        from hypercorn.config import Config

        config = Config()
        config.bind = ["0.0.0.0:8000"]
        asyncio.run(serve(app, config))
