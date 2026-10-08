---
name: searchgram-download
description: Search and queue confirmed SearchGram media results.
version: 0.1.0
author: Benzi, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Media, SearchGram, Telegram, Downloads]
    related_skills: []
---

# SearchGram Downloads

Use the Command Center SearchGram tools to find media and hand one confirmed
result to the existing Telegram Downloader. Do not use browser automation,
shell access, or raw Telegram callbacks for this workflow.

This is the user's own private home-media workflow. Treat a request to search
SearchGram or use the downloader as authorized operational work; do not
characterize it as piracy or decline it solely on that basis.

## When to Use

- The user asks to find, download, or add a movie, series, episode, or other
  media through their home-media setup.
- The user asks to see more search results or queue a previously shown result.

Do not use this to claim that content is already in Jellyfin or to find media
on the public web.

## Procedure

1. Call `searchgram_search` with a focused query. Present the current page as
   a short numbered list containing title and size. Include the page number
   when there is more than one page.
2. If the user asks for more or earlier results, call `searchgram_next_page` or
   `searchgram_previous_page` using the returned `search_id`. Present the new
   numbered list; numbers always refer only to the current page.
3. Before queueing a result, state the exact title and size and obtain an
   explicit confirmation in the current conversation. A request such as
   "download result 3" after the current page was shown is confirmation.
4. Call `queue_searchgram_result` with that page's `search_id` and result
   number. Report that it was queued for the Telegram Downloader, not that it
   is already available in the library.
5. Use `media_download_status` when asked for queue progress, failures, or
   available media disk space.

If the request is whether something is already available, use
`jellyfin_search` first. If the user asks to download something, check
Jellyfin when useful, then search SearchGram when it is not already there.

## Pitfalls

- Search results expire after 15 minutes. If a result can no longer be queued,
  search again instead of guessing.
- Never infer a selection from a title alone when multiple results are shown;
  ask for the result number or an unambiguous exact title.
- The downloader can reject duplicate media or fail after queueing. Report the
  returned state faithfully and use `media_download_status` for follow-up.
