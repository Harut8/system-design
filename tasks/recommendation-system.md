## System Design Task: Real-Time Recommendation System

### Problem Statement

Design a **large-scale, real-time recommendation system** — the kind that powers
the "For You" feed on TikTok, the YouTube home page, or the Netflix browse
experience — serving **personalized content recommendations** to hundreds of
millions of users with sub-200ms latency.

Today's recommendation systems are the most impactful ML systems in production.
They directly drive engagement, retention, and revenue. But they're also among
the hardest to build well: the item corpus changes continuously (new videos
uploaded every second), user preferences shift in real time (a user who just
watched three cooking videos wants more, not yesterday's interest in hiking),
and the system must balance exploration (surfacing novel content) with
exploitation (showing what the user is most likely to engage with) — all while
avoiding filter bubbles, promoting content quality, and respecting business
constraints (diversity, freshness, creator fairness).

This is not a batch-only collaborative filtering system. It is a **real-time,
multi-stage ranking pipeline** with online feature computation, where a user
action (watch, like, skip) changes what the next recommendation will be
**within seconds**, not hours.

The system serves a short-video platform (TikTok-like) with 500 million monthly
active users, 200 million pieces of content, and a 50:1 read-to-write ratio.
The core surface is a personalized "For You" feed that is the primary entry
point for 80%+ of user sessions.

---

### Functional Requirements

1. **Candidate Generation**

   * Given a user (with their profile, history, and real-time context), produce
     a set of **thousands of candidate items** from a corpus of 200M+ items in
     under 50ms.
   * Multiple retrieval channels, each producing candidates via a different
     signal:
     * **Collaborative filtering**: users who engaged with similar items.
     * **Content-based**: items similar to what this user recently engaged with
       (embedding similarity).
     * **Social graph**: items engaged by users this person follows or is
       similar to.
     * **Trending / popularity**: globally or regionally trending items.
     * **Cold-start**: new items with insufficient engagement data, surfaced
       via content features (topic, creator quality, production quality score).
     * **Exploration**: deliberately diverse/random candidates to escape filter
       bubbles and discover new interests.
   * Each channel returns a scored candidate list; candidates are merged with
     deduplication before passing to ranking.

2. **Feature Store and Feature Computation**

   * **User features**: demographics, historical engagement (last 7/30/90 days),
     real-time session features (last 5 items viewed, dwell times, scroll
     speed), device, time of day, location.
   * **Item features**: content embeddings (visual, audio, text), creator
     features, engagement statistics (total views, like rate, completion rate,
     share rate), freshness, content category, language, duration.
   * **Cross features**: user-item affinity (has user engaged with this creator
     before, user's affinity to this content category, embedding dot-product
     similarity).
   * **Real-time features** must be updated within **seconds** of a user action
     (a like, a skip, a 100% watch completion) and available to the ranker
     on the very next request — not batched hourly.
   * **Batch features** (aggregated statistics, embedding recomputation) updated
     on a daily or hourly schedule.
   * Feature serving latency: **P99 ≤ 10 ms** for a feature vector lookup.

3. **Multi-Stage Ranking**

   * **Stage 1 — Lightweight ranker (pre-ranking)**: a fast model (logistic
     regression or small neural net) that scores thousands of candidates on
     basic features, reducing to ~500 candidates. Latency budget: **≤ 20 ms**.
   * **Stage 2 — Heavy ranker (main ranking)**: a deep learning model (e.g.,
     Deep & Cross Network, multi-task learning model predicting multiple
     objectives) that scores ~500 candidates with rich features. Latency
     budget: **≤ 50 ms**.
   * **Stage 3 — Re-ranking / policy layer**: apply business rules and quality
     constraints:
     * **Diversity**: no more than 2 consecutive items from the same creator or
       category.
     * **Freshness**: boost items less than 24 hours old.
     * **Creator fairness**: ensure minimum exposure for eligible creators.
     * **Content quality**: suppress low-quality or borderline policy-violating
       content.
     * **Frequency capping**: don't show the same item or ad too often.
     * **Exploration injection**: insert exploration candidates at defined
       positions.
   * The final ranked list is paginated: the first page (10-20 items) is
     returned immediately; subsequent pages are pre-computed or computed on
     scroll.

4. **Multi-Objective Optimization**

   * The ranker must optimize for **multiple objectives simultaneously**:
     * **Watch time** (primary engagement metric).
     * **Like probability** (explicit positive signal).
     * **Share probability** (viral/growth signal).
     * **Follow probability** (creator ecosystem health).
     * **Negative signals**: skip rate, "not interested" rate, report rate.
   * These objectives are combined via a **weighted scoring formula** that is
     tunable without retraining the model:
     `final_score = w1 * P(watch_complete) + w2 * P(like) + w3 * P(share) - w4 * P(skip)`
   * The weights are a **product decision**, not an ML decision — the system
     must support rapid experimentation with different weight configurations
     via A/B testing.

5. **Real-Time Feedback Loop**

   * When a user watches, likes, skips, or hides an item, the system must
     incorporate that signal into subsequent recommendations **within the same
     session** (seconds, not hours).
   * This requires:
     * Updating the user's real-time feature vector in the feature store.
     * Optionally re-running candidate generation with updated context.
     * At minimum, re-ranking remaining candidates with the updated features.
   * **Session-level context**: the ranker has access to what has been shown and
     engaged with in the current session (in-session dedup, fatigue signals,
     interest drift detection).

6. **Cold Start**

   * **New users**: no engagement history — recommend based on demographics,
     device, location, time, and globally popular/trending items, then rapidly
     learn preferences from the first 10-20 interactions.
   * **New items**: no engagement statistics — score based on content features
     (visual/audio quality, creator track record, topic classification), then
     allocate a minimum exploration budget to gather engagement data.
   * **New creators**: bootstrap from content features and similar creators;
     provide a minimum exposure guarantee for the first N items published.

7. **Experimentation Platform (A/B Testing)**

   * Every change to the recommendation system — new model, new feature, weight
     change, business rule — must be testable via controlled experiment before
     full rollout.
   * Support for:
     * **User-level randomization** (consistent assignment: a user stays in
       the same group for the experiment duration).
     * **Multiple concurrent experiments** with traffic isolation.
     * **Metric computation**: engagement metrics (watch time, DAU, retention),
       content ecosystem metrics (creator diversity, new creator exposure),
     * **Guardrail metrics**: ensure experiments don't degrade safety, diversity,
       or long-term retention even if short-term engagement increases.
   * Experiment results available within **24-48 hours** for statistically
     significant decisions.

8. **Content Safety Integration**

   * The recommendation system must respect content moderation decisions:
     suppressed/removed content must never appear in recommendations.
   * Borderline content (not removed but flagged) should be de-ranked, not
     served to minors, and excluded from trending/exploration channels.
   * The system must be robust to **adversarial manipulation**: coordinated
     engagement fraud (bot farms boosting content), engagement bait, and
     exploitation of the exploration mechanism.

---

### Non-Functional Requirements

1. **Scale**

   * **500 million MAU**, 200 million DAU.
   * **200 million items** in the candidate corpus, growing by 5 million/day.
   * **Peak QPS**: 500,000 recommendation requests/sec (200M DAU × ~50
     sessions/day × ~5 feed loads/session, distributed over peak hours).
   * **Feature store**: 500M user feature vectors + 200M item feature vectors,
     each up to 2 KB.

2. **Latency**

   * **End-to-end recommendation** (from request to ranked list returned):
     P50 ≤ **100 ms**, P99 ≤ **200 ms**.
   * **Candidate generation**: ≤ 50 ms.
   * **Feature lookup**: ≤ 10 ms.
   * **Ranking (all stages)**: ≤ 80 ms.
   * **Real-time feature update propagation**: ≤ 5 seconds from user action
     to feature available for next request.

3. **Availability**

   * **99.99%** — the recommendation system IS the product. If it's down, the
     app shows a blank feed.
   * Graceful degradation: if the heavy ranker is down, fall back to the
     lightweight ranker; if candidate generation is impaired, fall back to
     popularity-based recommendations.

4. **Freshness**

   * A newly uploaded item should be eligible for recommendation within
     **10 minutes** (after content moderation passes).
   * A user's preference shift should be reflected in recommendations within
     the **same session** (seconds).
   * Item engagement statistics (view count, like rate) should reflect reality
     within **5 minutes** at peak, **1 minute** at normal load.

5. **Quality**

   * Online A/B metrics: **watch time per session**, **day-7 retention**,
     **creator diversity index** (Gini coefficient of views across creators).
   * Offline metrics: **Recall@K**, **NDCG@K** on held-out engagement data.
   * No single experiment should degrade day-7 retention by more than 0.1%
     (guardrail).

---

### Constraints and Assumptions

* The platform is a short-video app (videos 15s–3min). The primary engagement
  signal is watch time / completion rate, not clicks.
* Content moderation is a separate system; the recommendation system consumes
  its decisions (approved, flagged, removed) but does not perform moderation
  itself.
* The ML models (embeddings, rankers) are trained offline and deployed to
  serving; online learning / real-time model updates are out of scope for v1
  but the feature pipeline must support it.
* Assume a global deployment with data centers in NA, EU, and APAC, with user
  data sharded by region for latency and compliance (GDPR).
* Advertising is out of scope but the ranked feed must have defined insertion
  points for ad placements.

---

### What You Should Deliver

1. Requirement clarification and explicit assumptions.
2. High-level architecture: every major component and the request flow from
   user opening the app to seeing a personalized feed.
3. Candidate generation: each retrieval channel's design, data structures, and
   how they're merged.
4. Feature store design: online vs. offline features, storage, computation
   pipeline, and how real-time features propagate within seconds.
5. Multi-stage ranking pipeline: each stage's model architecture, input
   features, latency budget, and how they chain together.
6. Multi-objective scoring: how multiple objectives are combined, tuned, and
   A/B tested.
7. Real-time feedback loop: how a user action in this session changes the
   next recommendation within seconds.
8. Cold-start strategies: for new users, new items, and new creators.
9. Serving architecture: how the pipeline serves 500K QPS at P99 ≤ 200ms,
   including caching, batching, and graceful degradation.
10. Experimentation platform: A/B testing infrastructure, metric computation,
    and guardrails.
11. Capacity estimates with arithmetic: feature store size, embedding index
    size, model serving GPU fleet, bandwidth.
12. Failure walkthroughs: feature store down, ranker model returning garbage,
    a viral item creating a thundering herd, and a bot farm manipulating
    engagement signals.
13. Trade-offs: exploration vs. exploitation, engagement vs. diversity,
    real-time vs. batch features, model complexity vs. serving latency.

---

### Expectations

* **Do the arithmetic.** Feature vector sizes, embedding index memory, model
  inference FLOPs, QPS per GPU — these should be concrete numbers.
* **Name concrete mechanisms** — ANN retrieval via HNSW, Two-Tower model for
  candidate generation, Deep & Cross Network v2 for ranking, MMR for
  diversity, Thompson sampling for exploration — and say what each buys.
* **Show the latency budget.** 200ms is tight. Show exactly how it's split
  across candidate generation, feature fetch, ranking stages, and network
  hops.
* **The real-time feedback loop is the hard part.** Don't hand-wave "we update
  features" — show the data flow from user action to Kafka to feature store
  to next ranking call.
* Prefer a design that starts as a single-region prototype and scales to
  global multi-region, over one that requires Netflix-scale infra on day one.

---
