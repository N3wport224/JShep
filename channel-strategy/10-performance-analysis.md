# Phase 10: Performance Analysis Protocol (Post-Publish)

*This phase activates once a video is live. Paste the data block below into a message and this protocol produces the diagnosis. Until then, this file is the framework + benchmarks so the analysis is instant and consistent.*

## Data to Provide (copy-paste template)

```
Video: <title>
Days live: <n>
Views: <n>            Impressions: <n>          CTR: <n>%
Avg view duration: <mm:ss>   Avg % viewed: <n>%
Retention graph: <describe: where are the cliffs/bumps, e.g. "drops to 55% by 0:30, slow slide to 35% at 5:00, cliff at 7:40">
Watch time (hours): <n>
Traffic sources: <browse/suggested/search/shorts/external %>
Subs gained: <n>     Likes/comments: <n>/<n>
```

## Benchmarks (faceless finance explainers, small/new channel)

| Metric | Underperforming | Healthy | Outlier |
|---|---|---|---|
| CTR (browse-dominant) | < 3% | 4–6% | > 8% |
| Avg % viewed (10-min video) | < 35% | 40–50% | > 55% |
| 0:30 retention | < 60% | 65–75% | > 80% |
| Views vs channel median (day 7) | < 0.7x | ~1x | > 3x |
| Subs per 1,000 views | < 2 | 3–6 | > 10 |

## Diagnosis Decision Tree (find the *binding constraint* — fix one thing per video)

1. **Impressions low** (< ~2K/day early)? → Topic selection or channel trust problem, *not* packaging. Check: is the topic in a proven-demand cluster (Phase 1 §3)? Was upload consistent? Algorithm tests packaging only after impressions exist — don't redesign thumbnails yet.
2. **Impressions OK, CTR < 4%?** → **Packaging problem.** Title/thumbnail mismatch with the promise, thumbnail unreadable at 120px, or title/thumbnail redundancy (Phase 6 rule violated). Action: swap to runner-up thumbnail concept within 48–72h; test title variant after (change one element at a time).
3. **CTR fine, 0:30 retention < 60%?** → **Hook problem.** The packaging promised something the first 30s didn't deliver, or delivered too slowly. Re-cut the open per Phase 7 spec (first visual change ≤ 4s, interrupt ≤ 15s, claim restated harder ≤ 5s). This is fixable on the *next* video only — note it, don't re-edit a live video's body.
4. **0:30 fine, mid-video slide steep (losing > 5%/min)?** → **Pacing problem.** Find each cliff's timestamp, match to the script: cliffs at abstraction blocks = missing concrete image; cliffs at story handoffs = loop closed too early. Cross-check Phase 8's predicted-drop table — were the interrupts actually in the cut?
5. **Retention healthy, views plateau day 3–5?** → **Suggested-feed fit.** Check traffic sources: if search-heavy, the video ranked but isn't being suggested — usually a topic too niche (fine for authority, don't expect virality). If browse died, the end screen/sequel loop isn't converting sessions — strengthen next-video linkage.
6. **Views fine, subs/1,000 < 3?** → **Channel-promise problem.** Video worked as content, not as an ad for the series. Sharpen the serial CTA ("the machine, piece by piece") and ensure banner/handle/description all state the same promise.

## Standard Improvement Levers (in order of leverage)
1. Thumbnail swap (highest ROI, zero production cost)
2. Title variant (one psychological trigger change, not a rewrite)
3. First-30s recut on next video (Phase 7 spec)
4. Description first-two-lines rewrite for search intent + add chapter markers at loop boundaries
5. End screen: single next-video element (multi-element end screens split clicks)
6. Pinned comment with sources (comment velocity is an algorithm input; receipts start arguments — politely)
7. Tags/SEO last — minor factor; only matters for search-dominant topics

## Post-Analysis Ritual (every video, every Friday)
1. Fill template → run decision tree → name the ONE binding constraint.
2. Log it in this file's changelog below (date, video, constraint, action taken).
3. Apply the single fix to the next video's production — never change three variables at once or attribution dies.
4. After 4 videos: compare constraint log — a repeating constraint is a *system* problem (revisit the relevant phase doc), a rotating one is normal iteration.

## Changelog
| Date | Video | Binding constraint | Action |
|---|---|---|---|
| — | — | — | — |
