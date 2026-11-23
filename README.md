# The Bench

A self-built YouTube homepage: the best maker videos of the day, no doom, no markets, no news
(except Fireship and AI Explained). Rebuilt every morning by GitHub Actions, served by GitHub Pages.

## Set up (5 minutes)
1. Push this folder to a new GitHub repo.
2. **Settings → Pages**: deploy from branch `main`, folder `/docs`.
3. **Actions → Build daily feed → Run workflow** to fill it the first time. After that it runs daily at about 5:17am Mountain.

## Make it smarter (optional secrets: Settings → Secrets → Actions)
- `YOUTUBE_API_KEY`: also searches YouTube for new maker videos from channels you don't follow (free Google Cloud key, YouTube Data API v3).
- `ANTHROPIC_API_KEY`: Claude reads every title and drops anything gloomy the keyword filter missed, then writes a short blurb for each card.

## Tune it
- Any channel under 100K subscribers (`INDIE_MAX_SUBS`) is tagged indie automatically and indie channels get at least half the feed (`INDIE_SHARE`). Subscriber counts are cached in `data/subs.json` and re-checked weekly, so a channel that grows past 100K graduates on its own.
- Every video is scored against its own channel's usual views, so a small maker's breakout beats a big channel's average upload.
- `channels.json`: `always` channels skip the filter; everything else goes in `channels`. Entries are `["Name", "UC…channelId"]` or an `@handle`. `search_queries` drives discovery.
- `scripts/build_feed.py`: the `BLOCK` keyword list, how many videos to show, and the time window.
- The page remembers which videos you've watched (per browser), and there's a "hide watched" toggle.
