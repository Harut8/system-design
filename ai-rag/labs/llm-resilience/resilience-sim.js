/* resilience-sim.js — the simulation engine behind simulator.html.
 *
 * A browser port of the Python lab in this folder (sim.py / provider.py /
 * gateway.py). Same model, same fixture, same conclusions — but arranged so the
 * defence stack is a *list you can reorder* instead of a fixed function.
 *
 * Three ideas make the UI possible:
 *
 *   1. Deterministic discrete-event kernel. Seeded RNG, an event heap, and
 *      generator-based coroutines. Same seed => byte-identical run, so when you
 *      change one knob the difference you see is caused by that knob.
 *
 *   2. Patterns as *wrappers*, not steps. Each gate is
 *      `function*(ctx, req, next)` where `next()` returns a generator for
 *      everything inside it. So the chain is a set of nested layers and the
 *      innermost thing is the provider call. Reordering the list genuinely
 *      changes the semantics — a retry placed outside the quota gate re-reserves
 *      tokens on every attempt; placed inside, it reuses one reservation.
 *
 *   3. Every rejection is attributed. `result.stoppedAt` names the gate that
 *      said no, which is what the funnel chart draws.
 *
 * Plain script, no modules, no build step: attaches `RSIM` to globalThis so it
 * works from file:// and from node.
 */
(function (root) {
  "use strict";

  /* ═══════════════════════════════════════════════════════ RNG + heap ═══ */

  function mulberry32(a) {
    return function () {
      a |= 0; a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  class RNG {
    constructor(seed) { this.next = mulberry32(seed >>> 0); this._spare = null; }
    random() { return this.next(); }
    expo(rate) { return -Math.log(1 - this.next()) / rate; }
    gauss(mu, sd) {
      if (this._spare !== null) { const s = this._spare; this._spare = null; return mu + sd * s; }
      let u = 0, v = 0, s = 0;
      do { u = this.next() * 2 - 1; v = this.next() * 2 - 1; s = u * u + v * v; }
      while (s >= 1 || s === 0);
      const f = Math.sqrt((-2 * Math.log(s)) / s);
      this._spare = v * f;
      return mu + sd * u * f;
    }
    lognorm(mu, sd) { return Math.exp(this.gauss(mu, sd)); }
  }

  class Heap {
    constructor() { this.a = []; }
    get size() { return this.a.length; }
    peek() { return this.a[0]; }
    push(x) {
      const a = this.a; a.push(x); let i = a.length - 1;
      while (i > 0) {
        const p = (i - 1) >> 1;
        if (a[i][0] < a[p][0] || (a[i][0] === a[p][0] && a[i][1] < a[p][1])) {
          const t = a[i]; a[i] = a[p]; a[p] = t; i = p;
        } else break;
      }
    }
    pop() {
      const a = this.a, top = a[0], last = a.pop();
      if (a.length) {
        a[0] = last;
        let i = 0;
        for (;;) {
          const l = 2 * i + 1, r = l + 1; let m = i;
          if (l < a.length && (a[l][0] < a[m][0] || (a[l][0] === a[m][0] && a[l][1] < a[m][1]))) m = l;
          if (r < a.length && (a[r][0] < a[m][0] || (a[r][0] === a[m][0] && a[r][1] < a[m][1]))) m = r;
          if (m === i) break;
          const t = a[i]; a[i] = a[m]; a[m] = t; i = m;
        }
      }
      return top;
    }
  }

  /* ═══════════════════════════════════════════════════════════ kernel ═══ */

  class Ev {
    constructor(sim) { this.sim = sim; this.cbs = []; this.done = false; this.value = null; }
    add(fn) { if (this.done) this.sim.at(0, () => fn(this)); else this.cbs.push(fn); }
    succeed(v) { if (!this.done) this.sim.at(0, () => this.fire(v)); return this; }
    fire(v) {
      if (this.done) return;
      this.done = true; this.value = v;
      const cbs = this.cbs; this.cbs = [];
      for (let i = 0; i < cbs.length; i++) cbs[i](this);
    }
  }

  class Proc extends Ev {
    constructor(sim, gen) { super(sim); this.gen = gen; sim.at(0, () => this.step(undefined)); }
    step(v) {
      let r;
      try { r = this.gen.next(v); }
      catch (e) {
        // Never swallow this. A process that dies quietly makes a one-character
        // typo look like a legitimate simulation result — every counter reads
        // zero and nothing says why.
        if (this.sim.onError) this.sim.onError(e);
        else if (typeof console !== "undefined") console.error("process crashed:", e);
        this.sim.crashes = (this.sim.crashes || 0) + 1;
        this.sim.lastError = e;
        this.fire(null);
        return;
      }
      if (r.done) { this.fire(r.value); return; }
      r.value.add((e) => this.step(e.value));
    }
  }

  class Sim {
    constructor(seed) { this.now = 0; this.h = new Heap(); this.seq = 0; this.rng = new RNG(seed); }
    at(d, fn) { this.h.push([this.now + d, this.seq++, fn]); }
    timeout(d, v) { const e = new Ev(this); this.at(d, () => e.fire(v === undefined ? null : v)); return e; }
    event() { return new Ev(this); }
    process(gen) { return new Proc(this, gen); }
    anyOf(evs) {
      const out = new Ev(this); let won = false;
      const done = (e) => { if (!won) { won = true; out.succeed(e); } };
      for (let i = 0; i < evs.length; i++) evs[i].add(done);
      return out;
    }
    run(until) {
      const h = this.h; let guard = 0;
      while (h.size && h.peek()[0] <= until) {
        if (++guard > 8e6) break;              // runaway protection for the browser
        const it = h.pop(); this.now = it[0]; it[2]();
      }
      this.now = Math.max(this.now, until);
    }
  }

  class Resource {
    constructor(sim, cap) { this.sim = sim; this.cap = Math.max(1, cap | 0); this.inUse = 0; this.q = []; }
    setCap(c) { this.cap = Math.max(1, c | 0); this.grant(); }
    request() { const e = new Ev(this.sim); this.q.push(e); this.grant(); return e; }
    cancel(e) { const i = this.q.indexOf(e); if (i >= 0) { this.q.splice(i, 1); return true; } return false; }
    release() { this.inUse--; this.grant(); }
    grant() { while (this.q.length && this.inUse < this.cap) { this.inUse++; this.q.shift().succeed(true); } }
  }

  class TokenBucket {
    constructor(perMinute) { this.limit = perMinute; this.rate = perMinute / 60; this.tokens = perMinute; this.t = 0; }
    refill(now) { if (now > this.t) { this.tokens = Math.min(this.limit, this.tokens + (now - this.t) * this.rate); this.t = now; } }
    level(now) { this.refill(now); return this.tokens; }
    take(now, n) { this.refill(now); if (n > this.limit) return false; if (this.tokens >= n) { this.tokens -= n; return true; } return false; }
    wait(now, n) { this.refill(now); if (n > this.limit) return Infinity; if (this.tokens >= n) return 0; return (n - this.tokens) / this.rate; }
    give(now, n) { this.refill(now); this.tokens = Math.min(this.limit, this.tokens + n); }
    util(now) { return this.limit ? 1 - this.level(now) / this.limit : 0; }
  }

  /* ════════════════════════════════════════════════════════ the fixture ═══ */

  const MODELS = {
    haiku:  { id: "claude-haiku-4-5",  rpm: 4000, itpm: 4000000, otpm: 800000,
              prefill: 12000, decode: 110, base: 0.18, pin: 1, pout: 5,  servers: 400 },
    sonnet: { id: "claude-sonnet-5",   rpm: 2000, itpm: 8000000, otpm: 1000000,
              prefill: 8000,  decode: 65,  base: 0.32, pin: 3, pout: 15, servers: 200 },
    opus:   { id: "claude-opus-5",     rpm: 400,  itpm: 400000,  otpm: 320000,
              prefill: 5000,  decode: 35,  base: 0.45, pin: 5, pout: 25, servers: 80 },
  };

  const CRIT = { CRITICAL_PLUS: 0, CRITICAL: 1, SHEDDABLE_PLUS: 2, SHEDDABLE: 3 };
  const CRIT_NAME = ["CRITICAL_PLUS", "CRITICAL", "SHEDDABLE_PLUS", "SHEDDABLE"];

  const CLASSES = [
    { name: "guard",   model: "haiku",  crit: CRIT.CRITICAL,       perTurn: 1.0,
      inTok: [420, 90],    outTok: [12, 3],     maxTokens: 64,   stream: false,
      deadline: 2.5,  ttft: 2.5,  total: 4,   attempts: 2, fallback: "queue_for_review",
      note: "safety / moderation classifier on the inbound turn" },
    { name: "rewrite", model: "haiku",  crit: CRIT.SHEDDABLE_PLUS, perTurn: 0.85,
      inTok: [900, 220],   outTok: [70, 20],    maxTokens: 256,  stream: false,
      deadline: 2.5,  ttft: 2.0,  total: 3,   attempts: 1, fallback: "raw_query",
      note: "query rewrite ahead of retrieval" },
    { name: "answer",  model: "sonnet", crit: CRIT.CRITICAL_PLUS,  perTurn: 1.0,
      inTok: [6200, 1400], outTok: [520, 190],  maxTokens: 2048, stream: true,
      deadline: 40,   ttft: 8,    total: 45,  attempts: 2, fallback: "none",
      note: "grounded answer over retrieved context, streamed" },
    { name: "think",   model: "opus",   crit: CRIT.CRITICAL,       perTurn: 0.08,
      inTok: [6800, 1500], outTok: [2400, 900], maxTokens: 8192, stream: true,
      deadline: 180,  ttft: 25,   total: 200, attempts: 1, fallback: "downgrade",
      note: "extended-thinking escalation for hard turns" },
    { name: "title",   model: "haiku",  crit: CRIT.SHEDDABLE,      perTurn: 0.18,
      inTok: [1600, 400],  outTok: [28, 8],     maxTokens: 64,   stream: false,
      deadline: 300,  ttft: 3,    total: 6,   attempts: 1, fallback: "defer_to_batch",
      note: "conversation title — nobody is waiting on it" },
  ];
  const BY_NAME = {};
  CLASSES.forEach((c) => (BY_NAME[c.name] = c));

  function cost(model, inTok, outTok) {
    return (inTok * model.pin + outTok * model.pout) / 1e6;
  }
  function serviceTime(cls) {
    const m = MODELS[cls.model];
    return m.base + cls.inTok[0] / m.prefill + cls.outTok[0] / m.decode;
  }

  /* ─── capacity arithmetic (the part that needs no simulation) ─────────── */

  function binding(cls) {
    const m = MODELS[cls.model];
    const byRpm = m.rpm / 60;
    const byItpm = m.itpm / 60 / cls.inTok[0];
    const byOtpm = m.otpm / 60 / cls.outTok[0];
    const unrec = m.otpm / 60 / cls.maxTokens;
    const concCap = m.otpm / cls.maxTokens;
    const quota = Math.min(byRpm, byItpm, byOtpm);
    const svc = serviceTime(cls);
    const concNeed = quota * svc;
    const eff = Math.min(quota, concCap / svc);
    const binds = byRpm <= byItpm && byRpm <= byOtpm ? "RPM" : byItpm <= byOtpm ? "ITPM" : "OTPM";
    return { cls, byRpm, byItpm, byOtpm, unrec, concCap, concNeed, quota, eff, binds, svc,
             concBound: concCap < concNeed };
  }

  function headroom(turnsRps, classes) {
    const cs = classes || CLASSES;
    const out = {};
    for (const k in MODELS) out[k] = { rpm: 0, itpm: 0, otpm: 0, conc: 0 };
    cs.forEach((c) => {
      const rps = turnsRps * c.perTurn, row = out[c.model];
      row.rpm += rps * 60; row.itpm += rps * 60 * c.inTok[0];
      row.otpm += rps * 60 * c.outTok[0]; row.conc += rps * serviceTime(c) * c.maxTokens;
    });
    const res = {};
    for (const k in MODELS) {
      const m = MODELS[k];
      res[k] = { rpm: out[k].rpm / m.rpm, itpm: out[k].itpm / m.itpm,
                 otpm: out[k].otpm / m.otpm, conc: out[k].conc / m.otpm };
    }
    return res;
  }

  function worstMeter(turnsRps, classes) {
    const h = headroom(turnsRps, classes); let w = 0;
    for (const k in h) for (const m in h[k]) w = Math.max(w, h[k][m]);
    return w;
  }

  function sustainableTurnsRps(classes) {
    let lo = 0, hi = 1e5;
    for (let i = 0; i < 80; i++) {
      const mid = (lo + hi) / 2;
      if (worstMeter(mid, classes) > 1) hi = mid; else lo = mid;
    }
    return lo;
  }

  /* ═════════════════════════════════════════════════════════ provider ═══ */

  class Endpoint {
    constructor(sim, key) {
      this.sim = sim; this.key = key; this.m = MODELS[key];
      this.rpm = new TokenBucket(this.m.rpm);
      this.itpm = new TokenBucket(this.m.itpm);
      this.otpm = new TokenBucket(this.m.otpm);
      this.inFlight = 0; this.queue = 0;
      this.scale = 1; this.errorRate = 0.002; this.overloadRatio = 3;
      this.quotaScale = 1;
      this.stats = { ok: 0, "429": 0, "529": 0, "500": 0 };
      this.tokensIn = 0; this.tokensOut = 0; this.cost = 0;
    }
    get servers() { return Math.max(1, this.m.servers * this.scale); }

    /* Providers do not only get slower — sometimes they cut your allowance.
     * Scaling the three meters is a different failure mode from scaling the
     * fleet: it produces 429s rather than queueing and 529s, and the correct
     * client response is the opposite one. */
    setQuota(scale) {
      if (scale === this.quotaScale) return;
      this.quotaScale = scale;
      var now = this.sim.now;
      var pairs = [[this.rpm, this.m.rpm], [this.itpm, this.m.itpm], [this.otpm, this.m.otpm]];
      for (var i = 0; i < pairs.length; i++) {
        var b = pairs[i][0], base = pairs[i][1];
        b.refill(now);
        b.limit = Math.max(1, base * scale);
        b.rate = b.limit / 60;
        b.tokens = Math.min(b.tokens, b.limit);
      }
    }
    meter(req) {
      const now = this.sim.now;
      const checks = [["rpm", this.rpm, 1], ["itpm", this.itpm, req.inTok],
                      ["otpm", this.otpm, req.maxTokens]];
      for (const [name, b, amt] of checks) {
        const w = b.wait(now, amt);
        if (w > 0) { this.stats["429"]++; return { status: "429", retryAfter: Math.min(60, isFinite(w) ? w : 60), limit: name }; }
      }
      this.rpm.take(now, 1); this.itpm.take(now, req.inTok); this.otpm.take(now, req.maxTokens);
      return null;
    }
    refund(req) {
      const n = this.sim.now;
      this.rpm.give(n, 1); this.itpm.give(n, req.inTok); this.otpm.give(n, req.maxTokens);
    }
    reconcile(req, actualOut) {
      const slack = Math.max(0, req.maxTokens - actualOut);
      if (slack) this.otpm.give(this.sim.now, slack);
    }
    svc(req) {
      const rho = Math.min(0.985, Math.max(0, (this.inFlight + this.queue) / this.servers));
      // M/M/c, not M/M/1: flat until utilisation is high, then a hard corner.
      const stretch = Math.min(30, 1 + Math.pow(rho, 8) / (1 - rho));
      const jit = this.sim.rng.lognorm(0, 0.25);
      const ttft = (this.m.base + req.inTok / this.m.prefill) * stretch * jit;
      const total = ttft + (req.outTok / this.m.decode) * Math.min(stretch, 4) * jit;
      return [ttft, total];
    }
    call(req, ttftEv) { return this.sim.process(this._call(req, ttftEv)); }
    *_call(req, ttftEv) {
      const sim = this.sim, rng = sim.rng;
      const limited = this.meter(req);
      if (limited) { yield sim.timeout(0.02 + rng.random() * 0.03); return limited; }
      if (this.queue > this.overloadRatio * this.servers) {
        this.stats["529"]++; this.refund(req);
        yield sim.timeout(0.03 + rng.random() * 0.05);
        return { status: "529", retryAfter: 5 + rng.random() * 5 };
      }
      this.queue++;
      let waited = 0;
      while (this.inFlight >= this.servers) {
        waited += 0.05; yield sim.timeout(0.05);
        if (waited > 30) break;
      }
      this.queue--; this.inFlight++;
      const [ttft, total] = this.svc(req);
      yield sim.timeout(ttft);
      if (ttftEv) ttftEv.succeed(true);
      yield sim.timeout(Math.max(0, total - ttft));
      this.inFlight--;
      if (rng.random() < this.errorRate) {
        this.stats["500"]++; this.reconcile(req, 0);
        return { status: "500", ttft: ttft, duration: total };
      }
      this.stats.ok++; this.reconcile(req, req.outTok);
      this.tokensIn += req.inTok; this.tokensOut += req.outTok;
      this.cost += cost(this.m, req.inTok, req.outTok);
      return { status: "ok", ttft: ttft, duration: total, inTok: req.inTok, outTok: req.outTok };
    }
  }

  class Provider {
    constructor(sim) {
      this.sim = sim; this.ep = {};
      for (const k in MODELS) this.ep[k] = new Endpoint(sim, k);
    }
    call(req, ttftEv) { return this.ep[req.model].call(req, ttftEv); }
    setHealth(model, scale, errorRate, quotaScale) {
      const e = this.ep[model];
      e.scale = scale;
      e.errorRate = errorRate;
      e.setQuota(quotaScale === undefined ? 1 : quotaScale);
    }
    totals() {
      const t = { ok: 0, "429": 0, "529": 0, "500": 0, cost: 0 };
      for (const k in this.ep) {
        const e = this.ep[k];
        t.ok += e.stats.ok; t["429"] += e.stats["429"];
        t["529"] += e.stats["529"]; t["500"] += e.stats["500"]; t.cost += e.cost;
      }
      return t;
    }
  }

  /* ═════════════════════════════════════════════ pattern definitions ═══ */

  /* Each pattern is a wrapper: it may refuse, may delay, and may call
   * `next()` zero, one, or many times. The chain nests them outermost-first,
   * so the list order is genuinely the semantics.                          */

  const PATTERNS = [
    {
      key: "fallback", label: "Fallback / Degrade", short: "FB", colour: "#8b5cf6",
      blurb: "Turns a failure into a worse-but-real answer. Never fabricates one.",
      why: "Belongs outermost: it is the only stage that decides what the user actually gets, so it must see every failure from every gate inside it.",
      params: [
        { k: "mode", label: "On failure", type: "select", opts: ["degrade", "hard-error"], def: "degrade" },
      ],
    },
    {
      key: "admission", label: "Admission Control", short: "AC", colour: "#22c55e",
      blurb: "Sheds whole call classes by criticality as pressure rises. The only gate that says no for free.",
      why: "Belongs first among the real gates: a shed here costs one dict lookup. The same shed four gates later has already burned a queue slot, a quota reservation and a prefill you paid for.",
      params: [
        { k: "l1", label: "shed SHEDDABLE at", type: "range", min: 0.3, max: 1, step: 0.01, def: 0.80 },
        { k: "l2", label: "+ SHEDDABLE_PLUS at", type: "range", min: 0.3, max: 1, step: 0.01, def: 0.88 },
        { k: "l3", label: "+ CRITICAL at", type: "range", min: 0.3, max: 1, step: 0.01, def: 0.95 },
        { k: "deadlineCheck", label: "deadline feasibility", type: "bool", def: true },
      ],
    },
    {
      key: "cache", label: "Response Cache", short: "$", colour: "#06b6d4",
      blurb: "The only lever that removes a call instead of shrinking it.",
      why: "Not a resilience pattern at all, which is the point — it moves the capacity edge, while everything else decides how you behave at it.",
      params: [{ k: "hitRate", label: "hit rate", type: "range", min: 0, max: 0.9, step: 0.05, def: 0 }],
    },
    {
      key: "bulkhead", label: "Bulkhead + Queue", short: "BH", colour: "#f59e0b",
      blurb: "Per-class concurrency with a bounded queue. Isolation, so one slow class cannot starve another.",
      why: "Must sit outside the quota gate: otherwise a stalled class holds token reservations it cannot use.",
      params: [
        { k: "sizing", label: "slots (x Little's Law)", type: "range", min: 0.25, max: 4, step: 0.25, def: 1.5 },
        { k: "queueMult", label: "queue depth (x slots)", type: "range", min: 0, max: 6, step: 0.5, def: 2 },
        { k: "policy", label: "queue policy", type: "select", opts: ["lifo", "fifo"], def: "lifo" },
        { k: "codel", label: "CoDel drop", type: "bool", def: true },
      ],
    },
    {
      key: "concurrency", label: "Adaptive Concurrency", short: "CC", colour: "#3b82f6",
      blurb: "Infers the provider's current ceiling from latency. AIMD or the Netflix gradient.",
      why: "Bulkheads bound you by a static class budget; this bounds you by what the provider looks able to absorb right now, which moves.",
      params: [
        { k: "algo", label: "controller", type: "select", opts: ["gradient", "aimd", "fixed"], def: "gradient" },
        { k: "sizing", label: "initial (x Little's Law)", type: "range", min: 0.1, max: 3, step: 0.1, def: 1.3 },
        { k: "signal", label: "latency signal", type: "select", opts: ["ttft", "total"], def: "ttft" },
        { k: "perClass", label: "scope per class", type: "bool", def: true },
      ],
    },
    {
      key: "breaker", label: "Circuit Breaker", short: "CB", colour: "#ef4444",
      blurb: "Detects a broken dependency and fails fast instead of timing out per caller.",
      why: "Before the quota gate: when the circuit is open we want to fail in microseconds and not spend quota we could use on a model that still works.",
      params: [
        { k: "threshold", label: "failure ratio", type: "range", min: 0.05, max: 0.9, step: 0.05, def: 0.30 },
        { k: "window", label: "window (s)", type: "range", min: 5, max: 120, step: 5, def: 60 },
        { k: "minCalls", label: "min calls", type: "range", min: 5, max: 100, step: 5, def: 20 },
        { k: "openFor", label: "open for (s)", type: "range", min: 2, max: 60, step: 1, def: 20 },
        { k: "probes", label: "probes to close", type: "range", min: 1, max: 10, step: 1, def: 3 },
        { k: "counts429", label: "count 429 as failure", type: "bool", def: false },
        { k: "expBackoff", label: "exponential open backoff", type: "bool", def: false },
      ],
    },
    {
      key: "ratelimit", label: "Client Rate Limiter", short: "RL", colour: "#14b8a6",
      blurb: "Meters yourself on RPM / ITPM / OTPM so a 429 never has to happen.",
      why: "Last before the call: it must reflect reality at the instant of the call. Reserve max_tokens, call, reconcile against the real output.",
      params: [
        { k: "headroom", label: "headroom", type: "range", min: 0.5, max: 1, step: 0.05, def: 0.9 },
        { k: "maxWait", label: "max wait (s)", type: "range", min: 0, max: 10, step: 0.5, def: 2 },
        { k: "reconcile", label: "reconcile max_tokens", type: "bool", def: true },
        { k: "instances", label: "pods sharing quota", type: "range", min: 1, max: 32, step: 1, def: 1 },
      ],
    },
    {
      key: "retry", label: "Retries + Timeouts", short: "RT", colour: "#eab308",
      blurb: "Per-operation timeouts, error classification, jittered backoff, and a budget.",
      why: "Innermost, normally. Put it outside the quota gate and every attempt re-reserves tokens; outside the breaker and you retry into an open circuit.",
      params: [
        { k: "attempts", label: "max attempts", type: "range", min: 1, max: 5, step: 1, def: 2 },
        { k: "base", label: "base delay (s)", type: "range", min: 0.1, max: 5, step: 0.1, def: 1 },
        { k: "cap", label: "max delay (s)", type: "range", min: 1, max: 60, step: 1, def: 20 },
        { k: "jitter", label: "jitter", type: "select", opts: ["full", "equal", "none"], def: "full" },
        { k: "budget", label: "budget (x successes)", type: "range", min: 0, max: 1, step: 0.05, def: 0.10 },
        { k: "retryAfter", label: "honour retry-after", type: "bool", def: true },
        { k: "perOp", label: "per-operation timeouts", type: "bool", def: true },
      ],
    },
  ];
  const PATTERN_BY_KEY = {};
  PATTERNS.forEach((p) => (PATTERN_BY_KEY[p.key] = p));

  const DEFAULT_CHAIN = ["fallback", "admission", "bulkhead", "concurrency", "breaker", "ratelimit", "retry"];

  function defaultParams() {
    const out = {};
    PATTERNS.forEach((p) => {
      out[p.key] = {};
      p.params.forEach((q) => (out[p.key][q.k] = q.def));
    });
    return out;
  }

  /* ═══════════════════════════════════════════ pattern implementations ═══ */

  class Breaker {
    constructor(sim, cfg) {
      this.sim = sim; this.cfg = cfg; this.state = "closed";
      this.win = []; this.openedAt = 0; this.openFor = cfg.openFor;
      this.trips = 0; this.rejected = 0; this.consecutive = 0;
      this.probesIn = 0; this.probesOk = 0; this.transitions = [];
    }
    allow() {
      const now = this.sim.now;
      if (this.state === "open") {
        if (now - this.openedAt >= this.openFor) { this._set("half"); this.probesIn = 0; this.probesOk = 0; }
        else { this.rejected++; return false; }
      }
      if (this.state === "half") {
        if (this.probesIn >= 1) { this.rejected++; return false; }
        this.probesIn++; return true;
      }
      return true;
    }
    abandon() { if (this.state === "half" && this.probesIn > 0) this.probesIn--; }
    record(status) {
      if (status === "429" && !this.cfg.counts429) return;   // ← the load-bearing line
      if (status === "400") return;
      const failed = status !== "ok";
      if (this.state === "half") {
        this.probesIn = Math.max(0, this.probesIn - 1);
        if (failed) this._trip();
        else if (++this.probesOk >= this.cfg.probes) this._close();
        return;
      }
      this.win.push([this.sim.now, failed]);
      this._evict();
      if (this.win.length >= this.cfg.minCalls) {
        let f = 0; for (const w of this.win) if (w[1]) f++;
        if (f / this.win.length >= this.cfg.threshold) this._trip();
      }
    }
    _evict() { const cut = this.sim.now - this.cfg.window; while (this.win.length && this.win[0][0] < cut) this.win.shift(); }
    _trip() {
      // capture the ratio before clearing the window, or the log always says 0%
      if (this.win.length) {
        let f = 0; for (const w of this.win) if (w[1]) f++;
        this.tripRatio = f / this.win.length;
      }
      this._set("open"); this.openedAt = this.sim.now; this.consecutive++;
      // Exponential open-duration backoff is an *enhancement*, not the pattern.
      // pybreaker and Resilience4j both use a fixed reset timeout by default,
      // and for good reason: across a bounded brownout the doubling compounds
      // (20s → 40s → 80s) and the circuit stays open long after the dependency
      // has recovered. Off by default here; turn it on to see the overshoot.
      this.openFor = this.cfg.expBackoff
        ? Math.min(120, this.cfg.openFor * Math.pow(2, this.consecutive - 1))
        : this.cfg.openFor;
      this.win.length = 0; this.trips++;
    }
    _close() { this._set("closed"); this.consecutive = 0; this.openFor = this.cfg.openFor; this.win.length = 0; }
    _set(s) { if (s !== this.state) { this.state = s; this.transitions.push([this.sim.now, s]); } }
    ratio() { this._evict(); if (!this.win.length) return 0; let f = 0; for (const w of this.win) if (w[1]) f++; return f / this.win.length; }
  }

  class Limiter {
    constructor(algo, initial) {
      this.algo = algo; this.limit = initial; this.min = 4; this.max = 4000;
      this.win = []; this.winSize = 24; this.noload = Infinity; this.history = [];
    }
    get value() { return Math.max(1, Math.round(this.limit)); }
    success(rtt, inFlight) {
      if (this.algo === "fixed") return;
      if (this.algo === "aimd") {
        if (inFlight >= this.limit * 0.8) this.limit = Math.min(this.max, this.limit + 1 / this.limit);
        return;
      }
      if (rtt <= 0) return;
      this.win.push(rtt);
      // The sample window must scale with the limit. A fixed 24-sample window
      // is fine at limit=100 and fatal at limit=4: after a multiplicative
      // decrease the controller needs 24 successes to make one update, but a
      // limit of 4 only produces a handful per second — so it sits at the floor
      // long after the dependency recovered, and the floor itself is what keeps
      // throughput low. The controller death-spirals on its own output.
      const need = Math.max(4, Math.min(this.winSize, Math.ceil(this.limit)));
      if (this.win.length < need) return;
      // min-of-window vs long-run min. A mean-vs-min gradient sits below 1.0
      // even on an idle dependency and decays the limit to the floor.
      let s = Infinity; for (const x of this.win) if (x < s) s = x;
      this.win.length = 0;
      if (s < this.noload || !isFinite(this.noload)) this.noload = s;
      else this.noload += (s - this.noload) * 0.05;
      const g = Math.max(0.5, Math.min(1, this.noload / s));
      if (g < 1 && inFlight < this.limit * 0.5) return;  // never shrink an idle limit
      const target = this.limit * g + Math.sqrt(this.limit);
      this.limit = Math.max(this.min, Math.min(this.max, 0.8 * this.limit + 0.2 * target));
    }
    failure(status) {
      if (this.algo === "fixed") return;
      this.limit = status === "429"
        ? Math.max(this.min, this.limit * 0.9)
        : Math.max(this.min, this.limit * 0.5);
    }
  }

  class Bulkhead {
    constructor(sim, cfg, capacity) {
      this.sim = sim; this.cfg = cfg; this.cap = Math.max(1, capacity | 0);
      this.maxQ = Math.max(0, Math.round(this.cap * cfg.queueMult));
      this.inUse = 0; this.q = [];
      this.firstAbove = 0; this.dropping = false; this.dropNext = 0; this.dropCount = 0;
      this.maxDepth = 0; this.rejFull = 0; this.rejCodel = 0; this.rejDeadline = 0;
    }
    acquire(deadlineAt) {
      const e = new Ev(this.sim);
      if (this.inUse < this.cap && !this.q.length) { this.inUse++; e.succeed(true); return e; }
      if (this.q.length >= this.maxQ) { this.rejFull++; e.succeed("queue-full"); return e; }
      this.q.push({ e, at: this.sim.now, deadlineAt });
      this.maxDepth = Math.max(this.maxDepth, this.q.length);
      return e;
    }
    release() { this.inUse--; this.dispatch(); }
    dispatch() {
      const now = this.sim.now;
      while (this.inUse < this.cap && this.q.length) {
        const alive = [];
        for (const it of this.q) {
          if (it.deadlineAt <= now) { this.rejDeadline++; it.e.succeed("deadline-in-queue"); }
          else alive.push(it);
        }
        this.q = alive;
        if (!this.q.length) return;
        const it = this.cfg.policy === "fifo" ? this.q.shift() : this.q.pop();
        const sojourn = now - it.at;
        if (this.cfg.codel && this._codel(now, sojourn)) { this.rejCodel++; it.e.succeed("codel-drop"); continue; }
        this.inUse++; it.e.succeed(true);
      }
    }
    _codel(now, sojourn) {
      const target = 0.05, interval = 1;
      if (sojourn < target) { this.firstAbove = 0; this.dropping = false; return false; }
      if (this.firstAbove === 0) { this.firstAbove = now + interval; return false; }
      if (!this.dropping && now >= this.firstAbove) {
        this.dropping = true; this.dropCount = 1; this.dropNext = now + interval; return true;
      }
      if (this.dropping && now >= this.dropNext) {
        this.dropCount++; this.dropNext = now + interval / Math.sqrt(this.dropCount); return true;
      }
      return false;
    }
  }

  class Quota {
    constructor(sim, cfg) {
      this.sim = sim; this.cfg = cfg;
      const share = (cfg.instances > 1 ? 1 / cfg.instances : 1) * cfg.headroom;
      this.b = {};
      for (const k in MODELS) {
        this.b[k] = [new TokenBucket(MODELS[k].rpm * share),
                     new TokenBucket(MODELS[k].itpm * share),
                     new TokenBucket(MODELS[k].otpm * share)];
      }
      this.denied = 0; this.returned = 0;
    }
    wait(model, inTok, maxTokens) {
      const n = this.sim.now, b = this.b[model];
      return Math.max(b[0].wait(n, 1), b[1].wait(n, inTok), b[2].wait(n, maxTokens));
    }
    reserve(model, inTok, maxTokens) {
      const n = this.sim.now, b = this.b[model];
      if (b[0].wait(n, 1) > 0 || b[1].wait(n, inTok) > 0 || b[2].wait(n, maxTokens) > 0) { this.denied++; return null; }
      b[0].take(n, 1); b[1].take(n, inTok); b[2].take(n, maxTokens);
      return { model, inTok, maxTokens };
    }
    reconcile(res, actualOut) {
      if (!res || !this.cfg.reconcile) return;
      const slack = Math.max(0, res.maxTokens - actualOut);
      if (slack) { this.b[res.model][2].give(this.sim.now, slack); this.returned += slack; }
    }
    pressure(model) {
      const n = this.sim.now, b = this.b[model];
      return Math.max(b[0].util(n), b[1].util(n), b[2].util(n));
    }
  }

  class RetryBudget {
    constructor(ratio) { this.ratio = ratio; this.tokens = 10; this.cap = 200; this.granted = 0; this.denied = 0; }
    ok() { this.tokens = Math.min(this.cap, this.tokens + this.ratio); }
    take() { if (this.tokens >= 1) { this.tokens -= 1; this.granted++; return true; } this.denied++; return false; }
  }

  function classify(status) {
    if (status === "400" || status === "401") return "no";
    if (status === "429" || status === "529") return "after";
    if (status === "500" || status === "timeout") return "yes";
    return "no";
  }
  function backoff(rng, attempt, base, cap, retryAfter, jitter) {
    const w = Math.min(cap, base * Math.pow(2, Math.max(0, attempt - 1)));
    let d;
    if (jitter === "none") d = w;
    else if (jitter === "equal") d = w / 2 + rng.random() * w / 2;
    else d = rng.random() * w;
    return Math.max(retryAfter || 0, d);
  }

  /* ═══════════════════════════════════════════════════════════ context ═══ */

  class Ctx {
    constructor(sim, provider, chain, params, scenario) {
      this.sim = sim; this.provider = provider; this.chain = chain;
      this.p = params; this.scenario = scenario;
      this.active = {}; chain.forEach((k) => (this.active[k] = true));

      this.breakers = {}; this.bulkheads = {}; this.limiters = {}; this.slots = {};
      this.quota = this.active.ratelimit ? new Quota(sim, params.ratelimit) : null;
      this.budget = new RetryBudget(params.retry.budget);

      CLASSES.forEach((c) => {
        const b = binding(c);
        const need = Math.max(1, b.eff * b.svc);
        if (this.active.breaker) this.breakers[c.name] = new Breaker(sim, params.breaker);
        if (this.active.bulkhead) this.bulkheads[c.name] = new Bulkhead(sim, params.bulkhead, Math.max(2, Math.round(need * params.bulkhead.sizing)));
        if (this.active.concurrency) {
          const key = params.concurrency.perClass ? c.name : c.model;
          if (!this.limiters[key]) {
            const init = Math.max(4, Math.round(need * params.concurrency.sizing));
            this.limiters[key] = new Limiter(params.concurrency.algo, init);
            this.slots[key] = new Resource(sim, init);
          }
        }
      });

      this.m = newMetrics(scenario.duration);
      this.log = [];
      this.funnel = {};                       // gate key -> stops
      PATTERNS.forEach((p) => (this.funnel[p.key] = 0));
      this.funnel.served = 0; this.funnel.failed = 0; this.funnel.degraded = 0;
      this.samples = [];                      // per-second snapshots for charts
      this.deadlineLosses = 0;
    }
    ccKey(cls) { return this.p.concurrency.perClass ? cls.name : cls.model; }
    timeoutsFor(cls) {
      if (this.active.retry && this.p.retry.perOp) return { ttft: cls.ttft, total: cls.total };
      return { ttft: this.scenario.globalTimeout, total: this.scenario.globalTimeout };
    }
    say(kind, text) {
      if (this.log.length < 400) this.log.push({ t: this.sim.now, kind, text });
    }
  }

  function newMetrics(duration) {
    const m = { byClass: {}, bins: [], duration: duration,
                turns: 0, complete: 0, degraded: 0, failed: 0,
                costUseful: 0, costWasted: 0, attempts: 0, retries: 0 };
    CLASSES.forEach((c) => {
      m.byClass[c.name] = { offered: 0, ok: 0, shed: 0, err: 0, fallback: 0, lat: [], ttft: [] };
    });
    for (let i = 0; i < Math.ceil(duration) + 1; i++) {
      m.bins.push({ offered: 0, good: 0, shed: 0, err: 0, retries: 0,
                    r429: 0, r529: 0, r500: 0, lat: [], inFlight: 0,
                    limit: 0, pressure: 0, open: 0 });
    }
    return m;
  }

  /* ═══════════════════════════════════════════════════ the call path ═══ */

  function* doCall(ctx, req) {
    const sim = ctx.sim, cls = req.cls, m = MODELS[cls.model];
    const inTok = Math.max(50, sim.rng.gauss(cls.inTok[0], cls.inTok[1]));
    const outTok = Math.max(1, Math.min(cls.maxTokens, sim.rng.gauss(cls.outTok[0], cls.outTok[1])));
    const preq = { model: cls.model, inTok, outTok, maxTokens: cls.maxTokens };
    req.lastIn = inTok;

    ctx.m.attempts++;
    if (req.attempt > 1) { ctx.m.retries++; bin(ctx).retries++; }

    const ttftEv = sim.event();
    const call = ctx.provider.call(preq, ttftEv);
    const t0 = sim.now;
    const tos = ctx.timeoutsFor(cls);

    let ttftBudget = Math.min(tos.ttft, req.deadlineAt - sim.now);
    let deadlineLimited = ttftBudget < tos.ttft;
    const first = yield sim.anyOf([ttftEv, call, sim.timeout(Math.max(0.01, ttftBudget))]);

    let status = "timeout", resp = null, ttft = 0, streamFrac = 0;
    if (first === call) {
      resp = call.value; status = resp.status; ttft = resp.ttft || 0; deadlineLimited = false;
    } else if (first === ttftEv) {
      ttft = sim.now - t0;
      const opTotal = tos.total - ttft;
      const totalBudget = Math.min(opTotal, req.deadlineAt - sim.now);
      deadlineLimited = totalBudget < opTotal;
      const done = yield sim.anyOf([call, sim.timeout(Math.max(0.01, totalBudget))]);
      if (done === call) { resp = call.value; status = resp.status; deadlineLimited = false; }
      else {
        status = "timeout";
        const expDecode = Math.max(0.01, cls.outTok[0] / m.decode);
        streamFrac = Math.min(1, (sim.now - t0 - ttft) / expDecode);
      }
    }

    const actualOut = resp && resp.status === "ok" ? resp.outTok : 0;
    if (ctx.quota && req.reservation) { ctx.quota.reconcile(req.reservation, actualOut); req.reservation = null; }

    // Feed the two controllers — but only when the dependency is actually at
    // fault. A deadline we blew while queueing on our own side is our problem;
    // telling the breaker and the limiter about it makes both shrink in
    // response to their own queueing, which is a self-reinforcing collapse.
    if (deadlineLimited) {
      ctx.deadlineLosses++;
      if (ctx.breakers[cls.name]) ctx.breakers[cls.name].abandon();
    } else {
      if (ctx.breakers[cls.name]) ctx.breakers[cls.name].record(status);
      const lim = ctx.limiters[ctx.ccKey(cls)];
      if (lim) {
        const slot = ctx.slots[ctx.ccKey(cls)];
        const signal = ctx.p.concurrency.signal === "total" ? (resp ? resp.duration : sim.now - t0) : ttft;
        if (status === "ok") lim.success(Math.max(0.001, signal), slot.inUse);
        else lim.failure(status);
        slot.setCap(lim.value);
      }
    }

    const b = bin(ctx);
    if (status === "429") b.r429++; else if (status === "529") b.r529++; else if (status === "500") b.r500++;

    if (status === "ok") {
      ctx.budget.ok();
      ctx.m.costUseful += cost(m, resp.inTok, resp.outTok);
      return { ok: true, status: "ok", ttft, outTok: resp.outTok, latency: sim.now - req.started };
    }
    ctx.m.costWasted += cost(m, inTok, 0);
    return { ok: false, status, ttft, retryAfter: resp ? resp.retryAfter || 0 : 0,
             streamFrac, stoppedAt: "provider", deadlineLimited };
  }

  const GATE = {
    /* ── ⑦ fallback ───────────────────────────────────────────────────── */
    *fallback(ctx, req, next) {
      const r = yield* next();
      if (r.ok) return r;
      if (ctx.p.fallback.mode === "hard-error") return r;
      const fb = req.cls.fallback;
      if (fb === "none") return r;             // the answer path has no fallback
      ctx.funnel.degraded++;
      return { ok: true, degraded: true, status: "fallback:" + fb,
               latency: ctx.sim.now - req.started, outTok: 0, stoppedAt: r.stoppedAt };
    },

    /* ── ① admission ──────────────────────────────────────────────────── */
    *admission(ctx, req, next) {
      const cfg = ctx.p.admission;
      // Pressure must be the worst of BOTH signals. Reading only the token
      // buckets means the ladder never fires when the bottleneck is gateway
      // concurrency — which is the usual case, because the bulkheads and the
      // concurrency limiter are there precisely to stop you reaching the quota.
      // (Reading only CPU would be worse still: an LLM gateway's CPU is idle
      // while its token budget is exhausted.)
      const pressure = Math.max(
        ctx.quota ? ctx.quota.pressure(req.cls.model) : 0,
        ctx.loadPressure(req.cls)
      );
      req.pressure = pressure;
      let floor = CRIT.SHEDDABLE;
      if (pressure >= cfg.l3) floor = CRIT.CRITICAL_PLUS;
      else if (pressure >= cfg.l2) floor = CRIT.CRITICAL;
      else if (pressure >= cfg.l1) floor = CRIT.SHEDDABLE_PLUS;
      if (req.cls.crit > floor) {
        ctx.funnel.admission++;
        return { ok: false, status: "shed", stoppedAt: "admission",
                 reason: "pressure " + pressure.toFixed(2) };
      }
      if (cfg.deadlineCheck) {
        const need = serviceTime(req.cls);
        if (need > req.deadlineAt - ctx.sim.now) {
          ctx.funnel.admission++;
          return { ok: false, status: "shed", stoppedAt: "admission", reason: "deadline" };
        }
      }
      return yield* next();
    },

    /* ── response cache ──────────────────────────────────────────────── */
    *cache(ctx, req, next) {
      if (ctx.sim.rng.random() < ctx.p.cache.hitRate) {
        yield ctx.sim.timeout(0.004);
        ctx.funnel.cache++;
        return { ok: true, cached: true, status: "ok", latency: ctx.sim.now - req.started, outTok: 0 };
      }
      return yield* next();
    },

    /* ── ② bulkhead ───────────────────────────────────────────────────── */
    *bulkhead(ctx, req, next) {
      const bh = ctx.bulkheads[req.cls.name];
      const granted = yield bh.acquire(req.deadlineAt);
      if (granted !== true) {
        ctx.funnel.bulkhead++;
        return { ok: false, status: "shed", stoppedAt: "bulkhead", reason: String(granted) };
      }
      try { return yield* next(); }
      finally { bh.release(); }
    },

    /* ── ③ adaptive concurrency ───────────────────────────────────────── */
    *concurrency(ctx, req, next) {
      const key = ctx.ccKey(req.cls);
      const slot = ctx.slots[key];
      const ev = slot.request();
      const race = yield ctx.sim.anyOf([ev, ctx.sim.timeout(Math.max(0.01, req.deadlineAt - ctx.sim.now))]);
      if (race !== ev) {
        slot.cancel(ev);
        ctx.funnel.concurrency++;
        return { ok: false, status: "shed", stoppedAt: "concurrency", reason: "deadline waiting for a slot" };
      }
      try { return yield* next(); }
      finally { slot.release(); }
    },

    /* ── ④ circuit breaker ────────────────────────────────────────────── */
    *breaker(ctx, req, next) {
      const cb = ctx.breakers[req.cls.name];
      if (!cb.allow()) {
        ctx.funnel.breaker++;
        return { ok: false, status: "open", stoppedAt: "breaker", reason: cb.state };
      }
      return yield* next();
    },

    /* ── ⑤ client-side quota ──────────────────────────────────────────── */
    *ratelimit(ctx, req, next) {
      const cfg = ctx.p.ratelimit, cls = req.cls;
      const estIn = cls.inTok[0];
      const w = ctx.quota.wait(cls.model, estIn, cls.maxTokens);
      if (w > 0) {
        if (w > Math.min(cfg.maxWait, req.deadlineAt - ctx.sim.now)) {
          ctx.funnel.ratelimit++;
          if (ctx.breakers[cls.name]) ctx.breakers[cls.name].abandon();
          return { ok: false, status: "quota", stoppedAt: "ratelimit", reason: "over quota" };
        }
        yield ctx.sim.timeout(w);
      }
      const res = ctx.quota.reserve(cls.model, estIn, cls.maxTokens);
      if (!res) {
        ctx.funnel.ratelimit++;
        if (ctx.breakers[cls.name]) ctx.breakers[cls.name].abandon();
        return { ok: false, status: "quota", stoppedAt: "ratelimit", reason: "lost the race" };
      }
      req.reservation = res;
      try { return yield* next(); }
      finally {
        if (req.reservation) { ctx.quota.reconcile(req.reservation, 0); req.reservation = null; }
      }
    },

    /* ── ⑥ retries + timeouts ─────────────────────────────────────────── */
    *retry(ctx, req, next) {
      const cfg = ctx.p.retry, sim = ctx.sim;
      let last = null;
      for (let attempt = 1; attempt <= cfg.attempts; attempt++) {
        req.attempt = attempt;
        const r = yield* next();
        if (r.ok) return r;
        last = r;
        if (r.stoppedAt && r.stoppedAt !== "provider") return r;   // a gate said no; retrying is pointless
        if (attempt >= cfg.attempts) break;
        const kind = classify(r.status);
        if (kind === "no") { ctx.m.noRetry = (ctx.m.noRetry || 0) + 1; break; }
        if (r.streamFrac >= 0.8) break;      // most of the answer already arrived
        const delay = backoff(sim.rng, attempt, r.status === "529" ? cfg.base * 5 : cfg.base,
                              cfg.cap, cfg.retryAfter ? r.retryAfter : 0, cfg.jitter);
        if (delay + serviceTime(req.cls) > req.deadlineAt - sim.now) break;
        if (cfg.budget > 0 && !ctx.budget.take()) break;
        if (cfg.budget === 0) break;
        yield sim.timeout(delay);
      }
      return last || { ok: false, status: "error", stoppedAt: "provider" };
    },
  };

  /* ═══════════════════════════════════════════════════ the driver ═══ */

  function bin(ctx) {
    const i = Math.min(ctx.m.bins.length - 1, Math.max(0, Math.floor(ctx.sim.now)));
    return ctx.m.bins[i];
  }

  /* How saturated the gateway's own resources are for this class: the worst of
   * the concurrency slots and the bulkhead (counting its queue, because a full
   * queue is the clearest sign you are past capacity). */
  Ctx.prototype.loadPressure = function (cls) {
    let worst = 0;
    const slot = this.slots[this.ccKey(cls)];
    if (slot) worst = Math.max(worst, slot.inUse / Math.max(1, slot.cap));
    const bh = this.bulkheads[cls.name];
    if (bh) {
      const used = (bh.inUse + bh.q.length) / Math.max(1, bh.cap + bh.maxQ);
      worst = Math.max(worst, bh.inUse / Math.max(1, bh.cap), used);
    }
    return Math.min(1, worst);
  };

  function* runCall(ctx, cls, deadlineAt) {
    const req = { cls, started: ctx.sim.now, deadlineAt, attempt: 1, reservation: null };
    ctx.m.byClass[cls.name].offered++;
    bin(ctx).offered++;

    const chain = ctx.chain;
    function* invoke(i) {
      if (i >= chain.length) return yield* doCall(ctx, req);
      const g = GATE[chain[i]];
      if (!g) return yield* invoke(i + 1);
      return yield* g(ctx, req, () => invoke(i + 1));
    }

    const r = yield* invoke(0);
    const cm = ctx.m.byClass[cls.name], b = bin(ctx);
    if (r.ok && !r.degraded) {
      cm.ok++; b.good++; ctx.funnel.served++;
      if (r.latency) { cm.lat.push(r.latency); b.lat.push(r.latency); }
      if (r.ttft) cm.ttft.push(r.ttft);
    } else if (r.ok && r.degraded) {
      cm.fallback++; b.good++;
      if (r.latency) cm.lat.push(r.latency);
    } else if (r.status === "shed" || r.status === "quota" || r.status === "open") {
      cm.shed++; b.shed++;
    } else {
      cm.err++; b.err++; ctx.funnel.failed++;
    }
    return r;
  }

  function* turnPipeline(ctx) {
    const sim = ctx.sim, rng = sim.rng;
    ctx.m.turns++;
    let degraded = false;

    let r = yield* runCall(ctx, BY_NAME.guard, sim.now + BY_NAME.guard.deadline);
    if (!r.ok || r.degraded) degraded = true;

    if (rng.random() < BY_NAME.rewrite.perTurn) {
      r = yield* runCall(ctx, BY_NAME.rewrite, sim.now + BY_NAME.rewrite.deadline);
      if (!r.ok || r.degraded) degraded = true;
    }

    yield sim.timeout(rng.lognorm(-3, 0.4));   // vector retrieval; not an LLM call

    let produced = false;
    if (rng.random() < BY_NAME.think.perTurn) {
      r = yield* runCall(ctx, BY_NAME.think, sim.now + BY_NAME.think.deadline);
      if (r.ok && !r.degraded) produced = true; else degraded = true;
    }
    if (!produced) {
      r = yield* runCall(ctx, BY_NAME.answer, sim.now + BY_NAME.answer.deadline);
      produced = r.ok && !r.degraded;
    }

    if (produced) { ctx.m.complete++; if (degraded) ctx.m.degraded++; }
    else ctx.m.failed++;

    if (rng.random() < BY_NAME.title.perTurn) {
      sim.process((function* () { yield* runCall(ctx, BY_NAME.title, sim.now + BY_NAME.title.deadline); })());
    }
  }

  function* arrivals(ctx) {
    const sim = ctx.sim, sc = ctx.scenario;
    while (sim.now < sc.duration) {
      const rate = rateAt(sc, sim.now);
      yield sim.timeout(sim.rng.expo(Math.max(0.01, rate)));
      if (sim.now >= sc.duration) break;
      sim.process(turnPipeline(ctx));
    }
  }

  function rateAt(sc, t) {
    if (sc.shape === "ramp") return sc.turnsRps * (0.15 + 1.7 * (t / sc.duration));
    if (sc.shape === "spike") {
      const inSpike = t >= sc.duration * 0.35 && t <= sc.duration * 0.6;
      return inSpike ? sc.turnsRps * sc.spikeMultiple : sc.turnsRps;
    }
    return sc.turnsRps;
  }

  /* per-second sampling, for the timeline charts */
  function* sampler(ctx) {
    const sim = ctx.sim;
    while (sim.now <= ctx.scenario.duration) {
      const b = bin(ctx);
      let inFlight = 0, limit = 0, n = 0, open = 0;
      for (const k in ctx.slots) { inFlight += ctx.slots[k].inUse; limit += ctx.slots[k].cap; n++; }
      for (const k in ctx.breakers) if (ctx.breakers[k].state !== "closed") open++;
      b.inFlight = inFlight;
      b.limit = n ? limit : 0;
      b.open = open;
      b.pressure = ctx.quota ? ctx.quota.pressure("sonnet") : 0;
      yield sim.timeout(1);
    }
  }

  /* ═════════════════════════════════════════════════════════ run() ═══ */

  function run(opts) {
    const scenario = Object.assign({
      turnsRps: 12, duration: 60, seed: 7, shape: "steady", spikeMultiple: 4,
      globalTimeout: 30,
      incident: { enabled: false, model: "sonnet", start: 20, end: 45, scale: 0.15,
                  errorRate: 0.45, quotaScale: 1 },
    }, opts.scenario || {});
    const params = opts.params || defaultParams();
    const chain = (opts.chain || DEFAULT_CHAIN).filter((k) => PATTERN_BY_KEY[k]);

    const sim = new Sim(scenario.seed);
    const provider = new Provider(sim);
    const ctx = new Ctx(sim, provider, chain, params, scenario);

    ctx.say("info", "chain: " + (chain.length ? chain.map((k) => PATTERN_BY_KEY[k].short).join(" → ") : "(none)") + " → provider");
    ctx.say("info", "offered " + scenario.turnsRps.toFixed(1) + " turns/s vs a fixture that sustains " + sustainableTurnsRps().toFixed(1));

    if (scenario.incident.enabled) {
      const inc = scenario.incident;
      var q = inc.quotaScale === undefined ? 1 : inc.quotaScale;
      sim.at(inc.start, () => {
        provider.setHealth(inc.model, inc.scale, inc.errorRate, q);
        ctx.say("incident", "incident on " + inc.model + " — fleet at " +
          Math.round(inc.scale * 100) + "%, error rate " + Math.round(inc.errorRate * 100) + "%" +
          (q < 1 ? ", quota cut to " + Math.round(q * 100) + "%" : ""));
      });
      sim.at(inc.end, () => {
        provider.setHealth(inc.model, 1, 0.002, 1);
        ctx.say("incident", "provider recovers");
      });
    }

    // narrate breaker transitions as they happen
    const watch = () => {
      for (const k in ctx.breakers) {
        const cb = ctx.breakers[k];
        const seen = cb._seen || 0;
        for (let i = seen; i < cb.transitions.length; i++) {
          const [t, s] = cb.transitions[i];
          ctx.say(s === "open" ? "breaker" : "recover",
            "breaker[" + k + "] → " + s.toUpperCase() +
            (s === "open" && cb.tripRatio !== undefined
              ? " (failure ratio " + (cb.tripRatio * 100).toFixed(0) + "%)" : ""));
        }
        cb._seen = cb.transitions.length;
      }
      if (sim.now < scenario.duration + 60) sim.at(0.25, watch);
    };
    sim.at(0.25, watch);

    sim.process(arrivals(ctx));
    sim.process(sampler(ctx));
    sim.run(scenario.duration + 240);        // drain in-flight work

    return summarise(ctx);
  }

  function pct(arr, q) {
    if (!arr.length) return 0;
    const a = arr.slice().sort((x, y) => x - y);
    return a[Math.min(a.length - 1, Math.floor(q * a.length))];
  }

  function summarise(ctx) {
    const m = ctx.m, sc = ctx.scenario, t = ctx.provider.totals();
    const perClass = CLASSES.map((c) => {
      const s = m.byClass[c.name];
      return {
        name: c.name, model: c.model, crit: CRIT_NAME[c.crit], colour: null,
        offered: s.offered, ok: s.ok, fallback: s.fallback, shed: s.shed, err: s.err,
        served: s.offered ? s.ok / s.offered : 0,
        goodput: s.ok / sc.duration,
        p50: pct(s.lat, 0.5), p95: pct(s.lat, 0.95), p99: pct(s.lat, 0.99),
        ttft50: pct(s.ttft, 0.5),
      };
    });
    const bins = m.bins.map((b, i) => ({
      t: i, offered: b.offered, good: b.good, shed: b.shed, err: b.err,
      retries: b.retries, r429: b.r429, r529: b.r529, r500: b.r500,
      p95: pct(b.lat, 0.95), p50: pct(b.lat, 0.5),
      inFlight: b.inFlight, limit: b.limit, pressure: b.pressure, open: b.open,
    }));
    const breakerBands = [];
    for (const k in ctx.breakers) {
      const tr = ctx.breakers[k].transitions;
      let openAt = null;
      tr.forEach(([time, s]) => {
        if (s === "open" && openAt === null) openAt = time;
        else if (s !== "open" && openAt !== null) { breakerBands.push({ cls: k, from: openAt, to: time }); openAt = null; }
      });
      if (openAt !== null) breakerBands.push({ cls: k, from: openAt, to: sc.duration });
    }
    return {
      scenario: sc, chain: ctx.chain, params: ctx.p,
      turns: { offered: m.turns, complete: m.complete, degraded: m.degraded, failed: m.failed,
               completion: m.turns ? m.complete / m.turns : 0 },
      perClass, bins, funnel: ctx.funnel, log: ctx.log, breakerBands,
      provider: t,
      spend: { useful: m.costUseful, wasted: m.costWasted,
               wastedShare: (m.costUseful + m.costWasted) ? m.costWasted / (m.costUseful + m.costWasted) : 0 },
      attempts: m.attempts, retries: m.retries,
      retryRatio: m.attempts ? m.retries / m.attempts : 0,
      deadlineLosses: ctx.deadlineLosses,
      goodput: perClass.reduce((a, c) => a + c.ok, 0) / sc.duration,
      offeredRps: perClass.reduce((a, c) => a + c.offered, 0) / sc.duration,
      breakerTrips: Object.keys(ctx.breakers).reduce((a, k) => a + ctx.breakers[k].trips, 0),
      sustainable: sustainableTurnsRps(),
    };
  }

  /* ══════════════════════════════════ isolated single-pattern demos ═══ */

  const DEMOS = {
    /* Breaker: a healthy dependency that rate-limits 30% of calls, run with
     * the 429 flag both ways. This is the highest-value line in the lab. */
    breaker(p) {
      const out = { series: [], note: "" };
      [["counts 429 as failure", true, "#ef4444"], ["429 excluded (correct)", false, "#22c55e"]]
        .forEach(([label, flag, colour]) => {
          const sim = new Sim(3);
          const cb = new Breaker(sim, Object.assign({}, p, { counts429: flag }));
          const data = [], refused = [];
          let ref = 0;
          for (let i = 0; i < 300; i++) {
            sim.now = i * 0.2;
            if (!cb.allow()) { ref++; }
            else cb.record(i % 10 < 3 ? "429" : "ok");
            data.push(cb.state === "open" ? 1 : cb.state === "half" ? 0.5 : 0);
            refused.push(ref);
          }
          out.series.push({ label, colour, state: data, refused, trips: cb.trips, ref });
        });
      out.note = out.series[0].ref + " of 300 calls refused with the flag on, " +
                 out.series[1].ref + " with it off — against an API that was answering 70% of them.";
      return out;
    },

    /* Adaptive concurrency against a capacity step change. */
    concurrency(p) {
      const algos = [["fixed", "#94a3b8"], ["aimd", "#f59e0b"], ["gradient", "#3b82f6"]];
      const N = 600, series = [], capacity = [];
      for (let i = 0; i < N; i++) capacity.push(i > 200 && i < 400 ? 35 : 100);
      algos.forEach(([algo, colour]) => {
        const lim = new Limiter(algo, 100);
        const rng = new RNG(11);
        const data = [];
        for (let i = 0; i < N; i++) {
          const cap = capacity[i];
          const inFlight = Math.min(lim.limit, cap * 1.4);
          // latency rises as we push past the available capacity
          const load = Math.min(0.985, inFlight / cap);
          const rtt = 0.1 * (1 + Math.pow(load, 8) / (1 - load)) * rng.lognorm(0, 0.12);
          if (inFlight > cap * 1.25 && rng.random() < 0.25) lim.failure("500");
          else lim.success(rtt, inFlight);
          data.push(lim.value);
        }
        series.push({ label: algo, colour, data });
      });
      return { series, capacity, note: "The step at t=200 is a noisy neighbour arriving. A fixed limit is wrong in both directions; the gradient controller tracks, AIMD sawtooths below." };
    },

    /* Bulkhead: FIFO vs LIFO through a burst. */
    bulkhead(p) {
      const out = { series: [], note: "" };
      [["fifo", "#94a3b8"], ["lifo", "#f59e0b"]].forEach(([policy, colour]) => {
        const sim = new Sim(5);
        const bh = new Bulkhead(sim, Object.assign({}, p, { policy }), 10);
        const served = [], dropped = [], depth = [];
        let ok = 0, drop = 0;
        const hold = 0.25, deadline = 1.5;
        for (let step = 0; step < 400; step++) {
          sim.now = step * 0.02;
          const burst = step > 100 && step < 260 ? 5 : 1;
          for (let k = 0; k < burst; k++) {
            const e = bh.acquire(sim.now + deadline);
            e.add((ev) => {
              if (ev.value === true) { ok++; sim.at(hold, () => bh.release()); }
              else drop++;
            });
          }
          sim.run(sim.now + 0.02);
          served.push(ok); dropped.push(drop); depth.push(bh.q.length);
        }
        out.series.push({ label: policy.toUpperCase(), colour, served, dropped, depth, ok, drop });
      });
      out.note = "Through the same burst, LIFO serves " + out.series[1].ok + " within deadline vs FIFO's " +
                 out.series[0].ok + ". FIFO always works on the request that has already waited longest — the one most likely to be dead on arrival.";
      return out;
    },

    /* Retries: amplification, and what jitter does to arrival correlation. */
    retry(p) {
      const rng = new RNG(9);
      const hist = { full: new Array(20).fill(0), none: new Array(20).fill(0), equal: new Array(20).fill(0) };
      ["full", "none", "equal"].forEach((j) => {
        for (let i = 0; i < 4000; i++) {
          const d = backoff(rng, 3, p.base, p.cap, 0, j);
          const b = Math.min(19, Math.floor((d / p.cap) * 20));
          hist[j][b]++;
        }
      });
      // amplification under a failure rate, with and without a budget
      const amp = [];
      for (let fr = 0; fr <= 100; fr += 2) {
        const f = fr / 100;
        let attempts = 0;
        const budget = new RetryBudget(p.budget);
        for (let i = 0; i < 1000; i++) {
          let a = 1; attempts++;
          while (a < p.attempts && rng.random() < f) {
            if (p.budget > 0 && !budget.take()) break;
            if (p.budget === 0) break;
            attempts++; a++;
          }
          if (rng.random() >= f) budget.ok();
        }
        amp.push({ f, ratio: attempts / 1000 });
      }
      return { hist, amp, note: "With no jitter every client that failed in the same second retries in the same later second. Full jitter spreads them across the whole window. The budget line flattens as failures become correlated — which is exactly when extra attempts cannot help." };
    },

    /* Client rate limiter: reservation vs reconciliation on the OTPM bucket. */
    ratelimit(p) {
      const out = { series: [], note: "" };
      [["reconciled", true, "#14b8a6"], ["reserve only", false, "#ef4444"]].forEach(([label, rec, colour]) => {
        const sim = new Sim(2);
        const q = new Quota(sim, Object.assign({}, p, { reconcile: rec, instances: 1 }));
        const level = [], servedArr = [];
        let served = 0;
        const cls = BY_NAME.answer;
        for (let step = 0; step < 600; step++) {
          sim.now = step * 0.1;
          const res = q.reserve(cls.model, cls.inTok[0], cls.maxTokens);
          if (res) { served++; q.reconcile(res, cls.outTok[0]); }
          level.push(q.b.sonnet[2].level(sim.now) / MODELS.sonnet.otpm);
          servedArr.push(served);
        }
        out.series.push({ label, colour, level, served: servedArr, total: served });
      });
      out.note = "Reserving max_tokens and never handing the slack back throttles you to " +
        out.series[1].total + " calls where reconciliation allows " + out.series[0].total +
        ". It is one line in the client, and it costs you most of the quota you are paying for.";
      return out;
    },

    /* Admission: the criticality staircase. */
    admission(p) {
      const rows = CLASSES.map((c) => {
        const admitted = [];
        for (let i = 0; i <= 100; i++) {
          const pressure = i / 100;
          let floor = CRIT.SHEDDABLE;
          if (pressure >= p.l3) floor = CRIT.CRITICAL_PLUS;
          else if (pressure >= p.l2) floor = CRIT.CRITICAL;
          else if (pressure >= p.l1) floor = CRIT.SHEDDABLE_PLUS;
          admitted.push(c.crit <= floor ? 1 : 0);
        }
        return { name: c.name, crit: CRIT_NAME[c.crit], admitted };
      });
      return { rows, note: "Read it left to right as load climbs: titles stop, then query rewriting, then the thinking tier. The answer path is defended to the last — but note that 'never shed by policy' is not 'always served'. Capacity still binds." };
    },

    cache(p) {
      const pts = [];
      for (let i = 0; i <= 90; i += 2) {
        const hit = i / 100;
        pts.push({ hit, turns: sustainableTurnsRps() / Math.max(0.05, 1 - hit) });
      }
      return { pts, note: "A whole-response cache is the only lever that removes a call rather than shrinking it — the sustainable turn rate scales as 1/(1-hit). It is also the only one on this page that is not a resilience pattern." };
    },

    fallback(p) {
      return { note: "Fallback has no dynamics of its own — it is a policy table. What matters is that every class has an entry and that none of them is 'make something up'." ,
        rows: CLASSES.map((c) => ({ name: c.name, crit: CRIT_NAME[c.crit], fallback: c.fallback })) };
    },
  };

  /* ═══════════════════════════════════════════════════════════ exports ═══ */

  root.RSIM = {
    Sim, Ev, Resource, TokenBucket, RNG, Heap,
    MODELS, CLASSES, BY_NAME, CRIT, CRIT_NAME,
    PATTERNS, PATTERN_BY_KEY, DEFAULT_CHAIN, defaultParams,
    Breaker, Limiter, Bulkhead, Quota, RetryBudget, backoff, classify,
    binding, headroom, sustainableTurnsRps, serviceTime, worstMeter, cost,
    run, DEMOS, pct,
  };
})(typeof globalThis !== "undefined" ? globalThis : this);

if (typeof module !== "undefined" && module.exports) module.exports = globalThis.RSIM;
