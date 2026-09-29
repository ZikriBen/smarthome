import asyncio

import aiohttp

from app.config import Config


class JellyfinClient:
    def __init__(
        self,
        config: Config,
    ):
        self.base_url = config.jellyfin_url
        self.api_key = config.jellyfin_api_key

    @property
    def enabled(self) -> bool:
        return bool(
            self.base_url
            and self.api_key
        )

    async def refresh_library(self) -> bool:
        if not self.enabled:
            print(
                "Jellyfin refresh disabled: "
                "missing URL or API key",
                flush=True,
            )
            return False

        url = (
            f"{self.base_url}"
            "/Library/Refresh"
        )

        headers = {
            "Authorization": (
                f'MediaBrowser Token="{self.api_key}"'
            ),
        }

        timeout = aiohttp.ClientTimeout(
            total=10,
        )

        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
            ) as session:
                async with session.post(
                    url,
                    headers=headers,
                ) as response:
                    if response.status in (
                        200,
                        204,
                    ):
                        print(
                            "Jellyfin library refresh requested",
                            flush=True,
                        )
                        return True

                    body = await response.text()

                    print(
                        "Jellyfin refresh failed: "
                        f"HTTP {response.status} "
                        f"{body[:200]}",
                        flush=True,
                    )

                    return False

        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ) as exc:
            print(
                "Jellyfin refresh failed: "
                f"{type(exc).__name__}: "
                f"{exc}",
                flush=True,
            )

            return False
