import asyncio
import re
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urlparse

from telethon import TelegramClient
from telethon.errors import BotResponseTimeoutError
from telethon.tl.functions.messages import (
    GetBotCallbackAnswerRequest,
)


_RESULT_RE = re.compile(
    r"^\[(?P<size>[^\]]+)\]\s*(?P<title>.+)$",
    re.DOTALL,
)


@dataclass
class SearchResultItem:
    title: str
    size: str | None
    callback_data: str


@dataclass
class SearchNavigation:
    text: str
    callback_data: str


@dataclass
class SearchPage:
    query: str
    message_id: int
    total_results: int | None
    page: int | None
    total_pages: int | None
    items: list[SearchResultItem]
    navigation: list[SearchNavigation]

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "message_id": self.message_id,
            "total_results": self.total_results,
            "page": self.page,
            "total_pages": self.total_pages,
            "items": [
                asdict(item)
                for item in self.items
            ],
            "navigation": [
                asdict(item)
                for item in self.navigation
            ],
        }


@dataclass
class DeliveredMedia:
    chat_id: int
    message_id: int
    filename: str | None
    file_size: int | None


class SearchGram:
    def __init__(
        self,
        client: TelegramClient,
        search_chat_id: int,
        delivery_bot: str,
    ):
        self.client = client
        self.search_chat_id = (
            search_chat_id
        )
        self.delivery_bot = (
            delivery_bot
        )

        self.search_chat = None
        self.search_peer = None

        self.delivery_entity = None
        self.delivery_chat_id: int | None = (
            None
        )

        # SearchGram is one shared Telegram
        # conversation. Serialize interactions
        # so responses cannot cross.
        self.lock = asyncio.Lock()

    async def initialize(
        self,
    ) -> None:
        self.search_chat = (
            await self.client.get_entity(
                self.search_chat_id
            )
        )

        self.search_peer = (
            await self.client
            .get_input_entity(
                self.search_chat_id
            )
        )

        self.delivery_entity = (
            await self.client.get_entity(
                self.delivery_bot
            )
        )

        self.delivery_chat_id = (
            self.delivery_entity.id
        )

        print(
            "SearchGram initialized: "
            f"search={self.search_chat_id}, "
            f"delivery={self.delivery_chat_id}",
            flush=True,
        )

    async def _wait_for_search_reply(
        self,
        query_message_id: int,
        timeout: float = 15.0,
    ):
        loop = (
            asyncio.get_running_loop()
        )

        deadline = (
            loop.time()
            + timeout
        )

        while (
            loop.time()
            < deadline
        ):
            messages = (
                await self.client
                .get_messages(
                    self.search_chat,
                    limit=30,
                )
            )

            for message in messages:
                reply_to = getattr(
                    message.reply_to,
                    "reply_to_msg_id",
                    None,
                )

                if (
                    not message.out
                    and reply_to
                    == query_message_id
                    and message.buttons
                ):
                    return message

            await asyncio.sleep(
                0.4
            )

        raise TimeoutError(
            "Timed out waiting for "
            "SearchGram results"
        )

    def _button_data(
        self,
        button,
    ) -> str | None:
        data = getattr(
            button,
            "data",
            None,
        )

        if not data:
            return None

        return data.decode(
            "utf-8",
            errors="replace",
        )

    def _parse_total_results(
        self,
        text: str,
    ) -> int | None:
        match = re.search(
            r"תוצאות:\s*(\d+)",
            text,
        )

        if not match:
            return None

        return int(
            match.group(1)
        )

    def _parse_page_indicator(
        self,
        message,
    ) -> tuple[
        int | None,
        int | None,
    ]:
        for row in (
            message.buttons
            or []
        ):
            for button in row:
                text = (
                    getattr(
                        button,
                        "text",
                        "",
                    )
                    or ""
                )

                match = re.search(
                    r"עמוד\s*(\d+)\s*/\s*(\d+)",
                    text,
                )

                if match:
                    return (
                        int(
                            match.group(1)
                        ),
                        int(
                            match.group(2)
                        ),
                    )

        return (
            None,
            None,
        )

    def parse_page(
        self,
        message,
        query: str,
    ) -> SearchPage:
        items = []
        navigation = []

        for row in (
            message.buttons
            or []
        ):
            for button in row:
                callback_data = (
                    self._button_data(
                        button
                    )
                )

                if not callback_data:
                    continue

                text = (
                    getattr(
                        button,
                        "text",
                        "",
                    )
                    or ""
                ).strip()

                if callback_data.startswith(
                    "dl_"
                ):
                    match = (
                        _RESULT_RE.match(
                            text
                        )
                    )

                    if match:
                        size = (
                            match.group(
                                "size"
                            )
                            .strip()
                        )

                        title = (
                            match.group(
                                "title"
                            )
                            .strip()
                        )

                    else:
                        size = None
                        title = text

                    items.append(
                        SearchResultItem(
                            title=title,
                            size=size,
                            callback_data=(
                                callback_data
                            ),
                        )
                    )

                elif callback_data.startswith(
                    "search#"
                ):
                    navigation.append(
                        SearchNavigation(
                            text=text,
                            callback_data=(
                                callback_data
                            ),
                        )
                    )

        page, total_pages = (
            self._parse_page_indicator(
                message
            )
        )

        return SearchPage(
            query=query,
            message_id=message.id,
            total_results=(
                self._parse_total_results(
                    message.message
                    or ""
                )
            ),
            page=page,
            total_pages=total_pages,
            items=items,
            navigation=navigation,
        )

    async def _send_callback(
        self,
        *,
        message_id: int,
        callback_data: str,
    ):
        """
        Send callback data directly.

        We intentionally do NOT try to locate
        the Telegram button object again.

        SearchGram can rebuild/edit its keyboard
        between browser requests, while the
        callback payload itself is sufficient.
        """

        return await self.client(
            GetBotCallbackAnswerRequest(
                peer=self.search_peer,
                msg_id=message_id,
                data=(
                    callback_data
                    .encode(
                        "utf-8"
                    )
                ),
            )
        )

    async def search(
        self,
        query: str,
    ) -> SearchPage:
        query = query.strip()

        if not query:
            raise ValueError(
                "Search query is empty"
            )

        async with self.lock:
            sent = (
                await self.client
                .send_message(
                    self.search_chat,
                    query,
                )
            )

            result = (
                await self
                ._wait_for_search_reply(
                    sent.id
                )
            )

            return self.parse_page(
                result,
                query,
            )

    async def navigate(
        self,
        message_id: int,
        callback_data: str,
        query: str,
    ) -> SearchPage:
        if not callback_data.startswith(
            "search#"
        ):
            raise ValueError(
                "Invalid SearchGram "
                "navigation callback"
            )

        async with self.lock:
            before = (
                await self.client
                .get_messages(
                    self.search_chat,
                    ids=message_id,
                )
            )

            if not before:
                raise ValueError(
                    "Search result message "
                    "no longer exists"
                )

            old_signature = (
                self._message_signature(
                    before
                )
            )

            try:
                await self._send_callback(
                    message_id=message_id,
                    callback_data=(
                        callback_data
                    ),
                )

            except BotResponseTimeoutError:
                print(
                    "SearchGram navigation callback "
                    "timed out; waiting for page update anyway",
                    flush=True,
                )

            loop = (
                asyncio.get_running_loop()
            )

            deadline = (
                loop.time()
                + 10.0
            )

            while (
                loop.time()
                < deadline
            ):
                refreshed = (
                    await self.client
                    .get_messages(
                        self.search_chat,
                        ids=message_id,
                    )
                )

                if (
                    refreshed
                    and self._message_signature(
                        refreshed
                    )
                    != old_signature
                ):
                    return (
                        self.parse_page(
                            refreshed,
                            query,
                        )
                    )

                await asyncio.sleep(
                    0.3
                )

            #
            # Some SearchGram versions send a
            # new result message instead of
            # editing the previous one.
            #
            recent = (
                await self.client
                .get_messages(
                    self.search_chat,
                    limit=20,
                )
            )

            candidates = []

            for message in recent:
                if (
                    message.out
                    or not message.buttons
                ):
                    continue

                text = (
                    message.message
                    or ""
                )

                if (
                    "תוצאות חיפוש"
                    not in text
                ):
                    continue

                candidates.append(
                    message
                )

            if candidates:
                newest = max(
                    candidates,
                    key=lambda item:
                        item.id,
                )

                return self.parse_page(
                    newest,
                    query,
                )

            raise TimeoutError(
                "SearchGram page "
                "navigation timed out"
            )

    def _message_signature(
        self,
        message,
    ) -> tuple:
        buttons = []

        for row in (
            message.buttons
            or []
        ):
            for button in row:
                buttons.append(
                    (
                        getattr(
                            button,
                            "text",
                            None,
                        ),
                        self._button_data(
                            button
                        ),
                    )
                )

        return (
            message.message,
            tuple(
                buttons
            ),
        )

    def _start_token_from_callback(
        self,
        callback_result,
    ) -> str:
        url = getattr(
            callback_result,
            "url",
            None,
        )

        if not url:
            raise RuntimeError(
                "SearchGram callback "
                "did not return a URL"
            )

        parsed = urlparse(
            url
        )

        params = parse_qs(
            parsed.query
        )

        values = params.get(
            "start"
        )

        if not values:
            raise RuntimeError(
                "SearchGram callback URL "
                "contains no start token"
            )

        return values[0]

    async def _wait_for_delivery(
        self,
        *,
        after_message_id: int,
        timeout: float = 30.0,
    ):
        loop = (
            asyncio.get_running_loop()
        )

        deadline = (
            loop.time()
            + timeout
        )

        while (
            loop.time()
            < deadline
        ):
            messages = (
                await self.client
                .get_messages(
                    self.delivery_entity,
                    limit=20,
                )
            )

            candidates = [
                message
                for message in messages
                if (
                    message.id
                    > after_message_id
                    and not message.out
                    and message.file
                )
            ]

            if candidates:
                return min(
                    candidates,
                    key=lambda item:
                        item.id,
                )

            await asyncio.sleep(
                0.4
            )

        raise TimeoutError(
            "Timed out waiting for "
            "SearchGram media delivery"
        )

    async def request_media(
        self,
        *,
        message_id: int,
        callback_data: str,
    ) -> DeliveredMedia:
        if not callback_data.startswith(
            "dl_"
        ):
            raise ValueError(
                "Invalid SearchGram "
                "download callback"
            )

        async with self.lock:
            search_message = (
                await self.client
                .get_messages(
                    self.search_chat,
                    ids=message_id,
                )
            )

            if not search_message:
                raise ValueError(
                    "Search result message "
                    "no longer exists"
                )

            latest = (
                await self.client
                .get_messages(
                    self.delivery_entity,
                    limit=1,
                )
            )

            before_id = (
                latest[0].id
                if latest
                else 0
            )

            #
            # Important:
            # send callback directly rather than
            # looking for the original button.
            #
            callback_result = (
                await self._send_callback(
                    message_id=message_id,
                    callback_data=(
                        callback_data
                    ),
                )
            )

            token = (
                self
                ._start_token_from_callback(
                    callback_result
                )
            )

            print(
                "SearchGram selected "
                f"{callback_data}; "
                f"delivery token={token}",
                flush=True,
            )

            sent = (
                await self.client
                .send_message(
                    self.delivery_entity,
                    f"/start {token}",
                )
            )

            print(
                "SearchGram delivery request "
                f"sent: message={sent.id}",
                flush=True,
            )

            media_message = (
                await self
                ._wait_for_delivery(
                    after_message_id=(
                        before_id
                    )
                )
            )

            print(
                "SearchGram delivered media: "
                f"message={media_message.id}, "
                f"file="
                f"{media_message.file.name}",
                flush=True,
            )

            return DeliveredMedia(
                chat_id=(
                    self.delivery_chat_id
                ),
                message_id=(
                    media_message.id
                ),
                filename=(
                    media_message.file.name
                    if media_message.file
                    else None
                ),
                file_size=(
                    media_message.file.size
                    if media_message.file
                    else None
                ),
            )
