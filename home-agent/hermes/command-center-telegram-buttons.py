"""Native Telegram buttons for narrowly scoped Command Center approvals.

Loaded as ``sitecustomize`` before Hermes starts. This keeps Telegram callback
handling in Hermes (where the user allowlist is enforced), while Command Center
remains the sole authority that can execute an approved action.
"""
import asyncio
import json
import os
import re
import urllib.error
import urllib.request

from plugins.platforms.telegram import adapter as telegram_adapter


MARKER = re.compile(r"\s*\[\[CC_APPROVAL:([0-9a-f]{12,64})\]\]\s*$")
ORIGINAL_SEND = telegram_adapter.TelegramAdapter.send
ORIGINAL_CALLBACK = telegram_adapter.TelegramAdapter._handle_callback_query


def command_center_callback(action, approval_id):
    base = os.environ.get("COMMAND_CENTER_CALLBACK_URL", "http://command-center:8080/v1").rstrip("/")
    token = os.environ.get("COMMAND_CENTER_CALLBACK_TOKEN", "")
    if not token:
        raise RuntimeError("Command Center button callback is not configured")
    request = urllib.request.Request(
        f"{base}/approvals/{approval_id}/{action}", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "X-Command-Center-Callback": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc)
        except Exception:
            detail = {"error": f"HTTP {exc.code}"}
        raise RuntimeError(str(detail.get("error", f"HTTP {exc.code}"))) from exc


async def send_with_command_center_buttons(self, chat_id, content, reply_to=None, metadata=None):
    match = MARKER.search(content or "")
    if not match:
        return await ORIGINAL_SEND(self, chat_id, content, reply_to, metadata)
    approval_id = match.group(1)
    clean_content = content[:match.start()].rstrip()
    result = await ORIGINAL_SEND(self, chat_id, clean_content, reply_to, metadata)
    if not result.success or not getattr(self, "_bot", None):
        return result
    keyboard = telegram_adapter.InlineKeyboardMarkup([[
        telegram_adapter.InlineKeyboardButton("✅ Approve", callback_data=f"cc:a:{approval_id}"),
        telegram_adapter.InlineKeyboardButton("❌ Deny", callback_data=f"cc:d:{approval_id}"),
    ]])
    button_result = await self._send_control_message(
        chat_id, "Choose an action:", parse_mode=None,
        thread_id=self._metadata_thread_id(metadata), metadata=metadata,
        reply_markup=keyboard, reply_to_mode=self._reply_to_mode,
    )
    return button_result if not button_result else result


async def handle_command_center_callback(self, query, data, cb):
    if not await self._callback_authorized(query, cb, telegram_adapter._UNAUTHORIZED):
        return
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[1] not in {"a", "d"} or not re.fullmatch(r"[0-9a-f]{12,64}", parts[2]):
        await query.answer(text="Invalid approval data.")
        return
    action = "approve" if parts[1] == "a" else "deny"
    try:
        result = await asyncio.to_thread(command_center_callback, action, parts[2])
        label = "✅ Approved and executed" if action == "approve" else "❌ Denied"
        if result.get("status") == "denied":
            label = "❌ Denied"
    except Exception as exc:
        label = f"⚠️ Could not resolve approval: {exc}"
    await query.answer(text=label[:180])
    await self._edit_md_quiet(query, label)


async def callback_with_command_center_buttons(self, update, context):
    query = update.callback_query
    if query and query.data and query.data.startswith("cc:"):
        self._accept_update()
        await handle_command_center_callback(self, query, query.data, self._callback_ctx(query))
        return
    return await ORIGINAL_CALLBACK(self, update, context)


telegram_adapter.TelegramAdapter.send = send_with_command_center_buttons
telegram_adapter.TelegramAdapter._handle_callback_query = callback_with_command_center_buttons
