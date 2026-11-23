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

## Stats page (`stats.html`)
Every tracked video from the last 30 days, with 7-day and 30-day views. It covers:
- **What got the views:** the top 10 by raw views and by *breakout* (× the channel's normal pace), with chips showing which traits each shares with other overperformers.
- **Length vs views:** median or average views per length bucket, plus a scatter of every video.
- **Titles:** how much better or worse titles with a trait did (numbers, "$", "I built", ALL CAPS, emoji and so on), title length, and the most common words in breakout titles.
- **Thumbnails:** brightness, saturation, colorfulness, contrast, busyness and warmth, split into quarters, plus a wall of the top breakout thumbnails with their three main colors.
- **When it was posted:** day of week, hour (Mountain time) and YouTube category.
- **Channels:** a sortable leaderboard.

How the data is gathered:
- Each daily run saves every video into `data/videos.json`, along with its view history. The first time it sees a video, it reads the length, category, tags and likes from the public watch page, and analyzes the thumbnail with Pillow.
- `docs/videos.json` is the 30-day export the stats page reads.
- Missing days in the last 30 are rebuilt automatically. Those rebuilt days are ranked using *today's* view counts, so their order isn't exactly what the feed would have shown on the day.
- The RSS feed only lists each channel's 15 newest uploads, so channels that post daily won't have a full 30 days of history until the tracker has run for a while.
