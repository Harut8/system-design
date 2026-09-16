/* lessons.js — the teaching engine behind the Learn tab.
 *
 * Deliberately much smaller than resilience-sim.js. That file simulates a whole
 * product: five call classes, three model tiers, three token meters. It is the
 * right tool for "what happens to my system", and the wrong tool for "what does
 * a retry actually do", because by the time you have 3,000 calls a second across
 * five classes you cannot see a single request any more.
 *
 * So this is one call class, one provider, one fault, and a **full trace of
 * every individual request** — every attempt, every backoff gap, every outcome.
 * The Learn tab draws that trace as one row per request, which is the only
 * visualisation that makes retry legible: you can literally see the second
 * attempt, and the gap before it.
 *
 * Same kernel (RSIM.Sim), same seeded determinism.
 */
(function (root) {
  "use strict";
  var R = root.RSIM;
  if (!R) throw new Error("lessons.js requires resilience-sim.js to load first");

  var Sim = R.Sim, Breaker = R.Breaker, RetryBudget = R.RetryBudget,
      TokenBucket = R.TokenBucket, Resource = R.Resource, backoff = R.backoff;

  /* ───────────────────────────────────────────── a provider you can break */

  function SimpleProvider(sim, cfg) {
    this.sim = sim;
    this.cfg = cfg;
    // `capacity` is requests/sec, with about two seconds of burst — the shape a
    // published RPM limit actually has. (Charging the wrong number of tokens
    // here turns "2 req/s" into "1 per 30s" and makes the whole lesson lie.)
    this.rl = { rate: cfg.fault.capacity, cap: cfg.fault.capacity * 2,
                tokens: cfg.fault.capacity * 2, t: 0 };
    this.count = { ok: 0, err: 0, r429: 0, timeout: 0 };
    this.inFlight = 0;
  }

  SimpleProvider.prototype.faulty = function () {
    var f = this.cfg.fault;
    return f.kind !== "none" && this.sim.now >= f.from && this.sim.now <= f.to;
  };

  SimpleProvider.prototype.call = function (req) {
    var self = this;
    return this.sim.process(function* () {
      var sim = self.sim, rng = sim.rng, f = self.cfg.fault, on = self.faulty();

      // rate limiting is checked before any work: a 429 costs one round trip
      if (f.kind === "ratelimit" && on) {
        var b = self.rl;
        b.tokens = Math.min(b.cap, b.tokens + (sim.now - b.t) * b.rate);
        b.t = sim.now;
        if (b.tokens < 1) {
          self.count.r429++;
          yield sim.timeout(0.03);
          // Seconds until the bucket holds a full burst again — which is what a
          // real provider's retry-after reflects. If this were merely "time for
          // one more token" it would always be shorter than your own backoff,
          // the header would never bind, and honouring it would change nothing.
          return { status: "429", retryAfter: Math.max(0.3, (b.cap - b.tokens) / b.rate) };
        }
        b.tokens -= 1;
      }

      self.inFlight++;
      var lat = self.cfg.latency * rng.lognorm(0, self.cfg.latencyJitter);
      if (f.kind === "slow" && on) lat *= f.factor;
      yield sim.timeout(lat);
      self.inFlight--;

      if (on && (f.kind === "errors" || f.kind === "outage")) {
        var p = f.kind === "outage" ? 1 : f.rate;
        if (rng.random() < p) { self.count.err++; return { status: "500", duration: lat }; }
      }
      self.count.ok++;
      return { status: "ok", duration: lat };
    }());
  };

  /* ────────────────────────────────────────────────────── one request ─── */

  function* oneRequest(st, rec) {
    var sim = st.sim, c = st.cfg;

    // ── client-side rate limiter (self-metering) ───────────────────────
    if (c.limiter.on) {
      var w = st.clientBucket.wait(sim.now, 1);
      if (w > 0) {
        if (w > c.limiter.maxWait) {
          rec.outcome = "blocked"; rec.blockedBy = "limiter";
          rec.end = sim.now; return;
        }
        rec.waits.push({ s: sim.now, e: sim.now + w, why: "waiting for local quota" });
        yield sim.timeout(w);
      }
      st.clientBucket.take(sim.now, 1);
    }

    // ── admission control (priority shedding) ──────────────────────────
    if (c.admission.on && rec.priority > 0) {
      var busy = st.slots ? st.slots.inUse / Math.max(1, st.slots.cap) : 0;
      if (busy >= c.admission.shedAbove) {
        rec.outcome = "shed"; rec.blockedBy = "admission";
        rec.end = sim.now; return;
      }
    }

    // ── concurrency limit ──────────────────────────────────────────────
    var slotEv = null;
    if (c.concurrency.on) {
      slotEv = st.slots.request();
      var t0 = sim.now;
      var race = yield sim.anyOf([slotEv, sim.timeout(c.queueWait)]);
      if (race !== slotEv) {
        st.slots.cancel(slotEv);
        rec.outcome = "blocked"; rec.blockedBy = "queue";
        rec.end = sim.now; return;
      }
      if (sim.now > t0) rec.waits.push({ s: t0, e: sim.now, why: "queued for a slot" });
    }

    try {
      var attempts = c.retry.on ? c.retry.attempts : 1;
      for (var a = 1; a <= attempts; a++) {
        // ── circuit breaker ──────────────────────────────────────────
        if (c.breaker.on && !st.breaker.allow()) {
          rec.outcome = "blocked"; rec.blockedBy = "breaker";
          rec.end = sim.now; return;
        }

        var start = sim.now;
        var call = st.provider.call({});
        var done = yield sim.anyOf([call, sim.timeout(c.timeout)]);
        var status, resp = null;
        if (done === call) { resp = call.value; status = resp.status; }
        else { status = "timeout"; st.provider.count.timeout++; }

        rec.attempts.push({ s: start, e: sim.now, status: status });
        st.attempts++;
        if (a > 1) st.retries++;
        st.bin(start).attempts++;
        st.bin(start)[status === "ok" ? "ok" : status === "429" ? "r429" : "fail"]++;

        if (c.breaker.on) st.breaker.record(status);

        if (status === "ok") {
          if (c.retry.on && c.retry.budget > 0) st.budget.ok();
          rec.outcome = "ok"; rec.end = sim.now; return;
        }

        if (a >= attempts) break;
        if (status === "400") break;

        // ── backoff ───────────────────────────────────────────────────
        if (c.retry.budget > 0 && !st.budget.take()) {
          rec.stoppedRetrying = "budget exhausted";
          break;
        }
        var ra = c.retry.retryAfter && resp && resp.retryAfter ? resp.retryAfter : 0;
        var d = backoff(sim.rng, a, c.retry.base, c.retry.cap, ra, c.retry.jitter);
        rec.waits.push({ s: sim.now, e: sim.now + d, why: "backoff" });
        yield sim.timeout(d);
      }
      rec.outcome = "failed"; rec.end = sim.now;
    } finally {
      if (slotEv) st.slots.release();
    }
  }

  /* ─────────────────────────────────────────────────────────── runner ─── */

  var DEFAULTS = {
      rps: 2, duration: 30, seed: 5, clients: 1, arrivals: "even",
      latency: 0.4, latencyJitter: 0.12, timeout: 3, queueWait: 5,
      priorityMix: false,
      fault: { kind: "none", from: 10, to: 20, rate: 0.1, capacity: 2, factor: 6 },
      retry: { on: false, attempts: 3, base: 0.5, cap: 8, jitter: "none", retryAfter: false, budget: 0 },
      breaker: { on: false, threshold: 0.5, minCalls: 5, window: 20, openFor: 5, probes: 2,
                 counts429: false, expBackoff: false },
      limiter: { on: false, rate: 2, maxWait: 2 },
      concurrency: { on: false, limit: 4 },
      admission: { on: false, shedAbove: 0.8 },
      maxTrace: 80,
  };

  /* A lesson's `config` is a sparse override. Anything that reads a config —
   * the UI's knobs, the setup sentence, the arithmetic — needs the FULL object,
   * or a knob pointing at a path the lesson didn't set blows up. */
  function fullConfig(cfg) { return deep(DEFAULTS, cfg || {}); }

  function runLesson(cfg) {
    var c = fullConfig(cfg);

    var sim = new Sim(c.seed);
    var st = {
      sim: sim, cfg: c,
      provider: new SimpleProvider(sim, c),
      breaker: new Breaker(sim, c.breaker),
      budget: new RetryBudget(c.retry.budget || 0.1),
      clientBucket: new TokenBucket(c.limiter.rate * 60),
      slots: c.concurrency.on ? new Resource(sim, c.concurrency.limit) : null,
      attempts: 0, retries: 0,
      trace: [],
      bins: [],
      bin: function (t) {
        var i = Math.max(0, Math.min(this.bins.length - 1, Math.floor(t / BIN)));
        return this.bins[i];
      },
    };
    var BIN = 0.25;
    for (var i = 0; i < Math.ceil(c.duration / BIN) + 8; i++) {
      st.bins.push({ t: i * BIN, attempts: 0, ok: 0, fail: 0, r429: 0 });
    }

    var id = 0;
    function spawn() {
      for (var k = 0; k < c.clients; k++) {
        var rec = {
          id: id++, start: sim.now, end: null, attempts: [], waits: [],
          outcome: null, blockedBy: null, stoppedRetrying: null,
          priority: c.priorityMix ? (sim.rng.random() < 0.5 ? 1 : 0) : 0,
        };
        st.trace.push(rec);
        sim.process(oneRequest(st, rec));
      }
    }

    sim.process(function* () {
      if (c.arrivals === "poisson") {
        // Real traffic: exponential gaps, so the count varies run to run.
        while (sim.now < c.duration) {
          spawn();
          yield sim.timeout(sim.rng.expo(Math.max(0.05, c.rps)));
        }
      } else {
        // Evenly spaced, counted rather than clocked. Comparing an accumulating
        // float clock against `duration` yields 151 arrivals where 150 were
        // promised (0.2 added 150 times lands just under 30), and then the
        // arithmetic panel is off by one for no visible reason.
        var total = Math.max(1, Math.round(c.rps * c.duration));
        var gap = c.duration / total;
        for (var i = 0; i < total; i++) {
          spawn();
          yield sim.timeout(gap);
        }
      }
    }());

    sim.run(c.duration + 120);

    return summarise(st, c, BIN);
  }

  function summarise(st, c, BIN) {
    var t = st.trace;
    var ok = 0, failed = 0, blocked = 0, shed = 0, lat = [], latAll = [];
    var byPriority = [{ ok: 0, n: 0 }, { ok: 0, n: 0 }];
    t.forEach(function (r) {
      if (r.end === null) r.end = st.sim.now;
      byPriority[r.priority].n++;
      latAll.push(r.end - r.start);
      if (r.outcome === "ok") { ok++; byPriority[r.priority].ok++; lat.push(r.end - r.start); }
      else if (r.outcome === "failed") failed++;
      else if (r.outcome === "shed") shed++;
      else blocked++;
    });
    var breakerOpen = [];
    var openAt = null;
    st.breaker.transitions.forEach(function (x) {
      if (x[1] === "open" && openAt === null) openAt = x[0];
      else if (x[1] !== "open" && openAt !== null) { breakerOpen.push([openAt, x[0]]); openAt = null; }
    });
    if (openAt !== null) breakerOpen.push([openAt, c.duration]);

    return {
      cfg: c,
      trace: t,
      bins: st.bins,
      binWidth: BIN,
      breakerOpen: breakerOpen,
      breakerTrips: st.breaker.trips,
      provider: st.provider.count,
      summary: {
        requests: t.length, ok: ok, failed: failed, blocked: blocked, shed: shed,
        successRate: t.length ? ok / t.length : 0,
        attempts: st.attempts, retries: st.retries,
        amplification: t.length ? st.attempts / t.length : 0,
        providerCalls: st.provider.count.ok + st.provider.count.err +
                       st.provider.count.r429 + st.provider.count.timeout,
        p50: R.pct(lat, 0.5), p95: R.pct(lat, 0.95), max: lat.length ? Math.max.apply(null, lat) : 0,
        p95All: R.pct(latAll, 0.95),
        timeWasted: r_timeWasted(t),
        byPriority: byPriority,
      },
    };
  }

  /* Seconds of wall-clock burned by requests that produced nothing. This is
   * what a circuit breaker actually saves, and it is invisible if you only
   * measure the latency of successful calls. */
  function r_timeWasted(trace) {
    var total = 0;
    trace.forEach(function (r) {
      if (r.outcome !== "ok") total += (r.end - r.start);
    });
    return total;
  }

  function deep(base, over) {
    var out = {};
    for (var k in base) {
      if (base[k] && typeof base[k] === "object" && !Array.isArray(base[k])) {
        out[k] = deep(base[k], (over && over[k]) || {});
      } else {
        out[k] = over && over[k] !== undefined ? over[k] : base[k];
      }
    }
    for (var j in over) if (out[j] === undefined) out[j] = over[j];
    return out;
  }

  /* ═══════════════════════════════════════════════════ the lesson ladder ═══
   *
   * Ten steps, each one experiment. `config` is the setup, `knobs` are the two
   * or three things worth poking, `notice` computes a sentence from the actual
   * result, and `takeaway` is the thing to remember.
   */

  function pc(x) { return Math.round(x * 100) + "%"; }
  function s1(x) { return x.toFixed(1); }

  /* ── show your working ────────────────────────────────────────────────
   *
   * Every lesson can hand the UI a derivation, so the numbers on screen are
   * checkable rather than asserted. The retry arithmetic is a geometric series:
   * with failure probability p and up to n attempts, each request costs
   *
   *     attempts = 1 + p + p² + … + p^(n-1) = (1 - pⁿ) / (1 - p)
   *
   * and ends up failing with probability pⁿ. At p=0.1, n=3 that is 1.11
   * attempts per request and a 0.1% residual failure rate — so 100 requests
   * cost 111 attempts. The two things that make the observed number differ:
   * arrivals are Poisson (so N is not exactly rate×duration), and the fault
   * only covers part of the run (so only some requests roll the dice).
   */
  function retryMath(r) {
    var c = r.cfg, f = c.fault;
    var n = c.retry.on ? c.retry.attempts : 1;
    var p = f.kind === "outage" ? 1 : (f.kind === "errors" ? f.rate : 0);
    var N = r.trace.length;
    var inWin = 0;
    r.trace.forEach(function (x) { if (x.start >= f.from && x.start <= f.to) inWin++; });
    var outWin = N - inWin;
    var perReq = p >= 1 ? n : (1 - Math.pow(p, n)) / (1 - p);
    var expAttempts = outWin + inWin * perReq;
    var expFail = inWin * Math.pow(p, n);

    var rows = [
      ["requests generated", N,
       c.rps + " req/s × " + c.duration + "s = " + (c.rps * c.duration).toFixed(0) +
       (c.arrivals === "poisson"
         ? " on average — arrivals are Poisson, so the count varies run to run"
         : " exactly — arrivals are evenly spaced" +
           (c.clients > 1 ? ", × " + c.clients + " clients" : ""))],
      ["…of which hit the fault window", inWin,
       "the provider is only sick from " + f.from + "s to " + f.to + "s"],
    ];
    // Failures are a coin flip per exposed request, so the count is Binomial —
    // it has a SPREAD, not just a mean. Reporting the mean alone makes a
    // perfectly ordinary run look like a broken simulator: at 1 req/s only 16
    // requests are exposed, and 0.9^16 = 18% of runs produce zero failures.
    var pFinal = n > 1 ? Math.pow(p, n) : p;
    var mean = inWin * pFinal;
    var sd = Math.sqrt(inWin * pFinal * (1 - pFinal));

    if (p > 0 && n > 1) {
      rows.push(["attempts per faulted request", perReq.toFixed(3),
        "(1 − p" + sup(n) + ") / (1 − p)  with p=" + p + ", n=" + n +
        "  =  1 + " + p + " + " + (p * p).toFixed(3) + (n > 3 ? " + …" : "")]);
      rows.push(["expected attempts", expAttempts.toFixed(1),
        outWin + " clean × 1  +  " + inWin + " faulted × " + perReq.toFixed(3)]);
      rows.push(["actual attempts", r.summary.attempts,
        diffNote(r.summary.attempts, expAttempts)]);
      rows.push(["expected still failing", expFail.toFixed(2) + " ± " + sd.toFixed(2),
        inWin + " × p" + sup(n) + " = " + inWin + " × " + Math.pow(p, n).toFixed(4) +
        "  (± 1 standard deviation)"]);
      rows.push(["actual failures", r.summary.failed, spread(r.summary.failed, mean, sd, inWin, pFinal)]);
    } else if (p > 0) {
      rows.push(["expected failures", mean.toFixed(2) + " ± " + sd.toFixed(2),
        inWin + " × " + p + " — with no retry the error rate passes straight through" +
        "  (± 1 standard deviation)"]);
      rows.push(["actual failures", r.summary.failed, spread(r.summary.failed, mean, sd, inWin, pFinal)]);
      rows.push(["attempts", r.summary.attempts, "one per request; nothing is retried"]);
    } else {
      rows.push(["attempts", r.summary.attempts,
        n > 1 ? "no failures to retry" : "one per request"]);
    }
    return rows;
  }

  function sup(n) { return ["⁰", "¹", "²", "³", "⁴", "⁵"][n] || ("^" + n); }

  /* ── the setup sentence, generated from the LIVE config ──────────────────
   *
   * This used to be a hardcoded string per lesson, which meant it kept saying
   * "2 requests/sec" after you had dragged the slider to 10 — the description
   * and the experiment disagreed, and the description was the one people read.
   * Deriving it from the config makes that impossible.
   */
  function describe(c) {
    var parts = [];
    var load = c.rps + " req/s";
    if (c.clients > 1) load += " × " + c.clients + " clients";
    load += " for " + c.duration + "s";
    load += c.arrivals === "poisson" ? " (poisson arrivals)" : " (evenly spaced)";
    parts.push(load);

    var f = c.fault;
    if (f.kind === "none") parts.push("provider healthy, ~" + c.latency + "s per call");
    else if (f.kind === "errors")
      parts.push("provider fails " + Math.round(f.rate * 100) + "% of calls from " +
                 f.from + "s to " + f.to + "s");
    else if (f.kind === "outage")
      parts.push("provider completely down from " + f.from + "s to " + f.to + "s");
    else if (f.kind === "ratelimit")
      parts.push("provider allows " + f.capacity + " req/s, returns 429 above that");
    else if (f.kind === "slow")
      parts.push("provider stalls (×" + f.factor + " latency) from " + f.from + "s to " + f.to + "s");

    if (c.retry.on) {
      var rt = "retry up to " + c.retry.attempts + " attempt" + (c.retry.attempts > 1 ? "s" : "") +
        ", " + c.retry.base + "s base, " +
        (c.retry.jitter === "none" ? "no jitter" : c.retry.jitter + " jitter");
      if (c.retry.budget > 0) rt += ", budget " + Math.round(c.retry.budget * 100) + "% of successes";
      if (c.retry.retryAfter) rt += ", honouring retry-after";
      parts.push(rt);
    } else {
      parts.push("no retry");
    }

    if (c.breaker.on) {
      parts.push("breaker: trip at " + Math.round(c.breaker.threshold * 100) + "% failures, " +
        "open " + c.breaker.openFor + "s" +
        (c.breaker.counts429 ? ", COUNTING 429s as failures" : ""));
    }
    if (c.concurrency.on) parts.push("only " + c.concurrency.limit + " calls can run at once");
    if (c.admission.on) parts.push("shedding background work when busy");
    if (c.limiter.on) parts.push("client-side limit " + c.limiter.rate + " req/s");
    parts.push(c.timeout + "s timeout");
    return parts.join("  ·  ");
  }

  /* Is this run consistent with the model, or is something actually wrong?
   * Answering that needs the spread, and — when the expected count is tiny —
   * the probability of seeing exactly zero, which is the case people report as
   * a bug. */
  function spread(actual, mean, sd, n, p) {
    var pZero = Math.pow(1 - p, n);
    var z = sd > 0 ? (actual - mean) / sd : 0;
    var verdict;
    if (Math.abs(z) <= 2) verdict = "consistent with the model (within 2 s.d.)";
    else verdict = "more than 2 s.d. out — worth a second look";

    if (actual === 0 && pZero > 0.02) {
      verdict = "zero is normal here: P(no failures at all) = " +
        Math.round(pZero * 100) + "%, because only " + n + " requests were exposed";
    }
    if (mean < 3 && actual !== 0) {
      verdict += ", but with only " + mean.toFixed(1) + " expected there is not enough " +
        "signal here to measure a rate — raise the request rate or widen the fault window";
    }
    return verdict;
  }

  function diffNote(actual, expected) {
    if (expected <= 0) return actual === 0 ? "matches" : "";
    var d = (actual - expected) / expected;
    if (Math.abs(actual - expected) < 1.5) return "matches the prediction";
    return (d > 0 ? "+" : "") + Math.round(d * 100) + "% vs predicted — " +
      (Math.abs(d) < 0.3 ? "ordinary sampling noise at this sample size"
                         : "check the knobs above; something else is in play");
  }

  var LESSONS = [
    {
      id: "baseline",
      math: retryMath,
      title: "One healthy call",
      question: "What does a request to the model API actually look like?",
      viz: "timeline",
      config: { rps: 2, duration: 20, fault: { kind: "none" } },
      knobs: [
        { path: "rps", label: "requests per second", min: 1, max: 10, step: 1 },
        { path: "latency", label: "provider latency (s)", min: 0.1, max: 2, step: 0.1 },
      ],
      notice: function (r) {
        return "Every one of the " + r.summary.requests + " requests is a single green bar: one attempt, " +
          "about " + s1(r.summary.p50) + "s, then done. " +
          r.summary.attempts + " attempts for " + r.summary.requests + " requests — an amplification of " +
          r.summary.amplification.toFixed(2) + "×.";
      },
      takeaway: "This is the baseline. Each row below is one request; time runs left to right. " +
        "Everything in the next nine steps is a deviation from this picture.",
      next: "Now let's break the provider.",
    },

    {
      id: "errors",
      math: retryMath,
      title: "The provider starts failing",
      question: "10% of calls return a 500 for 15 seconds. With no retry, what does the user see?",
      viz: "timeline",
      config: { rps: 2, duration: 30, fault: { kind: "errors", from: 8, to: 23, rate: 0.1 } },
      knobs: [
        { path: "fault.rate", label: "provider error rate", min: 0, max: 1, step: 0.05, pct: true },
        { path: "rps", label: "requests per second", min: 1, max: 10, step: 1 },
        { path: "arrivals", label: "arrivals", options: ["even", "poisson"] },
      ],
      notice: function (r) {
        var f = r.cfg.fault;
        var inWin = r.trace.filter(function (x) { return x.start >= f.from && x.start <= f.to; });
        var failedIn = inWin.filter(function (x) { return x.outcome === "failed"; }).length;
        return r.summary.failed + " of " + r.summary.requests + " requests failed overall — but the " +
          "provider was only sick between " + f.from + "s and " + f.to + "s. Inside that window: " +
          failedIn + " of " + inWin.length + " failed (" + pc(inWin.length ? failedIn / inWin.length : 0) +
          "), which is the " + pc(f.rate) + " error rate passing straight through, one for one.";
      },
      takeaway: "No retry means the failure rate you see is exactly the failure rate they have. " +
        "Every red bar is a user-visible error.",
      next: "The obvious fix is to try again. Let's see what that actually costs.",
    },

    {
      id: "retry",
      math: retryMath,
      title: "Add a retry",
      question: "Same fault, but now try up to 3 times. How much does it help, and what does it cost?",
      viz: "timeline",
      config: { rps: 2, duration: 30, fault: { kind: "errors", from: 8, to: 23, rate: 0.3 },
                retry: { on: true, attempts: 3, base: 0.5, jitter: "none" } },
      knobs: [
        { path: "retry.attempts", label: "max attempts", min: 1, max: 5, step: 1 },
        { path: "fault.rate", label: "provider error rate", min: 0, max: 1, step: 0.05, pct: true },
        { path: "retry.base", label: "wait between tries (s)", min: 0.1, max: 3, step: 0.1 },
        { path: "arrivals", label: "arrivals", options: ["even", "poisson"] },
      ],
      notice: function (r) {
        var rescued = r.trace.filter(function (x) {
          return x.outcome === "ok" && x.attempts.length > 1;
        }).length;
        return rescued + " requests failed on their first try and were rescued by a retry. " +
          "Only " + r.summary.failed + " of " + r.summary.requests + " ended up failing (" +
          pc(r.summary.successRate) + " succeeded) — from a provider dropping " +
          pc(r.cfg.fault.rate) + " of calls. You sent " + r.summary.attempts + " attempts to do it, " +
          r.summary.amplification.toFixed(2) + "× the traffic. Find a row with a gap in it: the gap " +
          "is the wait, and the bar after it is the retry.";
      },
      takeaway: "A retry converts a failure into extra latency and extra load. That is a good trade " +
        "here, because the failures are independent — a second attempt genuinely has a fresh 90% " +
        "chance. Keep that word: independent.",
      next: "Now make the failures correlated instead.",
    },

    {
      id: "outage",
      math: retryMath,
      title: "When retrying cannot help",
      question: "The provider is completely down for 15 seconds. What do 3 attempts buy you?",
      viz: "timeline",
      config: { rps: 2, duration: 32, fault: { kind: "outage", from: 8, to: 23 },
                retry: { on: true, attempts: 3, base: 0.5, jitter: "none" } },
      knobs: [
        { path: "retry.attempts", label: "max attempts", min: 1, max: 5, step: 1 },
        { path: "retry.base", label: "wait between tries (s)", min: 0.1, max: 4, step: 0.1 },
        { path: "arrivals", label: "arrivals", options: ["even", "poisson"] },
      ],
      notice: function (r) {
        return "Still " + r.summary.failed + " failures — the retries rescued almost nothing — " +
          "but you sent " + r.summary.attempts + " attempts (" + r.summary.amplification.toFixed(2) +
          "×) to find that out. On an LLM API every one of those re-processed the whole prompt, and " +
          "you paid for it.";
      },
      takeaway: "Retries help when failures are INDEPENDENT and are pure cost when they are " +
        "CORRELATED. A single bad node is independent. A down fleet, a bad deploy, and an " +
        "overloaded provider are correlated — and that is exactly when your retries arrive.",
      next: "There is a failure that specifically means 'stop sending me traffic'.",
    },

    {
      id: "ratelimit",
      title: "429: the provider says slow down",
      question: "You send 5 req/sec at an API that allows 2. What does retrying do?",
      viz: "timeline",
      config: { rps: 5, duration: 30, latency: 0.3,
                fault: { kind: "ratelimit", from: 0, to: 999, capacity: 2 },
                retry: { on: true, attempts: 3, base: 0.5, jitter: "none", retryAfter: false } },
      knobs: [
        { path: "rps", label: "your request rate", min: 1, max: 12, step: 1 },
        { path: "fault.capacity", label: "provider allows (req/s)", min: 1, max: 10, step: 1 },
        { path: "retry.retryAfter", label: "honour retry-after", bool: true },
      ],
      notice: function (r) {
        var honoured = r.cfg.retry.retryAfter;
        return r.provider.r429 + " calls were rate limited. " +
          (honoured
            ? "You are honouring retry-after, so retries wait until the quota actually refills."
            : "You are ignoring retry-after and backing off on your own schedule — so retries arrive " +
              "before the bucket has refilled and get 429'd again.") +
          " Success rate " + pc(r.summary.successRate) + " on " + r.summary.attempts + " attempts.";
      },
      takeaway: "A 429 is not a failure. It is the provider working correctly and telling you exactly " +
        "how long to wait. Toggle 'honour retry-after' and watch the amber bars. The real fix is " +
        "lower down: do not send more than your quota in the first place.",
      next: "So far one client. Now imagine your whole fleet retrying at once.",
    },

    {
      id: "herd",
      title: "Everyone retries at the same moment",
      question: "30 clients all fail at once, then all retry. Where do the retries land?",
      viz: "histogram",
      config: { rps: 1, clients: 30, duration: 20, latency: 0.2,
                fault: { kind: "outage", from: 5, to: 7 },
                retry: { on: true, attempts: 3, base: 2, cap: 4, jitter: "none" } },
      knobs: [
        { path: "retry.jitter", label: "jitter", options: ["none", "equal", "full"] },
        { path: "clients", label: "clients retrying together", min: 5, max: 60, step: 5 },
      ],
      notice: function (r) {
        var peak = 0, peakT = 0;
        r.bins.forEach(function (b) { if (b.attempts > peak) { peak = b.attempts; peakT = b.t; } });
        return "Busiest quarter-second: " + peak + " attempts, at t=" + s1(peakT) + "s. " +
          (r.cfg.retry.jitter === "none"
            ? "With no jitter every client that failed together retries together — that spike is a " +
              "self-inflicted load test aimed at a provider that is already unwell."
            : "Jitter spreads the retries across the whole backoff window, so the provider sees a " +
              "smooth trickle instead of a wall.");
      },
      takeaway: "sleep(base * 2^n) synchronises every client that failed in the same second into the " +
        "same future second. Full jitter — random(0, window) — is one line and removes the " +
        "correlation entirely. This is the cheapest fix in the whole lab.",
      next: "Jitter spreads retries out. It does not reduce how many there are.",
    },

    {
      id: "budget",
      math: retryMath,
      title: "A retry budget",
      question: "During a long outage, how do you stop retrying without turning retries off?",
      viz: "chart",
      config: { rps: 3, duration: 36, fault: { kind: "outage", from: 8, to: 28 },
                retry: { on: true, attempts: 3, base: 1, jitter: "full", budget: 0.1 } },
      knobs: [
        { path: "retry.budget", label: "retry budget (× successes)", min: 0, max: 1, step: 0.05 },
        { path: "retry.attempts", label: "max attempts", min: 1, max: 5, step: 1 },
      ],
      notice: function (r) {
        var stopped = r.trace.filter(function (x) { return x.stoppedRetrying; }).length;
        return r.summary.attempts + " attempts for " + r.summary.requests + " requests (" +
          r.summary.amplification.toFixed(2) + "×). " +
          (r.cfg.retry.budget > 0
            ? stopped + " requests gave up early because the budget was empty — during the outage " +
              "there were no successes to refill it."
            : "With the budget at zero there is no brake: every request spends its full attempt " +
              "allowance no matter how bad things are.");
      },
      takeaway: "A per-call-site attempt count multiplies load exactly when the dependency can least " +
        "take it. A budget expressed as a fraction of SUCCESSES self-cancels during a real outage, " +
        "because there are no successes to fund it. Retries stay available for the blip they are for.",
      next: "Better still: notice the dependency is broken and stop calling it.",
    },

    {
      id: "breaker",
      title: "Fail fast: the circuit breaker",
      question: "The provider hangs instead of erroring. Every call burns the full 3s timeout. Now what?",
      viz: "timeline",
      config: { rps: 4, duration: 40, timeout: 3, latency: 0.4,
                fault: { kind: "slow", from: 10, to: 30, factor: 20 },
                breaker: { on: true, threshold: 0.5, minCalls: 5, window: 10, openFor: 3, probes: 2 } },
      knobs: [
        { path: "breaker.on", label: "circuit breaker", bool: true },
        { path: "breaker.openFor", label: "stay open for (s)", min: 1, max: 20, step: 1 },
        { path: "breaker.threshold", label: "trip at failure ratio", min: 0.1, max: 0.9, step: 0.1, pct: true },
      ],
      notice: function (r) {
        var blocked = r.summary.blocked;
        return (r.cfg.breaker.on
          ? "The breaker tripped " + r.breakerTrips + " time(s) and refused " + blocked +
            " requests instantly (grey) instead of parking each one on the " + r.cfg.timeout +
            "s timeout. "
          : "No breaker: every request during the stall waits the full " + r.cfg.timeout +
            "s timeout before giving up, holding a connection the whole time. ") +
          "Wall-clock burned by requests that produced nothing: " + s1(r.summary.timeWasted) + "s. " +
          "Success rate " + pc(r.summary.successRate) + ". " +
          "Toggle the breaker and watch BOTH numbers move — they move in opposite directions.";
      },
      takeaway: "Without a breaker every caller rediscovers the outage independently, one timeout each, " +
        "holding a connection the whole time. With one, the first few discover it and everyone else " +
        "fails instantly — and the recovering provider gets probed by one cheap request at a time.\n\n" +
        "But notice the honest cost: an open circuit also refuses requests that WOULD have " +
        "succeeded. A breaker trades a little availability for a lot of wasted time and a much " +
        "gentler recovery. If your critical path cannot afford that trade, the answer is to give it " +
        "a fallback — not to remove the breaker.",
      next: "One more thing the breaker must NOT do.",
    },

    {
      id: "flag",
      title: "429 must not trip the breaker",
      question: "The provider is healthy but rate-limiting 30% of your calls. Should the circuit open?",
      viz: "timeline",
      config: { rps: 6, duration: 30, latency: 0.25,
                fault: { kind: "ratelimit", from: 0, to: 999, capacity: 4 },
                retry: { on: true, attempts: 2, base: 0.4, jitter: "full", retryAfter: true },
                breaker: { on: true, threshold: 0.3, minCalls: 10, window: 20, openFor: 8,
                           probes: 2, counts429: true } },
      knobs: [
        { path: "breaker.counts429", label: "count 429 as a failure", bool: true },
      ],
      notice: function (r) {
        var blocked = r.trace.filter(function (x) { return x.blockedBy === "breaker"; }).length;
        return r.cfg.breaker.counts429
          ? "The breaker tripped " + r.breakerTrips + " time(s) and refused " + blocked +
            " requests — against an API that was answering every call it had quota for. " +
            "You have layered a self-inflicted outage on top of a rate limit."
          : "The breaker stayed closed and refused " + blocked + " requests. The 429s are handled by " +
            "backing off, which is what they are asking for. Success rate " + pc(r.summary.successRate) + ".";
      },
      takeaway: "This is one line of code: `if (status === 429) return;` before the breaker records " +
        "anything. A breaker exists to detect a BROKEN dependency. One that is rate-limiting you is " +
        "working perfectly. Tick and untick the box and watch the grey bars appear and vanish.",
      next: "Last one: what happens when the problem is you, not them.",
    },

    {
      id: "overload",
      title: "Too much load, and shedding on purpose",
      question: "Demand is 3× what you can run concurrently. Do you queue everyone, or refuse some?",
      viz: "split",
      config: { rps: 12, duration: 30, latency: 0.6, timeout: 4, queueWait: 3,
                priorityMix: true,
                concurrency: { on: true, limit: 4 },
                admission: { on: false, shedAbove: 0.8 } },
      knobs: [
        { path: "admission.on", label: "shed background work first", bool: true },
        { path: "concurrency.limit", label: "how many can run at once", min: 1, max: 16, step: 1 },
        { path: "rps", label: "requests per second", min: 2, max: 30, step: 2 },
      ],
      notice: function (r) {
        var u = r.summary.byPriority[0], b = r.summary.byPriority[1];
        return "Important work: " + u.ok + "/" + u.n + " served (" + pc(u.n ? u.ok / u.n : 0) + "). " +
          "Background work: " + b.ok + "/" + b.n + " (" + pc(b.n ? b.ok / b.n : 0) + "). " +
          (r.cfg.admission.on
            ? "Shedding is on, so background requests are refused immediately and the slots go to the " +
              "work someone is waiting for."
            : "Shedding is off, so both classes compete equally for the same 4 slots — and the " +
              "important work is queued behind background work that nobody is waiting for.");
      },
      takeaway: "You cannot serve more than your capacity. The only decision left is WHO gets served " +
        "and how fast everyone else is refused. Shedding does not create capacity — it reallocates it " +
        "from work nobody is waiting on to work someone is. That choice is a product judgement, " +
        "which is why no library will make it for you.",
      next: "You now have every pattern. The Sandbox tab lets you compose and reorder them against " +
        "the full five-call-class workload.",
    },
  ];

  root.RLESSONS = { runLesson: runLesson, LESSONS: LESSONS, deep: deep, describe: describe, fullConfig: fullConfig, DEFAULTS: DEFAULTS };
})(typeof globalThis !== "undefined" ? globalThis : this);

if (typeof module !== "undefined" && module.exports) module.exports = globalThis.RLESSONS;
