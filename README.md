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
- `channels.json`: `always` channels skip the filter. Add or remove `makers` handles. `search_queries` drives discovery.
- `scripts/build_feed.py`: the `BLOCK` keyword list, how many videos to show, and the time window.
- The page remembers which videos you've watched (per browser), and there's a "hide watched" toggle.
