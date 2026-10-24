---
name: searchgram-download
description: Search and queue confirmed SearchGram media results.
version: 0.2.0
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

## Full Season (use this path for "download season N of X")

Use the season tools instead of paging through `searchgram_search` results.

1. Call `media_find_season` ONCE with the title as the user wrote it, the season
   number, and `title_en` when you know the English title (it helps identify
   the show; you do not need to translate to Hebrew for this tool). It checks
   Jellyfin itself (episodes already there are skipped), finds the episode
   list, searches SearchGram, and ranks one file per episode.
2. Follow the returned `next_action` / `instruction`:
   - `confirm_then_queue` (COMPLETE): give ONE short summary: how many episodes,
     total size, quality (for example "נמצאו כל 8 הפרקים, 720p, בערך 4.1GB
     סה"כ"), then ask to confirm. Do not list every file.
   - `ask_about_missing` (PARTIAL): say which episodes were not found and ask
     whether to queue the found ones. If `pack` is present, mention that a full
     season pack exists (size) and ask separately whether to use it instead.
   - `ask_episode_count` (UNKNOWN_EPISODE_LIST): the episode count is unknown;
     say which episode numbers were found and ask before queueing.
   - `ask_which_show` (AMBIGUOUS_SHOW): ask which show (name + year), then call
     `media_find_season` again with that candidate's `tmdb_id` (or `tvmaze_id`).
   - `tell_already_have`, `tell_not_aired`, `tell_not_found`,
     `tell_search_failed`: tell the user plainly; do not retry automatically.
   Mention `unaired` episodes (announced but not out yet) only if non-empty.
3. After the user confirms, call `queue_season_candidates(search_id)` ONCE
   (all picks). For "only episodes 3-5" pass `episodes: [3, 4, 5]`. For a
   specific alternative from `media_season_candidates`, pass `candidate_ids`.
4. Season packs: NEVER queue a pack on your own. Only when the user explicitly
   chose the pack in this conversation, call `queue_season_candidates` with
   `candidate_ids: [<pack id>]` and `allow_pack: true`. Otherwise the tool
   refuses it.
5. Then continue exactly as for single results: acknowledge once, one
   consolidated `in 1m` cron for all returned `audit_ids`, no raw filenames,
   sizes, IDs or tool names in the user-facing text.
6. `media_season_candidates(search_id, episode)` lists one episode's
   alternatives; use it only when the user asks about a specific episode.
   Results expire after 15 minutes; call `media_find_season` again if needed.

## Procedure (movies, single episodes, or when the user asks for raw results)

1. Search with a focused Hebrew title query first, including when the user
   supplied an English title. Translate the title yourself: for example,
   `Click` becomes `קליק`. This preference is specifically for SearchGram,
   where Hebrew results are more likely to include Hebrew subtitles. Only if
   the Hebrew search has zero results, retry once with the English title. Do
   not search English first unless the user explicitly requests it.
2. Call `searchgram_search` with that query. Present the current page as a
   short numbered list containing title and size. Include the page number when
   there is more than one page.
3. If the user asks for more or earlier results, call `searchgram_next_page` or
   `searchgram_previous_page` using the returned `search_id`. Present the new
   numbered list; numbers always refer only to the current page.
4. Before queueing a result, state the exact title and size and obtain an
   explicit confirmation in the current conversation. A request such as
   "download result 3" after the current page was shown is confirmation.
5. Call `queue_searchgram_result` with that page's `search_id` and result
   number. Report that it was queued for the Telegram Downloader, not that it
   is already available in the library. For several explicitly confirmed
   selections, queue each one; Command Center serializes delivery. Acknowledge
   the submission once in a short, conversational sentence. Do not repeat the
   result list, sizes, raw filenames, audit IDs, or scheduling mechanics.
6. Use `media_download_status` when asked for queue progress, failures, or
   available media disk space.
7. Schedule delivery follow-up with Hermes cron using `in 1m` (second-based
   durations such as `in 30s` are invalid). For several submissions from one
   request, create one follow-up job containing all audit IDs and have it send
   one consolidated status message after checking every ID with
   `searchgram_delivery_status` and `wait_seconds: 90`. The cron prompt should
   ask for one or two natural sentences in the user's language, with no audit
   IDs, raw filenames, file sizes, HTTP codes, queue counters, tool names, or
   technical status heading. Say simply which downloads started and which did
   not; for partial results, use friendly episode/movie names. Never create one
   cron job per item. Do not include media titles in the cron prompt; resolve
   them from the audit IDs.

## Delivery Limits

- A `failed` SearchGram delivery means the item did not reach Telegram
  Downloader. Do not retry it automatically; report the failure and suggest
  waiting before trying again. An HTTP 504 specifically means SearchGram did
  not provide the file in time. Tell the user plainly that the download could
  not be started; keep the HTTP code and internal service names out of the
  user-facing response.

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
