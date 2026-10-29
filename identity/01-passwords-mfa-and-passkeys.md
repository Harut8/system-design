# 01 — Authenticating humans: passwords, MFA and passkeys

> **Prerequisites:** [`00-mental-models.md`](00-mental-models.md).
>
> **Feeds into:** [`02`](02-sessions-cookies-and-browser-security.md) (what happens after login succeeds),
> [`08`](08-storing-credentials-and-tokens-at-rest.md) (how password hashes, TOTP seeds and passkey public keys
> are stored), [`16`](18-designing-an-identity-platform.md) (account-takeover defense at scale).
>
> **THESIS:** the strength of a login is set by its weakest *phishable* step and by its *recovery* flow, not by its
> strongest factor. A password plus SMS code is phished by a reverse proxy in real time; a passkey is not, because
> the browser binds the signature to the real origin. So the 2026 default is: **passkeys first, password plus a
> phishing-resistant or at least app-based second factor as fallback, and an account-recovery flow that is no
> weaker than the login it replaces.**

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [Authentication factors and assurance levels](#1-authentication-factors-and-assurance-levels)
2. [Passwords done right](#2-passwords-done-right)
3. [One-time codes: TOTP, HOTP, SMS, email, push](#3-one-time-codes-totp-hotp-sms-email-push)
4. [Phishing and why most MFA does not stop it](#4-phishing-and-why-most-mfa-does-not-stop-it)
5. [WebAuthn, FIDO2 and passkeys](#5-webauthn-fido2-and-passkeys)
6. [Magic links and social login](#6-magic-links-and-social-login)
7. [Account recovery: the real front door](#7-account-recovery-the-real-front-door)
8. [Account-takeover defenses at the login endpoint](#8-account-takeover-defenses-at-the-login-endpoint)
9. [Code: password verify, TOTP, WebAuthn ceremony shape](#9-code-password-verify-totp-webauthn-ceremony-shape)
10. [Anti-patterns](#10-anti-patterns)
11. [Interview questions](#11-interview-questions)
12. [Sources](#sources)

---

## Start here — the whole chapter in plain words

Logging in is proving you are the account owner. You can prove it with something you **know** (password),
something you **have** (phone, security key), or something you **are** (fingerprint, which in practice only
unlocks something you have). Using two different kinds is **multi-factor authentication** (MFA).

Most MFA still fails against a fake login page: you type your password *and* your six-digit code into
`examp1e.com`, and the attacker's server replays both to the real site within seconds. **Passkeys** fix this.
A passkey is a key pair: the private key stays on your device, the site keeps only the public key, and your
browser will only use the key for the exact site it was created on. A fake site gets nothing usable.

The other half of the story is **recovery**. If "forgot password" emails a link that logs you straight in, your
login is exactly as strong as your email account, no matter how good your MFA is.

---

## 1. Authentication factors and assurance levels

NIST SP 800-63B-4 (final, 2025) defines **Authenticator Assurance Levels**:

| Level | Requirement (simplified) | Typical realization |
|---|---|---|
| AAL1 | Single factor | Password, or a passkey on its own |
| AAL2 | Two factors; a phishing-resistant option **must be offered** | Password + TOTP, or a passkey (the passkey's device unlock is the second factor) |
| AAL3 | Phishing-resistant, hardware-backed, non-exportable key | Device-bound FIDO2 security key or platform authenticator with a non-exportable key |

Revision 4 explicitly covers **syncable authenticators** (passkeys that sync through a cloud account such as
iCloud Keychain or Google Password Manager). They can reach AAL2. Because the key is exportable to the sync
fabric, they do not meet AAL3, which needs a non-exportable key.

Factor taxonomy and what each is good for:

| Factor | Kind | Phishing-resistant | Main weakness |
|---|---|---|---|
| Password | Know | No | Reuse, phishing, stuffing |
| SMS / voice OTP | Have (phone number) | No | SIM swap, SS7 interception, real-time phishing |
| Email OTP / link | Have (mailbox) | No | Mailbox compromise, phishing |
| TOTP app | Have (seed) | No | Real-time phishing, seed theft from server |
| Push approve | Have (phone) | No (unless number-matching + context) | Push fatigue / MFA bombing |
| Passkey (synced) | Have + biometric/PIN unlock | **Yes** | Sync account compromise |
| Security key (device-bound) | Have | **Yes** | Loss; needs a backup key |

---

## 2. Passwords done right

The NIST SP 800-63B-4 rules, which reversed a decade of folklore:

- **Length, not composition.** Minimum 15 characters when the password is the *only* factor; minimum 8 when used
  as part of MFA. Allow at least 64. **Do not** impose composition rules ("one uppercase, one symbol").
- **Blocklist.** Reject passwords found in breach corpora, dictionary words, and context words (service name,
  username). The Have I Been Pwned *Pwned Passwords* range API checks breaches with k-anonymity: you send the first
  5 hex characters of the SHA-1 and match the suffix locally, so the password never leaves your server.
- **No periodic expiry.** Force a change only on evidence of compromise. Forced rotation produces
  `Spring2026!` → `Summer2026!`.
- **No hints, no knowledge-based questions** ("first pet").
- **Allow paste and password managers.** Blocking paste lowers security by discouraging generated passwords.
- **Accept all printable ASCII, spaces and Unicode.** Normalize Unicode (NFKC is the usual choice) before hashing
  so the same visible password hashes the same way on every device.
- **Rate-limit** failed attempts per account and per source (§8).

Storage (detail in [ch 08](08-storing-credentials-and-tokens-at-rest.md)): a **slow, salted, memory-hard** hash.
Argon2id first (OWASP minimum: 19 MiB memory, 2 iterations, parallelism 1), scrypt second, bcrypt for legacy
(cost ≥ 10, and mind its 72-byte input limit), PBKDF2-HMAC-SHA256 with 600,000 iterations where FIPS mandates it.
Never MD5, SHA-1 or SHA-256 alone; a GPU computes billions of those per second.

---

## 3. One-time codes: TOTP, HOTP, SMS, email, push

**TOTP (RFC 6238)** = HOTP (RFC 4226) with the counter replaced by `floor(unix_time / 30)`.

```
code = Truncate( HMAC-SHA1(secret, counter) ) mod 10^6
```

Server-side rules that are easy to get wrong:

- Accept a **window** of ±1 step for clock skew, no more.
- **Prevent replay**: store the last accepted time step per user and reject codes at or below it. Without this,
  a shoulder-surfed code works twice within 30–90 seconds.
- **Rate-limit** guesses: a 6-digit code has 10⁶ values; with a ±1 window, three are valid at any time, so
  unlimited guessing succeeds in ~333,000 tries.
- The **seed is a secret the server must be able to read**, so it cannot be hashed. Encrypt it at rest
  (ch 08 §5). A database dump with plaintext TOTP seeds is a dump of everyone's second factor.
- Provide **recovery codes** at enrollment: 8–10 single-use random codes, hashed like passwords.

**SMS** is the weakest second factor in common use: SIM-swap fraud, SS7 interception, and it phishes as easily
as TOTP. NIST classes it "restricted". Keep it only as a fallback for consumers who have nothing else, and never
as the recovery channel for high-value accounts.

**Push approval** is vulnerable to **MFA fatigue** (the attacker triggers dozens of prompts until the user taps
"Approve"; this was the 2022 Uber breach vector). Mitigate with **number matching** (the login screen shows a
number the user must type on the phone), showing location and app context, and rate-limiting prompts.

---

## 4. Phishing and why most MFA does not stop it

An **adversary-in-the-middle (AitM)** phishing kit (Evilginx, Modlishka, and the commercial "phishing-as-a-
service" kits built on them) is a reverse proxy:

```
Victim ──▶ https://login.examp1e.com  (attacker proxy) ──▶ https://login.example.com (real)
            relays password ─────────────────────────────▶
            relays TOTP / push approval ─────────────────▶
            ◀───────────────── session cookie ───────────  (attacker keeps a copy)
```

The attacker never needs your factors again; they keep the **session cookie**. Every factor that is "a value the
user transfers" (password, OTP, push tap) is relayed. Only factors **bound to the origin** defeat it:

- **WebAuthn/passkeys**: the browser puts the origin it actually sees (`https://login.examp1e.com`) into the
  signed data; the real site rejects the mismatch.
- **Client certificates / device-bound credentials** on managed devices.

Post-login defenses still matter, because cookies get stolen by malware too: short session lifetimes for admin
roles, re-authentication for sensitive actions, and binding the session to the device (Device Bound Session
Credentials in Chrome; see ch 02 §8).

---

## 5. WebAuthn, FIDO2 and passkeys

**WebAuthn** (W3C, Level 3) is the browser API; **CTAP** is the protocol to external authenticators; together
they are **FIDO2**. A **passkey** is a WebAuthn **discoverable credential** (the authenticator stores the user
handle so no username is needed), usually synced.

### 5.1 Registration ceremony

```
Server                                    Browser / authenticator
  │  challenge (random ≥16 bytes),
  │  rp.id = "example.com", user.id (opaque, not email),
  │  pubKeyCredParams [ES256(-7), RS256(-257)],
  │  authenticatorSelection {residentKey: "required", userVerification: "preferred"}
  ├──────────────────────────────────────────▶ navigator.credentials.create()
  │                                            user unlocks (Face ID / PIN)
  │                                            authenticator creates key pair for rp.id
  │◀────────── attestationObject + clientDataJSON {type, challenge, origin}
  │  verify: challenge matches, origin allowed, rpIdHash = SHA-256("example.com"),
  │          flags UP (user present) / UV (user verified), alg allowed
  │  store: credential_id, public_key (COSE), sign_count, transports,
  │         backup_eligible (BE), backup_state (BS), user_id
```

### 5.2 Authentication ceremony

The server sends a fresh challenge; the authenticator signs `authenticatorData || SHA-256(clientDataJSON)`; the
server verifies with the stored public key, checks challenge, origin, `rpIdHash`, the UP/UV flags, and the
signature counter (a counter that goes backwards may indicate a cloned authenticator; synced passkeys often
report 0, so treat it as a signal, not a hard rule).

### 5.3 Why it resists phishing

1. The key is scoped to the **RP ID** (a registrable domain). The authenticator will not even offer a passkey for
   `example.com` on `examp1e.com`.
2. `clientDataJSON.origin` is filled in **by the browser**, not by page JavaScript, and is covered by the
   signature.
3. There is no shared secret on the server to steal: a database dump yields public keys.

### 5.4 Practical decisions

| Decision | Recommendation |
|---|---|
| `user.id` | Random opaque bytes (≤64), never the email (it is exposed to the authenticator and can leak) |
| `userVerification` | `required` if the passkey is the only factor; `preferred` otherwise |
| Attestation | `none` for consumer apps (privacy, simplicity); `direct`/`enterprise` only when you must restrict to approved hardware models |
| Autofill UI | Use conditional mediation (`mediation: "conditional"`) so passkeys appear in the username field's autofill |
| Multiple passkeys | Allow many per account; show them with names and last-used dates; allow deletion |
| Related domains | WebAuthn **Related Origin Requests** let `example.co.uk` use `example.com` passkeys via `/.well-known/webauthn` (ch 06) |
| Fallback | Keep a second passkey or a security key; recovery is §7 |
| Library | Use a maintained server library (e.g. `py_webauthn`, `SimpleWebAuthn`, `webauthn4j`); do not hand-parse CBOR |

---

## 6. Magic links and social login

**Magic links / email OTP.** Fine for low-risk consumer apps. Rules: single-use, short TTL (10–15 minutes),
bound to the browser that requested it (store a nonce in a cookie at request time and require it at click
time, or else the link logs in whoever clicks it, including email security scanners that prefetch links). Make
the landing page require a click (a POST), since scanners issue GETs.

**Social login** ("Sign in with Google/Apple/GitHub") is OIDC or OAuth (ch 04–05). Two traps:

- **Account linking by email.** If you merge a social identity into an existing account because the emails match,
  verify the provider asserts the email is verified (`email_verified: true`) **and** that this provider is
  authoritative for that domain. The 2023 "nOAuth" issue: some apps keyed Microsoft Entra users on the mutable,
  unverified `email` claim, letting an attacker set a victim's email in their own tenant and log in as the
  victim. Key on `(iss, sub)`, never on email.
- **GitHub OAuth is not OIDC.** It returns an access token, not an ID token; your app must call the user API and
  must treat the returned email list's `verified` flag carefully.

---

## 7. Account recovery: the real front door

Your effective security is `min(login, recovery, support desk)`. Real breaches through recovery:

- Help-desk social engineering resetting MFA (the 2023 MGM Resorts intrusion was attributed to a phone call to
  the IT help desk).
- SIM swap → SMS reset code → account.
- Email compromise → password reset link.

Design rules:

1. Recovery must **not be weaker than login**. If login needs a passkey, recovery needs a second registered
   passkey/security key, recovery codes, or an identity-verification process with a waiting period.
2. **Notify on every recovery** through all channels on file, and add a **cooling-off period** (e.g. 24–72
   hours for high-value accounts) during which the original owner can cancel.
3. After recovery: revoke all sessions and refresh tokens, require re-enrollment of MFA, and flag the account for
   heightened monitoring.
4. Reset tokens: random ≥128 bits, single-use, short TTL, **stored hashed** (ch 08 §4), invalidated when the
   password changes or another reset is issued.
5. Help-desk resets for workforce accounts: require a verified callback or manager attestation; log and alert.

---

## 8. Account-takeover defenses at the login endpoint

| Attack | What it looks like | Defense |
|---|---|---|
| Credential stuffing | Millions of leaked username/password pairs from many IPs, low attempts per account | Breached-password checks at login, bot detection, per-IP and per-ASN rate limits, MFA, passkeys |
| Password spraying | One common password across many accounts | Per-password-hash global counters, blocklist at set time |
| Brute force on one account | Many attempts on one account | Per-account exponential backoff; **avoid hard lockout** (it becomes a DoS lever); CAPTCHA after N failures |
| Account enumeration | Different responses/timings for "no such user" vs "wrong password" | Identical messages, constant-time path (hash a dummy password when the user does not exist) |
| Session hijack after login | Stolen cookie used elsewhere | Device binding, anomaly detection on IP/UA change, short admin sessions |
| MFA fatigue | Burst of push prompts | Number matching, prompt rate limits |

Throttling keys: combine **per account**, **per source IP / prefix**, and **global failure rate** sliding windows,
stored in Redis. See the rate-limiter design in [`solutions/api-gateway-rate-limiter-design.md`](../solutions/api-gateway-rate-limiter-design.md).

**Risk-based (adaptive) authentication**: score each login (new device, impossible travel, Tor exit, breached
password) and step up to stronger factors or block. Feed the score into session lifetime too.

---

## 9. Code: password verify, TOTP, WebAuthn ceremony shape

### 9.1 Password hashing and verification (argon2-cffi)

```python
import unicodedata
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError

# OWASP floor is m=19 MiB, t=2, p=1. Tune so one hash takes ~50-250 ms on your login hosts,
# and remember each concurrent login holds memory_cost KiB.
ph = PasswordHasher(time_cost=2, memory_cost=19 * 1024, parallelism=1)

# Precomputed so "unknown user" costs the same as "wrong password" (no enumeration via timing).
_DUMMY_HASH = ph.hash("dummy-password-for-timing")

def normalize(pw: str) -> str:
    return unicodedata.normalize("NFKC", pw)

def set_password(pw: str) -> str:
    if len(pw) < 15:                       # single-factor minimum per NIST SP 800-63B-4
        raise ValueError("password too short")
    # also: reject if in breached-password corpus (k-anonymity range API)
    return ph.hash(normalize(pw))

def verify_login(stored_hash: str | None, pw: str) -> tuple[bool, str | None]:
    """Returns (ok, new_hash_if_rehash_needed)."""
    try:
        ph.verify(stored_hash or _DUMMY_HASH, normalize(pw))
    except (VerifyMismatchError, InvalidHashError):
        return False, None
    if stored_hash is None:
        return False, None
    # Parameters are encoded in the hash string, so upgrades happen transparently at next login.
    return True, ph.hash(normalize(pw)) if ph.check_needs_rehash(stored_hash) else None
```

### 9.2 TOTP with replay protection

```python
import hmac, hashlib, struct, time

def hotp(secret: bytes, counter: int, digits: int = 6) -> str:
    mac = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % 10**digits
    return f"{code:0{digits}d}"

def verify_totp(secret: bytes, code: str, last_step: int, step: int = 30, window: int = 1):
    """Returns the accepted time step (persist it as last_step) or None."""
    now = int(time.time()) // step
    for t in range(now - window, now + window + 1):
        if t <= last_step:
            continue                                  # replay of an already-used code
        if hmac.compare_digest(hotp(secret, t), code):
            return t
    return None
```

The RFC 6238 test vector (`secret = b"12345678901234567890"`, time 59 → counter 1, 8 digits) yields `94287082`;
use it as a unit test.

### 9.3 WebAuthn: what the server stores and checks

```python
# Stored per credential (never a secret):
# credential_id (bytes, unique), user_id, public_key_cose (bytes), sign_count (int),
# transports (list), backup_eligible (bool), backup_state (bool), aaguid, created_at, last_used_at, nickname

# On authentication, a library call like py_webauthn's verify_authentication_response() checks:
#   challenge == the one issued for this session (single use, ~5 min TTL)
#   origin in allowed set, rpIdHash == sha256(rp_id)
#   UP flag set; UV flag set if required
#   signature valid over authenticatorData || sha256(clientDataJSON)
# Then update sign_count and last_used_at.
```

---

## 10. Anti-patterns

| Anti-pattern | Why it is wrong | Instead |
|---|---|---|
| Composition rules + 90-day expiry | Predictable patterns, sticky notes | Length + blocklist, no expiry |
| Fast hash (SHA-256) for passwords | Billions of guesses per second on a GPU | Argon2id / scrypt / bcrypt |
| "User not found" vs "wrong password" messages | Enumeration | One generic message, equal timing |
| Hard account lockout after 5 failures | Attacker can lock anyone out | Backoff + CAPTCHA + risk signals |
| SMS as the only second factor for admins | SIM swap, phishing | Security keys / passkeys |
| Reset link that logs you in and skips MFA | Recovery weaker than login | Reset password only; MFA still required, or recovery codes |
| TOTP seeds stored in plaintext | Database leak = second factor leak | Encrypt with a KMS-held key |
| Linking accounts on unverified email | Account takeover (nOAuth class) | Key on `(iss, sub)`; link only after logging in to both |
| Passkeys with `user.id = email` | Leaks PII to authenticators, breaks on email change | Random opaque user handle |

---

## 11. Interview questions

**Q1. Why does TOTP-based MFA not stop modern phishing, and what does?**
An AitM proxy relays the password and the code in real time and keeps the resulting session cookie. WebAuthn and
passkeys stop it because the browser signs over the real origin and the key is scoped to the RP ID; the fake
origin can't obtain a valid assertion.

**Q2. What changed in NIST's password guidance?**
Minimum 15 characters for single-factor passwords (8 inside MFA), at least 64 allowed, blocklist screening, no
composition rules, no periodic expiry, no security questions, allow paste.

**Q3. How do you make login not leak which usernames exist?**
Same error text and status, and the same work on both paths: run the password hash against a dummy hash when
the user is absent. Also apply the same rule to "forgot password" ("if an account exists, we sent an email") and
registration (send an email to the existing owner instead of saying "already registered").

**Q4. Design account recovery for an app whose login is passkey-only.**
Encourage multiple passkeys and a hardware key; offer recovery codes generated at enrollment and stored hashed;
otherwise, an identity-proofing flow with a notification to all channels and a cooling-off period, after which
all sessions are revoked and a new passkey must be enrolled.

---

## Sources

- NIST SP 800-63B-4, Authentication and Authenticator Management (2025): https://pages.nist.gov/800-63-4/sp800-63b.html
- W3C Web Authentication Level 3: https://www.w3.org/TR/webauthn-3/
- FIDO Alliance, passkeys: https://fidoalliance.org/passkeys/
- RFC 4226 (HOTP): https://datatracker.ietf.org/doc/html/rfc4226 ; RFC 6238 (TOTP): https://datatracker.ietf.org/doc/html/rfc6238
- OWASP Password Storage Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
- OWASP Authentication Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html
- RFC 9106, Argon2: https://datatracker.ietf.org/doc/html/rfc9106
- Have I Been Pwned, Pwned Passwords range API: https://haveibeenpwned.com/API/v3#PwnedPasswords
- Descope, "nOAuth: How Microsoft OAuth misconfiguration can lead to full account takeover" (2023): https://www.descope.com/blog/post/noauth
