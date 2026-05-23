# Supply Chain Security: Sigstore, SBOMs, SLSA, and Admission-Time Verification

Every other chapter in this folder is about what Kubernetes does once a container image is on the node. This one is about everything that happens *before* — the chain of custody from a developer's `git push` to a running pod, and the cryptographic machinery that lets a cluster refuse to run anything whose provenance it cannot prove. If chapter 02 explained what an image is and where it lives, and chapter 06 explained admission control as a generic policy gate, this chapter explains how those two combine into a system that can answer "was this image built by my CI, from my source code, with no human in the loop, on a tamper-resistant builder, in the last 24 hours?" — and refuse the pod if the answer is no.

The chapter is structured around four artifacts (source → provenance → SBOM → signed manifest) and one verifier (admission). We start with the threat model, then build Sigstore from primitives (cosign, Fulcio, Rekor, TUF), then layer in-toto attestations on top, then SLSA on top of that, then plug the whole thing into a Kubernetes admission controller. Every section has source paths into `sigstore/cosign`, `sigstore/policy-controller`, `kyverno/kyverno`, `slsa-framework/slsa`, and `in-toto/attestation` so you can read the spec alongside the prose.

This is the chapter that turns "we sign our images" from a meaningless sentence into a measurable security control with a defined adversary, a defined coverage gap, and a defined cost.

---

## Table of Contents

1. [The Supply Chain Threat Model](#1-the-supply-chain-threat-model)
2. [The Four Artifacts of a Secure Supply Chain](#2-the-four-artifacts-of-a-secure-supply-chain)
3. [Sigstore: Architecture and Components](#3-sigstore-architecture-and-components)
4. [The Keyless OIDC Signing Flow, Step by Step](#4-the-keyless-oidc-signing-flow-step-by-step)
5. [What Gets Signed: Manifest Digest vs Tag](#5-what-gets-signed-manifest-digest-vs-tag)
6. [cosign verify: Anatomy of a Verification](#6-cosign-verify-anatomy-of-a-verification)
7. [The cert-identity and cert-oidc-issuer Policy Knobs](#7-the-cert-identity-and-cert-oidc-issuer-policy-knobs)
8. [Long-Lived Key Signing (the Alternative to Keyless)](#8-long-lived-key-signing-the-alternative-to-keyless)
9. [SBOMs: SPDX vs CycloneDX](#9-sboms-spdx-vs-cyclonedx)
10. [SBOM Generation Tooling: Syft, Trivy, cyclonedx-cli](#10-sbom-generation-tooling-syft-trivy-cyclonedx-cli)
11. [Signing the SBOM: cosign attest](#11-signing-the-sbom-cosign-attest)
12. [In-Toto Attestations: Subject + Predicate](#12-in-toto-attestations-subject--predicate)
13. [Verifying Attestations at Admission](#13-verifying-attestations-at-admission)
14. [SLSA: Supply-Chain Levels for Software Artifacts](#14-slsa-supply-chain-levels-for-software-artifacts)
15. [SLSA Build Track L1–L4 Requirements](#15-slsa-build-track-l1l4-requirements)
16. [In-Toto: Layouts, Links, and Functionaries](#16-in-toto-layouts-links-and-functionaries)
17. [SLSA Provenance v1: The buildDefinition + runDetails Schema](#17-slsa-provenance-v1-the-builddefinition--rundetails-schema)
18. [GitHub Actions Signing: The Reference Implementation](#18-github-actions-signing-the-reference-implementation)
19. [GitLab CI, Tekton Chains, Buildkite, and Kubernetes ServiceAccount Signing](#19-gitlab-ci-tekton-chains-buildkite-and-kubernetes-serviceaccount-signing)
20. [Registry Support for OCI Artifacts and the Referrers API](#20-registry-support-for-oci-artifacts-and-the-referrers-api)
21. [Admission-Time Verification: policy-controller, Kyverno, Connaisseur, Ratify](#21-admission-time-verification-policy-controller-kyverno-connaisseur-ratify)
22. [ClusterImagePolicy YAML in Depth](#22-clusterimagepolicy-yaml-in-depth)
23. [Kyverno verifyImages YAML in Depth](#23-kyverno-verifyimages-yaml-in-depth)
24. [Tag-to-Digest Mutation at Admission](#24-tag-to-digest-mutation-at-admission)
25. [Vulnerability Scanning and Admission Gating](#25-vulnerability-scanning-and-admission-gating)
26. [VEX: Vulnerability EXchange](#26-vex-vulnerability-exchange)
27. [Base Image Hygiene: Distroless, Chainguard, Scratch](#27-base-image-hygiene-distroless-chainguard-scratch)
28. [The cosign UX Gap](#28-the-cosign-ux-gap)
29. [Air-Gapped Supply Chain](#29-air-gapped-supply-chain)
30. [Multi-Arch Signing](#30-multi-arch-signing)
31. [Provenance Pinning Across the SDLC](#31-provenance-pinning-across-the-sdlc)
32. [The Signing Ladder: L0 to L4](#32-the-signing-ladder-l0-to-l4)
33. [Performance of Admission Verification](#33-performance-of-admission-verification)
34. [Observability: Reports, Rekor Monitoring, CI Metrics](#34-observability-reports-rekor-monitoring-ci-metrics)
35. [Pitfalls](#35-pitfalls)
36. [TL;DR](#36-tldr)

---

## 1. The Supply Chain Threat Model

A signature without a threat model is theatre. Before any cryptography, name what you are defending against.

A modern container image is the output of a long chain of decisions, each by a different principal, each made before the image ever reaches your registry. The threats follow that chain.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  SOURCE                                                                     │
│  developer ──git push──► forge (GitHub/GitLab)                              │
│     ▲                            │                                          │
│     │                            │ webhook                                  │
│     └── threat: stolen creds     ▼                                          │
│         malicious commit    ┌─────────────┐                                 │
│         compromised laptop  │  CI runner  │                                 │
│                             │  (GH Actions│                                 │
│                             │   GitLab,   │                                 │
│                             │   Tekton)   │                                 │
│                             └──────┬──────┘                                 │
│                                    │                                        │
│  ── threat: malicious base image, typosquatted dep,                          │
│             compromised build dependency, tampered build env ───►            │
│                                    │                                        │
│                                    ▼                                        │
│                             ┌─────────────┐                                 │
│                             │  REGISTRY   │ ◄── threat: tampered storage,   │
│                             │   (OCI)     │     compromised admin,           │
│                             └──────┬──────┘     poisoned cache               │
│                                    │                                        │
│  ── threat: MITM during pull, registry impersonation,                        │
│             pull-through-cache poisoning ───────────────────►                │
│                                    │                                        │
│                                    ▼                                        │
│                              ┌────────────┐                                 │
│                              │  KUBELET   │                                 │
│                              │  + admission│                                │
│                              └────────────┘                                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

The five canonical attacks the rest of this chapter exists to mitigate:

1. **Solarwinds-style upstream compromise.** An attacker who gains access to the build system (not the source repo, not the registry) injects a backdoor into a release that the source code does not contain. The git history is clean. The published artifact is not. Defended by: provenance attestations binding the artifact to the source commit *and* the build environment, plus a transparency log that catches a stale build environment.

2. **Malicious base image.** Your Dockerfile says `FROM debian:bookworm`. The publisher of `debian:bookworm` (or someone who compromised the publisher) ships a backdoored image. Defended by: signing of base images by their publishers, verification of those signatures at build time (not just at deploy time), and policy that requires base images to be signed by an allow-list of identities.

3. **Typosquatted dependency.** Your build pulls `requests` from PyPI. An attacker publishes `requets`, you have a typo, your image now contains malware. The image is correctly signed by *you*, by your CI, with full provenance — and it still ships malware. Defended by: SBOMs (you can audit the dependency list after the fact), vulnerability scanning, source-side allowlists, and dependency pinning with lockfiles.

4. **Registry tampering.** An attacker with write access to the registry replaces `myorg/api@sha256:abc...` with a malicious image. Defended by: content-addressability (the digest can't change without the manifest changing) plus signing (the signature is over the digest, so a swapped image fails verification).

5. **MITM during pull.** An attacker intercepts the TLS connection to the registry and serves a malicious image. Defended by: TLS to the registry, content-addressability after the manifest is fetched (the kubelet hashes the bytes), and signature verification *before* the kubelet pulls (admission only allows pulls of digests for which a valid signature exists in the registry).

What signing **does not** prevent:

- A bug in your code. The signature attests "this came from your CI"; it does not attest "this code is correct".
- A compromised developer. If alice's laptop is compromised and she pushes a backdoor through normal review, the supply chain stamps it as legitimate. Code review and branch protection are upstream of signing, not replaced by it.
- A compromised CI secret. If your `id-token: write` workflow has a vulnerable step that an attacker can hijack, the attacker now signs malicious images with your identity. Provenance helps post-incident (the attacker's workflow run is logged) but does not stop the deploy.
- A compromised admission webhook. The webhook is the verifier; if you compromise it, you bypass all of this. This is why the webhook lives in `kube-system` with hardened RBAC and the webhook configuration itself is policy-protected (see §35).

Everything below is a mechanism to make attacks 1–5 expensive and detectable. None of it is a mechanism to make all software safe.

---

## 2. The Four Artifacts of a Secure Supply Chain

A secure supply chain in practice is not a single signature. It is four artifacts, each produced by a different stage, each binding the next:

```
┌─────────────────────────────────────────────────────────────────────────┐
│  1. SOURCE                                                              │
│     git commit sha:    abc123...                                        │
│     signed by:         developer GPG / sigstore gitsign                 │
│     stored in:         git host                                         │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ build trigger
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  2. BUILD PROVENANCE                                                    │
│     in-toto Statement:                                                  │
│       subject: { name: ghcr.io/me/api, digest: { sha256: ... } }        │
│       predicateType: https://slsa.dev/provenance/v1                     │
│       predicate:                                                        │
│         buildDefinition:                                                │
│           buildType:  https://actions.github.io/buildtypes/workflow/v1  │
│           externalParameters: { workflow: build.yml, ref: refs/heads/main}│
│           resolvedDependencies: [ sourceUri, baseImageDigest, ... ]     │
│         runDetails:                                                     │
│           builder.id: https://github.com/actions/runner/...             │
│           metadata: { invocationId, startedOn, finishedOn }             │
│     signed by:  Fulcio cert bound to OIDC subject (github workflow)     │
│     stored in:  Rekor log AND as OCI artifact next to image             │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  3. SBOM                                                                │
│     CycloneDX or SPDX document:                                         │
│       components: [ { name, version, purl, hash, license }, ... ]       │
│       dependencies: [ { ref, dependsOn: [...] } ]                       │
│     signed by:  same Fulcio identity (as a separate attestation)        │
│     stored in:  OCI artifact next to image                              │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  4. SIGNED IMAGE MANIFEST                                               │
│     image:    ghcr.io/me/api@sha256:def456...                           │
│     signature payload:                                                  │
│       critical:                                                         │
│         identity.docker-reference: ghcr.io/me/api                       │
│         image.docker-manifest-digest: sha256:def456...                  │
│         type: cosign container image signature                          │
│     signed by:  Fulcio cert bound to OIDC subject                       │
│     stored in:  Rekor + OCI artifact (.sig tag or referrer)             │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ admission request
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  5. ADMISSION VERIFICATION                                              │
│     policy-controller / Kyverno / Connaisseur:                          │
│       fetch signature artifact                                          │
│       verify signature over manifest digest                             │
│       verify Rekor inclusion proof                                      │
│       verify cert chain to Fulcio root via TUF                          │
│       verify cert identity matches policy                               │
│       fetch attestation artifact                                        │
│       verify provenance: buildType, builder.id, source repo, branch     │
│     decision: admit | reject                                            │
└─────────────────────────────────────────────────────────────────────────┘
```

Each arrow is *cryptographic binding by digest*. The provenance attestation names the source commit and the resulting image digest, so if you tampered with the image after the build, the digest in the attestation would not match. The SBOM is over the same digest, so a substituted image has no valid SBOM. The signature is over the same digest, so a substituted image has no valid signature. The admission controller only needs to verify one thing — the signature — and the rest of the chain is bound to that.

The next sections build the machinery to produce and verify each of these artifacts.

---

## 3. Sigstore: Architecture and Components

Sigstore is the name of the public-good infrastructure that makes keyless signing practical at scale. It is four components, plus a root of trust:

```
                            ┌──────────────────────────────┐
                            │  TUF root of trust           │
                            │  https://tuf-repo-cdn.        │
                            │   sigstore.dev               │
                            │  signed metadata for Fulcio   │
                            │   CA, Rekor pubkey, CT log    │
                            └──────────────┬───────────────┘
                                           │ fetched at first cosign use
                                           ▼
   ┌───────────────────┐                    │
   │   cosign (CLI)    │  ── OIDC token ────┴──┐
   │   sigstore/cosign │                       │
   └────────┬──────────┘                       │
            │                                  ▼
            │                       ┌──────────────────────┐
            │                       │  Fulcio              │
            │                       │  short-lived X.509   │
            │                       │  CA, binds OIDC      │
            │                       │  subject → 10-min    │
            │                       │  cert                │
            │   ◄── cert ───────────┤  sigstore/fulcio     │
            │                       └──────────────────────┘
            │ sign digest with cert priv key
            │ (priv key never leaves memory; ephemeral)
            ▼
   ┌──────────────────┐                ┌──────────────────────┐
   │  REGISTRY (OCI)  │ ◄── upload ───┐│  Rekor               │
   │  signature       │   .sig OCI   ││  immutable            │
   │  artifact        │   artifact   ││  transparency log     │
   │  (cosign payload │              ││  Merkle tree of all   │
   │   + sig + cert)  │              ││  signing events       │
   └──────────────────┘              ┘│  sigstore/rekor       │
                                      └──────────────────────┘
                                       cosign also writes the
                                       signing event (entry +
                                       inclusion proof) here.
```

**cosign** (https://github.com/sigstore/cosign) is the CLI. It signs, verifies, attests, and uploads. It is the user-facing surface. Everything else is infrastructure cosign talks to.

**Fulcio** (https://github.com/sigstore/fulcio) is a code-signing certificate authority that mints short-lived (10-minute) X.509 certificates whose Subject Alternative Name encodes an OIDC identity. The OIDC identity comes from an external IdP (GitHub Actions OIDC, Google, GitLab, Buildkite, Kubernetes ServiceAccount tokens via OIDC, etc.). Fulcio's job is one thing: prove "the holder of this certificate's private key, at this moment, was the holder of an OIDC token with this subject and this issuer". The certificate is good for ten minutes, then it expires; the private key is generated in-memory by cosign and thrown away after signing.

**Rekor** (https://github.com/sigstore/rekor) is an append-only transparency log. Every signature event is recorded in Rekor as a Merkle-tree entry; Rekor publishes a signed tree head periodically; auditors monitor the log for unexpected entries. The point of Rekor is that *signing is detectable*. If an attacker compromises Fulcio for thirty minutes and mints a cert in your name, the resulting signature is visible in Rekor and a monitor can flag it. Without a transparency log, a stolen key (or a compromised CA) signs invisibly.

**TUF** (The Update Framework, https://theupdateframework.io/) is the root of trust. It distributes the trusted public keys of Fulcio's CA root and Rekor's signing key. TUF metadata is itself signed by offline keys with role separation (root, targets, snapshot, timestamp), expiration windows, and threshold signatures. The cosign client fetches TUF metadata, verifies it, and learns *which* Fulcio root and Rekor pubkey to trust. Without TUF, you'd have to ship Sigstore root keys hard-coded in every cosign release, which is exactly the brittle distribution model PKI has always failed at.

**Certificate Transparency (CT) log.** Fulcio's issued certificates are also logged to a CT log so that anyone can audit which certs were minted. This is the same pattern Let's Encrypt uses for TLS certs. The cosign verifier checks the SCT (Signed Certificate Timestamp) embedded in the Fulcio cert.

The combination is "PKI without the worst parts of PKI":

- No long-lived signing keys to lose or rotate (Fulcio certs expire in 10 minutes).
- No key escrow problem (the private key is generated, used, and discarded in seconds).
- Detectable misuse (Rekor + CT log).
- Verifiable identity (the cert binds to an OIDC subject the verifier can policy on).
- Distributed root of trust (TUF).

The price is online dependence: signing needs Fulcio reachable, and verification typically needs Rekor reachable. Section 29 covers the air-gapped case.

---

## 4. The Keyless OIDC Signing Flow, Step by Step

Now the flow, in mechanical detail. The setting: a GitHub Actions workflow builds an image and signs it. (Every other CI follows the same shape; section 19 lists the variants.)

```
[GitHub Actions runner]
  │
  │ (1) Workflow declares: permissions: { id-token: write }
  │     This gives the runner the ability to mint an OIDC token
  │     signed by GitHub's OIDC provider:
  │       issuer:  https://token.actions.githubusercontent.com
  │       subject: repo:myorg/myrepo:ref:refs/heads/main
  │       aud:     sigstore (custom audience requested by cosign)
  │
  ▼
[cosign sign ghcr.io/me/api@sha256:def...]
  │
  │ (2) cosign reads $ACTIONS_ID_TOKEN_REQUEST_URL and
  │     $ACTIONS_ID_TOKEN_REQUEST_TOKEN env vars
  │     (set by the runner because of id-token: write)
  │     POSTs to that URL with audience=sigstore
  │     receives a signed JWT
  │
  │ (3) cosign generates an ephemeral ECDSA P-256 keypair in memory
  │
  │ (4) cosign sends to Fulcio:
  │       POST /api/v2/signingCert
  │       body: {
  │         credentials.oidcIdentityToken: <JWT>,
  │         publicKey:                     <PEM of pubkey>,
  │         proofOfPossession:             <signature of token sub by privkey>
  │       }
  │
  ▼
[Fulcio]
  │
  │ (5) Verifies the JWT signature against its trusted issuer list
  │     (GitHub Actions, Google, GitLab, etc.)
  │     Extracts subject (e.g., "repo:myorg/myrepo:ref:refs/heads/main")
  │     Extracts issuer (e.g., "https://token.actions.githubusercontent.com")
  │     Verifies proofOfPossession to confirm the requester holds the privkey
  │
  │ (6) Mints an X.509 cert:
  │       Subject: empty (deliberately, the identity is in SAN)
  │       Subject Alternative Name (SAN):
  │         URI: https://github.com/myorg/myrepo/.github/workflows/build.yml@refs/heads/main
  │       X.509 extensions (OIDs in the 1.3.6.1.4.1.57264 range):
  │         1.3.6.1.4.1.57264.1.1   issuer: https://token.actions.githubusercontent.com
  │         1.3.6.1.4.1.57264.1.2   GitHub workflow trigger (push, pull_request, …)
  │         1.3.6.1.4.1.57264.1.3   commit SHA
  │         1.3.6.1.4.1.57264.1.4   workflow name
  │         1.3.6.1.4.1.57264.1.5   repository
  │         1.3.6.1.4.1.57264.1.6   ref
  │         (many more: source repo digest, run id, run attempt, …)
  │       notBefore: now
  │       notAfter:  now + 10 minutes
  │       Signed by Fulcio's CA private key.
  │
  │ (7) Submits the cert to a CT log; embeds the SCT in the cert.
  │
  │     Returns cert chain to cosign.
  │
  ▼
[cosign continues]
  │
  │ (8) Computes the payload to sign. For a container signature
  │     (cosign cosign/v1 payload):
  │       {
  │         "critical": {
  │           "identity": { "docker-reference": "ghcr.io/me/api" },
  │           "image":    { "docker-manifest-digest":
  │                         "sha256:def..." },
  │           "type":     "cosign container image signature"
  │         },
  │         "optional": null
  │       }
  │
  │ (9) Signs SHA256(payload) with the ephemeral private key.
  │
  │ (10) Sends to Rekor:
  │        POST /api/v1/log/entries
  │        body: { signature, payload, cert chain }
  │      Rekor returns: log index, log entry UUID, signed timestamp,
  │      and an inclusion proof (Merkle path).
  │
  │ (11) Builds a "signature bundle" — the Sigstore bundle format,
  │      currently defined in sigstore/protobuf-specs:
  │        {
  │          messageSignature: { signature, hashAlgorithm },
  │          verificationMaterial: {
  │            x509CertificateChain: [...],
  │            tlogEntries:          [...]   // Rekor entry + proof
  │          }
  │        }
  │
  │ (12) Uploads to the registry:
  │      either as a tagged artifact: ghcr.io/me/api:sha256-def....sig
  │      or via referrers API (OCI 1.1+): subject = the image manifest,
  │      with mediaType application/vnd.dev.sigstore.bundle.v0.3+json
  │
  ▼
[Done — total wall time ~1–3 seconds]
  │
  │ (13) Ephemeral private key is discarded.
  │      The cert has 8+ more minutes of validity but no one cares —
  │      verifiers check the Rekor inclusion timestamp against the
  │      cert's notBefore/notAfter, not the current time.
```

The conceptual punchline: **the only secret material that ever existed was held in cosign's memory for 1–3 seconds, and the only way to get a Fulcio cert in your name is to hold an OIDC token signed by GitHub for your repo at that instant**. There is no key to steal, no rotation to schedule. The "identity" you sign with is the workflow identity that the OIDC provider asserts.

The Rekor entry binds *cert + signature + payload + timestamp* in a transparency log. If Fulcio is compromised tomorrow and an attacker mints a cert in your name, that signature can only land in Rekor if the attacker also somehow forces Rekor to lie. A monitor watching Rekor for new entries under your identity would see the entry and alert.

---

## 5. What Gets Signed: Manifest Digest vs Tag

The single most misunderstood property of cosign:

**You sign the digest, not the tag.**

```
cosign sign ghcr.io/me/api:v1.2.3
```

does **not** sign "the thing currently at tag `v1.2.3`". It resolves the tag to its current manifest digest, signs the **digest**, and stores the signature next to the digest. Tomorrow, someone with push access overwrites `v1.2.3` to point at a new manifest. The new manifest has a different digest. The old signature is still in the registry, still valid for the old digest, and *useless for the new tag content*.

Why this design is correct:

```
TAG               ────►   MANIFEST DIGEST   ────►   IMAGE CONTENT
"ghcr.io/me/api:v1.2.3"   sha256:def...             layers, config

The tag is a mutable pointer; one tag can point to many digests over time.
The digest is content-addressed; the same digest is always the same image.
A signature MUST be over an immutable thing or it is a lie.
```

In practice, signature artifacts are stored under tags derived from the digest, not under the human tag:

```
# Pre-OCI 1.1 layout (used by cosign for years):
ghcr.io/me/api:sha256-def....sig        # signature
ghcr.io/me/api:sha256-def....att        # attestations
ghcr.io/me/api:sha256-def....sbom       # SBOM (older cosign)

# OCI 1.1+ referrers API layout:
GET /v2/me/api/referrers/sha256:def...
  → returns a list of manifests that "refer to" the image,
    each with its own digest and artifactType.
```

When you `cosign verify ghcr.io/me/api:v1.2.3 --certificate-identity ...`, cosign does this:

```
1. Resolve the tag to its current digest by HEAD /v2/me/api/manifests/v1.2.3
2. Fetch the signature artifact at the digest-derived tag (or via referrers)
3. Verify the signature payload's "image.docker-manifest-digest" matches
   the digest you just resolved
4. Verify the cert, the Rekor entry, and the identity
```

If between step 1 and the pull the tag is repointed to something malicious, you have two protections: (a) cosign reports the digest it verified, so a downstream consumer can pin to that digest; (b) at admission, the policy verifies the digest *and* the admission webhook should resolve the tag to a digest and pin it in the pod spec (see §24). Anything that consumes the tag without pinning to the digest you verified is vulnerable to a tag swap.

**Rule: signatures verify digests. Tags are an index; pin to the digest the verifier returned.**

---

## 6. cosign verify: Anatomy of a Verification

`cosign verify` does seven things. Knowing all seven matters because each one is a policy knob — and a place where misconfiguration silently weakens the chain.

```
cosign verify \
  --certificate-identity-regexp 'https://github.com/myorg/.+/.github/workflows/.+@refs/heads/main' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/me/api:v1.2.3
```

The seven verifications:

```
(1) DIGEST RESOLUTION
    HEAD ghcr.io/me/api:v1.2.3 → digest sha256:def...
    All subsequent checks are against this digest.

(2) SIGNATURE FETCH
    Look for ghcr.io/me/api:sha256-def....sig or use referrers API.
    May find multiple signatures (image was signed by multiple identities).
    All are verified; at least one must pass policy.

(3) CRYPTOGRAPHIC SIGNATURE
    Decode the payload (cosign claim format).
    Verify ECDSA/RSA signature over SHA256(payload) using
    the public key in the embedded cert.
    Verify payload.critical.image.docker-manifest-digest == sha256:def...
    Verify payload.critical.identity.docker-reference == ghcr.io/me/api.

(4) CERTIFICATE CHAIN
    Build path from embedded cert to a Fulcio root distributed via TUF.
    Verify intermediate signatures, validity periods.

(5) CT LOG (Signed Certificate Timestamp)
    The embedded SCT proves Fulcio published the cert to a CT log.
    Verify SCT signature against the CT log's pubkey (also from TUF).

(6) REKOR INCLUSION PROOF
    Use the Rekor entry in the signature bundle.
    Verify the entry's inclusion proof against Rekor's signed tree head.
    Verify the entry's signedEntryTimestamp (signed by Rekor's key).
    Verify the signing-time interval: the cert's notBefore/notAfter must
    contain the Rekor entry's timestamp. (This is what lets us verify a
    signature made by a now-expired cert — the cert was valid AT THE TIME
    of signing, as proven by Rekor's signed timestamp.)

(7) IDENTITY POLICY
    Extract Subject Alternative Name from the cert (the OIDC subject).
    Extract the issuer extension (OID 1.3.6.1.4.1.57264.1.1 or 1.3.6.1.4.1.57264.1.8).
    Compare against --certificate-identity{-regexp} and --certificate-oidc-issuer.
    Reject if no match.
```

Step 7 is where 90% of operational mistakes live. The other six are cryptographic and largely automatic. Step 7 is the **policy** — you, the operator, choosing what identities you trust.

The minimum viable policy is two flags:

- `--certificate-identity` (or `--certificate-identity-regexp`): the SAN URI the cert must have, e.g., the URL to the GitHub Actions workflow file.
- `--certificate-oidc-issuer` (or `--certificate-oidc-issuer-regexp`): the OIDC issuer the JWT came from.

You **must** set both. If you only set identity, an attacker who compromises any OIDC provider can mint a cert with the same SAN. If you only set issuer, anyone with any workflow at GitHub Actions can sign as you.

---

## 7. The cert-identity and cert-oidc-issuer Policy Knobs

These two flags deserve their own section because they are the only thing standing between "I verified a cosign signature" and "I verified that the signature came from my CI".

A real Fulcio cert SAN for a GitHub Actions signing looks like:

```
URI: https://github.com/myorg/myrepo/.github/workflows/release.yml@refs/heads/main
```

The corresponding issuer (X.509 extension OID 1.3.6.1.4.1.57264.1.1):

```
https://token.actions.githubusercontent.com
```

Policy choices and what they mean:

```
EXACT MATCH (strictest):
--certificate-identity 'https://github.com/myorg/myrepo/.github/workflows/release.yml@refs/heads/main'
--certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
  → only signed by this exact workflow file on this exact branch.

REGEXP — any workflow in this repo on main:
--certificate-identity-regexp '^https://github\.com/myorg/myrepo/\.github/workflows/.+@refs/heads/main$'
--certificate-oidc-issuer 'https://token.actions.githubusercontent.com'

REGEXP — any repo in the org on main:
--certificate-identity-regexp '^https://github\.com/myorg/[^/]+/\.github/workflows/.+@refs/heads/main$'
--certificate-oidc-issuer 'https://token.actions.githubusercontent.com'

TOO PERMISSIVE:
--certificate-identity-regexp '.*github\.com.*'
  → ANY public GitHub workflow on GitHub.com can sign as you.
    This is a CVE waiting to be reported.

CATASTROPHIC:
--certificate-identity-regexp '.*'
  → anything in any Fulcio cert from any issuer.
    Identical to "no signature verification".
```

Three principles for writing identity policies:

1. **Anchor your regex.** Always `^...$`. Without anchors, `github\.com` matches `evil-github.com.attacker.example`.
2. **Pin the branch (or tag pattern).** A signature from `refs/heads/main` and from `refs/heads/dependabot-attacker` are equally cryptographically valid; only your policy distinguishes them.
3. **Pin the issuer in lock-step.** Identity URIs are not globally unique across issuers. A workflow with the same path on a self-hosted Gitea could have a colliding SAN; the issuer extension is what prevents that confusion.

In policy-controller and Kyverno (see §22–23), these knobs are exposed as `identities[]` entries with `subject` and `issuer` fields.

---

## 8. Long-Lived Key Signing (the Alternative to Keyless)

Keyless is the default and the recommended path. But three scenarios force you to long-lived keys:

1. **Air-gapped / disconnected environments** where Fulcio is unreachable.
2. **Registries that don't support OCI artifacts** (older Docker Hub paths, some on-prem registries).
3. **Compliance regimes** that require a stable signing key with documented rotation.

The cosign workflow for long-lived keys:

```bash
# Generate a key pair (prompts for a password to encrypt the private key):
cosign generate-key-pair
# Produces cosign.key (encrypted) and cosign.pub.

# Sign:
cosign sign --key cosign.key ghcr.io/me/api@sha256:def...

# Verify:
cosign verify --key cosign.pub ghcr.io/me/api@sha256:def...
```

You can also store the key in cloud KMS, which is the strongly recommended pattern:

```bash
cosign sign --key awskms:///arn:aws:kms:us-east-1:123:key/abc-def-... \
  ghcr.io/me/api@sha256:def...

cosign sign --key gcpkms://projects/my-proj/locations/global/keyRings/sigs/cryptoKeys/cosign \
  ghcr.io/me/api@sha256:def...

cosign sign --key azurekms://my-vault.vault.azure.net/cosign \
  ghcr.io/me/api@sha256:def...

cosign sign --key hashivault://my-key \
  ghcr.io/me/api@sha256:def...
```

KMS-backed keys are the right way to do long-lived signing:

- The private key never leaves the KMS HSM.
- Signing is an authenticated API call with audit logging.
- Rotation is a config change in the policy.
- Access control is IAM, not file permissions.

What you lose by going long-lived:

- **No identity binding.** The signature attests "someone with access to this key signed this". The identity policy `--certificate-identity` does not apply — there is no Fulcio cert. You can verify with `--key`, and that's all.
- **No transparency by default.** You can still upload to Rekor with `cosign sign --key ... --rekor-url ...`, but verification is by key, not by Rekor inclusion. (cosign 2.x lets you require Rekor with `--tlog-upload=true`.)
- **Key rotation becomes your problem.** Old signatures with old keys must continue to verify; new images must use new keys; verification config must accept both during the transition window.

The hybrid you usually want: keyless for production CI, KMS-backed for emergencies and out-of-band signing. Both go into the verification policy as alternatives.

---

## 9. SBOMs: SPDX vs CycloneDX

A signature says "this image came from where I expected". An SBOM says "this image contains what I expected". Together they are necessary; neither alone is sufficient.

A Software Bill of Materials is a structured list of every component in a software artifact. The two formats in practice:

```
SPDX (Software Package Data Exchange)
  Linux Foundation, ISO/IEC 5962:2021.
  Originally license-tracking focused; now full SBOM.
  Versions: SPDX 2.3 widespread; SPDX 3.0 published 2024.
  Format:   tag-value (text) or JSON.
  Strength: license metadata, regulatory acceptance, mature toolchain.

CycloneDX
  OWASP project, ECMA-424 (as of 2024).
  Originally vulnerability/security focused.
  Versions: 1.5 widespread; 1.6 current.
  Format:   JSON or XML.
  Strength: vulnerability disclosure, services, ML/AI BOMs (now: SBOM,
            HBOM hardware, SaaSBOM, ML-BOM, CBOM cryptographic).
```

Choose by the consumer:

- License compliance / FOSS hygiene team: SPDX.
- Vulnerability management / security team: CycloneDX.
- Both: generate both, sign both. Storage is cheap.

A CycloneDX 1.6 fragment for a Node app image:

```json
{
  "bomFormat": "CycloneDX",
  "specVersion": "1.6",
  "serialNumber": "urn:uuid:3e671687-395b-41f5-a30f-a58921a69b79",
  "version": 1,
  "metadata": {
    "timestamp": "2026-05-23T12:00:00Z",
    "tools": { "components": [{ "name": "syft", "version": "1.10.0" }] },
    "component": {
      "type": "container",
      "name": "ghcr.io/me/api",
      "version": "sha256:def456...",
      "purl": "pkg:oci/api@sha256:def456...?repository_url=ghcr.io/me"
    }
  },
  "components": [
    {
      "type": "library",
      "name": "express",
      "version": "4.19.2",
      "purl": "pkg:npm/express@4.19.2",
      "hashes": [
        { "alg": "SHA-256", "content": "abc123..." }
      ],
      "licenses": [{ "license": { "id": "MIT" } }]
    },
    {
      "type": "operating-system",
      "name": "debian",
      "version": "12.5"
    },
    {
      "type": "library",
      "name": "libssl3",
      "version": "3.0.11-1~deb12u2",
      "purl": "pkg:deb/debian/libssl3@3.0.11-1~deb12u2?arch=amd64&distro=debian-12"
    }
  ],
  "dependencies": [
    { "ref": "pkg:npm/express@4.19.2", "dependsOn": [
        "pkg:npm/accepts@1.3.8",
        "pkg:npm/body-parser@1.20.2"
    ]}
  ]
}
```

Key fields that matter for downstream tooling:

- **purl (Package URL).** A platform-independent identifier like `pkg:npm/express@4.19.2` or `pkg:deb/debian/libssl3@3.0.11-1~deb12u2`. The purl is what vulnerability databases (OSV, NVD via CPE-to-purl mapping) match against.
- **hashes.** Content-addressing for components. A vuln scanner can confirm the component in the image is actually the version named in the SBOM.
- **dependencies.** The DAG. Knowing `express` is in the SBOM is much less useful than knowing it was pulled in *because of* one specific top-level dep.

The SPDX equivalent uses slightly different terminology (`PackageName`, `PackageVersion`, `PackageLicenseConcluded`, `Relationship`) but covers the same ground. Tooling routinely converts between them.

---

## 10. SBOM Generation Tooling: Syft, Trivy, cyclonedx-cli

SBOMs are generated by scanners that introspect a container image (or a source tree). The dominant tools:

```
Syft (anchore/syft)
  go install github.com/anchore/syft/cmd/syft@latest
  syft ghcr.io/me/api:v1.2.3 -o spdx-json > sbom.spdx.json
  syft ghcr.io/me/api:v1.2.3 -o cyclonedx-json > sbom.cdx.json
  Strengths: many ecosystems (npm, python, ruby, go, java, debian, alpine,
             rpm, gem, etc), accurate purl emission, fast.

Trivy (aquasecurity/trivy)
  trivy image --format cyclonedx --output sbom.cdx.json ghcr.io/me/api:v1.2.3
  trivy image --format spdx-json   --output sbom.spdx.json ghcr.io/me/api:v1.2.3
  Strengths: combines SBOM generation with vuln scanning in one pass.

cyclonedx-cli (CycloneDX/cyclonedx-cli)
  Postprocessing: merge, diff, sign, validate.
  Not primarily a generator; pairs with language-specific generators
  like cyclonedx-bom-python, cyclonedx-gomod, cyclonedx-node-npm.
```

A canonical CI step (GitHub Actions excerpt):

```yaml
- name: Generate SBOM
  uses: anchore/sbom-action@v0
  with:
    image: ghcr.io/myorg/api@${{ steps.build.outputs.digest }}
    format: spdx-json
    output-file: sbom.spdx.json

- name: Attest SBOM to image
  run: |
    cosign attest --predicate sbom.spdx.json \
      --type spdxjson \
      --yes \
      ghcr.io/myorg/api@${{ steps.build.outputs.digest }}
```

The two important properties of a good CI SBOM step:

1. **It runs after the build, on the produced image.** Generating an SBOM from the source tree captures *what was supposed to be in the image*; generating from the built image captures *what is actually in the image*. The difference is where supply chain attacks hide.
2. **It pins to the digest.** Even your own CI must not rely on `:latest` — between the build and the SBOM scan, an attacker with registry write could swap the image.

---

## 11. Signing the SBOM: cosign attest

An unsigned SBOM is interesting but unauthenticated. Anyone could write `{"components": [{"name": "express", "version": "1.0.0"}]}` and claim it describes your image. To make an SBOM useful in admission, you sign it the same way you sign the image — except the artifact is the SBOM, not the manifest, and the signing operation is `cosign attest` instead of `cosign sign`:

```bash
cosign attest \
  --predicate sbom.cdx.json \
  --type cyclonedx \
  --yes \
  ghcr.io/me/api@sha256:def...
```

What this actually does:

```
1. Wrap the SBOM as an in-toto Statement:
     {
       "_type": "https://in-toto.io/Statement/v1",
       "subject": [{
         "name":   "ghcr.io/me/api",
         "digest": { "sha256": "def..." }
       }],
       "predicateType": "https://cyclonedx.org/bom",
       "predicate":     <the SBOM JSON>
     }

2. Wrap the Statement in a DSSE envelope (Dead Simple Signing Envelope,
   in-toto/specs/v1/DSSE):
     {
       "payloadType": "application/vnd.in-toto+json",
       "payload":     <base64-encoded Statement>,
       "signatures":  [{ "sig": <base64 sig>, "keyid": "" }]
     }

3. Sign the DSSE PAE (Pre-Authentication Encoding) string with a Fulcio
   cert obtained via OIDC, exactly as in §4.

4. Log to Rekor as a hashedrekord or dsse entry type.

5. Upload to the registry as an OCI artifact next to the image, with
   artifactType "application/vnd.dsse.envelope.v1+json" or via the
   .att tag (older cosign).
```

Now the SBOM has the same trust properties as the image signature: cryptographically bound to the image digest, transparency-logged, identity-bound.

Verification:

```bash
cosign verify-attestation \
  --type cyclonedx \
  --certificate-identity-regexp '^https://github\.com/myorg/.+/\.github/workflows/.+@refs/heads/main$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/me/api@sha256:def... \
  | jq -r .payload | base64 -d | jq .predicate > verified-sbom.json
```

You can now run that SBOM through a vulnerability scanner (`grype sbom:verified-sbom.json`) and trust the result, because the SBOM was signed by the CI you trust.

---

## 12. In-Toto Attestations: Subject + Predicate

The pattern `cosign attest` uses is general. It is defined by the in-toto attestation spec (https://github.com/in-toto/attestation), and the shape is:

```
in-toto Statement v1
┌──────────────────────────────────────────────────────────────┐
│  "_type":         "https://in-toto.io/Statement/v1"          │
│  "subject":       [ { name, digest: { sha256: ... } }, ... ] │
│  "predicateType": "<URI naming the schema of predicate>"     │
│  "predicate":     <arbitrary JSON conforming to that schema> │
└──────────────────────────────────────────────────────────────┘
```

The **subject** identifies what the attestation is about: typically one image by digest. The **predicateType** is a URI that names the schema; the **predicate** is structured data matching that schema. The Statement is then signed and logged.

Standard predicate types (https://github.com/in-toto/attestation/tree/main/spec/predicates):

```
Predicate                              URI                                            Use
─────────────────────────────────────  ─────────────────────────────────────────────  ─────────────────────────
SLSA Provenance v1                     https://slsa.dev/provenance/v1                 Build provenance (§17)
SPDX SBOM                              https://spdx.dev/Document                      Software BoM (SPDX)
CycloneDX SBOM                         https://cyclonedx.org/bom                      Software BoM (CycloneDX)
Vulnerability scan                     https://cosign.sigstore.dev/attestation/vuln/v1 Trivy/Grype scan result
Test result                            https://in-toto.io/attestation/test-result/v0.1 CI test outcome
Link (legacy in-toto)                  https://in-toto.io/Link/v1                     in-toto layout/links
VEX                                    https://openvex.dev/ns                         OpenVEX statement (§26)
Source                                 https://slsa.dev/source/v1                     Source provenance
Runtime trace                          https://in-toto.io/attestation/runtime-trace/v0.1 Runtime telemetry
```

The point of the predicate type is that admission policy can demand a specific type:

```yaml
# Kyverno
verifyImages:
- imageReferences: ["ghcr.io/myorg/*"]
  attestations:
  - type: https://slsa.dev/provenance/v1
    conditions: [...]
  - type: https://cyclonedx.org/bom
    conditions: [...]
```

This means "admit only images that have (a) a SLSA provenance attestation with the policy conditions, *and* (b) a CycloneDX SBOM attestation". The combination is what binds the image to its origin and its content.

The DSSE envelope around the Statement is the actual signed thing; in-toto deliberately separated the data model (Statement) from the signing format (DSSE), so a Statement can be signed by cosign, by an HSM, by `slsa-github-generator`, by your custom tool — and all verifiers agree on what the payload means.

---

## 13. Verifying Attestations at Admission

`cosign verify-attestation` does the same seven checks as `cosign verify` (§6), plus a predicate-type filter and optional CUE/Rego policy evaluation over the predicate:

```bash
cosign verify-attestation \
  --type slsaprovenance \
  --certificate-identity-regexp '^https://github\.com/myorg/.+' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --policy policy.cue \
  ghcr.io/me/api@sha256:def...
```

The CUE policy file evaluates over the verified predicate. A real example:

```cue
// policy.cue: require SLSA provenance built from main branch by our CI

predicate: {
    buildDefinition: {
        buildType: "https://actions.github.io/buildtypes/workflow/v1"
        externalParameters: {
            workflow: {
                repository: "https://github.com/myorg/myrepo"
                ref:        "refs/heads/main"
                path:       =~ "^\\.github/workflows/(release|build)\\.ya?ml$"
            }
        }
    }
    runDetails: {
        builder: id: =~ "^https://github\\.com/actions/runner.*"
    }
}
```

At admission, policy-controller and Kyverno run this verification per pod create. They cache results by image digest (the digest doesn't change, so the verification result is stable). Section 33 covers the performance impact.

The structural property: **the admission controller only ever trusts what cosign verifies**. Cosign distills "this image is acceptable" into a single decision that the admission controller relays as admit/deny. You don't write crypto in your admission policy; you write identity policy and predicate policy.

---

## 14. SLSA: Supply-Chain Levels for Software Artifacts

SLSA (pronounced "salsa", https://slsa.dev/, https://github.com/slsa-framework/slsa) is a maturity framework for software supply chain integrity. Version 1.0 (2023, current) splits the framework into three **tracks**:

```
┌────────────────────────────────────────────────────────────────────────┐
│  BUILD track  — focuses on the build process                          │
│    L0 → L3, with L4 reserved/future                                   │
│    What you prove: how the artifact was produced and who produced it. │
└────────────────────────────────────────────────────────────────────────┘
┌────────────────────────────────────────────────────────────────────────┐
│  SOURCE track — focuses on the source code repository                 │
│    Levels for branch protection, two-person review, history retention. │
└────────────────────────────────────────────────────────────────────────┘
┌────────────────────────────────────────────────────────────────────────┐
│  DEPENDENCIES track — focuses on transitively pulled code             │
│    Levels for SBOM presence, vuln management, dep update policy.       │
└────────────────────────────────────────────────────────────────────────┘
```

The Build track is the most concrete and most adopted. The other tracks exist but are less crisp in tooling.

Each track is independent: a project can be Build L3 with Source L1 (excellent build hygiene, ad-hoc source policy) or vice versa. Don't say "SLSA Level 3" unless you also say which track.

What SLSA buys you over plain signing:

- A signature says "this came from CI".
- Provenance says "this came from CI **with these inputs, on this trigger, by this commit**".
- SLSA Build L3 says "this came from CI with those provenance attributes **and the build platform isolates builds from each other and the provenance cannot be forged by user code in the build**".

The asymmetry is critical: provenance is a statement; SLSA is a property of the platform that produces the statement. You can write any provenance you like; only certain platforms (or certain configurations of certain platforms) can be trusted to generate provenance that the build user could not have forged.

---

## 15. SLSA Build Track L1–L4 Requirements

The Build track is defined by what the **build platform** and **provenance** provide, not by what the source repo does. Summary table from https://slsa.dev/spec/v1.0/levels:

```
LEVEL    BUILD PLATFORM                       PROVENANCE                         WHAT YOU GAIN
─────    ──────────────────────────────────   ────────────────────────────────   ──────────────────────────
L0       No requirements                      None                               Nothing.

L1       Scripted, automated build.           Provenance is generated and        Mistakes are reproducible.
         No human "build on my laptop".       distributed alongside artifact.    Forgery is trivial — anyone
                                              May be unsigned.                   can edit the JSON.

L2       Hosted build platform.               Provenance is signed by the        Forgery requires compromising
         Not the developer's machine.         build platform's signing identity. the build platform.
         Build is recorded / observable.      Distributed with artifact.

L3       Build platform isolates builds       Provenance is unforgeable by       Forgery requires compromising
         from each other and from the         the build user. The signing       the build platform itself.
         signing infrastructure.              identity is held by the platform   The OIDC token (in Sigstore terms)
         User code in the build cannot        outside the user's reach.          is minted by an attestable
         extract the signing material.                                           runner that the user can't suborn.

L4       (reserved in v1.0)                   (reserved)                         Hermetic, reproducible.
         Originally: hermetic + reproducible.                                    Builds produce bit-identical
                                                                                 output from sources.
```

The mapping to real build platforms:

```
PLATFORM                                       BUILD LEVEL OBTAINABLE
GitHub Actions self-hosted runner               L2 at most (you control the runner)
GitHub Actions managed runner + slsa-github-gen L3 (the generator forces the OIDC token through
                                                a reusable workflow you can't tamper with)
GitLab CI managed runners                        L2; L3 with care
Tekton Chains on managed cluster                L3 with strict ChainsConfig
Buildkite self-hosted                            L2
Internal Jenkins                                 L1–L2 (usually L1)
Local docker build                               L0
```

The most-actually-achieved level in production today is **L2** (signed provenance from a hosted platform), with L3 reserved for high-stakes pipelines using `slsa-github-generator` or equivalent.

A subtle but important consequence of the L2→L3 distinction: if the *workflow YAML in your repo* generates the provenance, the build user (anyone with write access to the workflow file) can edit the workflow to lie. The SLSA L3 fix is to call out to a reusable workflow whose code lives in a separate repo with stricter access controls, and have *that* workflow generate the provenance and sign it. The L3 generator is in `slsa-framework/slsa-github-generator`.

---

## 16. In-Toto: Layouts, Links, and Functionaries

SLSA provenance is one application of a much older framework: **in-toto** (https://in-toto.io/, https://github.com/in-toto/attestation, https://github.com/in-toto/in-toto). In-toto predates SLSA and provides the underlying metadata model.

Original in-toto vocabulary:

```
Layout
   Declarative description of a supply chain:
     - the steps in order (e.g., clone, build, package)
     - who is allowed to perform each step (Functionaries, by pubkey)
     - what artifacts flow between steps (products of step N = materials of step N+1)
     - inspections (post-hoc rules: this binary must contain this version string)
   Signed by the project owner.

Link
   Evidence that a step was performed.
   Signed by the Functionary who performed it.
     - command run
     - environment
     - materials in (hashes of inputs)
     - products out (hashes of outputs)
   A Link is the predecessor of today's in-toto Statement.

Functionary
   A principal with a key, authorized in the Layout to sign Links for some
   step.
```

The old in-toto model required project owners to write Layouts up front. SLSA's contribution was to recognize that for the vast majority of users, the Layout collapses to "one build step, performed by my CI, that produces my artifact". So SLSA defined a single canonical Statement type (the SLSA Provenance predicate) that captures everything most projects need.

What this means in practice: **most teams writing SLSA-compliant pipelines never see the word "Layout". They see provenance Statements, signed via DSSE, stored as in-toto Attestations.** Layouts come back when you need multi-step supply chains (e.g., build, then sign, then notarize, then package, with different identities for each step).

Tooling:

```
in-toto-cli      Original CLI; produces and verifies Layouts and Links.
slsa-verifier    Verifier specifically for SLSA provenance attestations.
                 (slsa-framework/slsa-verifier)
cosign verify-attestation   General attestation verifier; type-aware.
```

For most readers of this chapter: you'll touch in-toto only through SLSA's Statement format and through cosign. The deeper layout machinery is in the spec for when your supply chain becomes a graph rather than a line.

---

## 17. SLSA Provenance v1: The buildDefinition + runDetails Schema

The SLSA Provenance v1 predicate is the canonical "what was built and how" document. Schema (from https://slsa.dev/spec/v1.0/provenance):

```json
{
  "buildDefinition": {
    "buildType": "https://actions.github.io/buildtypes/workflow/v1",
    "externalParameters": {
      "workflow": {
        "ref":        "refs/heads/main",
        "repository": "https://github.com/myorg/myrepo",
        "path":       ".github/workflows/release.yml"
      }
    },
    "internalParameters": {
      "github": {
        "event_name":    "push",
        "repository_id": "12345678",
        "runner_environment": "github-hosted"
      }
    },
    "resolvedDependencies": [
      {
        "uri":    "git+https://github.com/myorg/myrepo@refs/heads/main",
        "digest": { "gitCommit": "abc123..." }
      },
      {
        "uri":    "docker://docker.io/library/golang:1.22@sha256:...",
        "digest": { "sha256": "..." }
      }
    ]
  },
  "runDetails": {
    "builder": {
      "id":      "https://github.com/myorg/myrepo/.github/workflows/release.yml@refs/heads/main",
      "version": { "github-actions": "v4.0.0" }
    },
    "metadata": {
      "invocationId": "https://github.com/myorg/myrepo/actions/runs/1234567890/attempts/1",
      "startedOn":    "2026-05-23T12:00:00Z",
      "finishedOn":   "2026-05-23T12:08:43Z"
    },
    "byproducts": [
      {
        "uri":    "https://github.com/myorg/myrepo/attestations/12345",
        "digest": { "sha256": "..." }
      }
    ]
  }
}
```

Field-by-field, what it lets you prove:

```
buildDefinition.buildType
  The schema URL for externalParameters/internalParameters.
  Knowing the type tells the verifier what fields to expect.

buildDefinition.externalParameters
  Inputs the build USER can change. The actual build instructions:
  which workflow file, which ref, which inputs.

buildDefinition.internalParameters
  Inputs the build PLATFORM controls. Runner OS, runner image digest,
  GitHub event details. These are evidence of the build environment
  that the user did not choose.

buildDefinition.resolvedDependencies
  Every dependency the build pulled, by digest. The source commit
  is here. Base images are here. Tooling images are here.

runDetails.builder.id
  The identity of the build platform. NOT the user identity — the
  PLATFORM identity. For SLSA L3, this is the reusable workflow URL
  rather than the user's workflow URL.

runDetails.metadata.invocationId
  Pointer to the build run. Auditors can fetch the build logs.

runDetails.metadata.startedOn / finishedOn
  Build window. Useful for "this image was built in the last 24 hours"
  freshness policies.

runDetails.byproducts
  Secondary artifacts produced (SBOMs, build logs, etc.).
```

The two policy questions you can answer from this:

1. **Where did this image come from?** Read `externalParameters.workflow.repository` and `.ref` and `.path`. Read `resolvedDependencies` for the source commit.
2. **Was the build environment trustworthy?** Read `builder.id` and `internalParameters.github.runner_environment` (`github-hosted` vs `self-hosted`).

A typical admission policy will pin (a) the repository, (b) the ref pattern, (c) the workflow path, and (d) `runner_environment == github-hosted`.

---

## 18. GitHub Actions Signing: The Reference Implementation

GitHub Actions is the canonical reference because (a) it's free for public repos, (b) it has a native OIDC provider, and (c) `sigstore/cosign-installer` and `slsa-framework/slsa-github-generator` are first-party-quality reusable workflows. A complete signing workflow:

```yaml
# .github/workflows/release.yml
name: build-sign-attest

on:
  push:
    branches: [main]
    tags: ['v*']

permissions:
  contents: read
  packages: write          # push to GHCR
  id-token: write          # request OIDC tokens for cosign
  attestations: write      # for actions/attest-* (GitHub-native attestations)

jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      digest: ${{ steps.push.outputs.digest }}
    steps:
      - uses: actions/checkout@v4

      - uses: docker/setup-buildx-action@v3

      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - id: push
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ghcr.io/${{ github.repository }}:${{ github.sha }}
          # the build-push action emits the manifest digest as an output
          provenance: false   # disable build-push provenance; we do our own
          sbom: false

      - uses: sigstore/cosign-installer@v3
        with:
          cosign-release: 'v2.4.0'

      - name: cosign sign
        env:
          COSIGN_EXPERIMENTAL: '1'   # historical; modern cosign defaults to keyless
        run: |
          cosign sign --yes \
            ghcr.io/${{ github.repository }}@${{ steps.push.outputs.digest }}

      - uses: anchore/sbom-action@v0
        with:
          image: ghcr.io/${{ github.repository }}@${{ steps.push.outputs.digest }}
          format: cyclonedx-json
          output-file: sbom.cdx.json

      - name: cosign attest SBOM
        run: |
          cosign attest --yes \
            --predicate sbom.cdx.json \
            --type cyclonedx \
            ghcr.io/${{ github.repository }}@${{ steps.push.outputs.digest }}

  # SLSA L3 provenance via the slsa-github-generator reusable workflow
  provenance:
    needs: build
    permissions:
      actions: read
      id-token: write
      packages: write
    uses: slsa-framework/slsa-github-generator/.github/workflows/generator_container_slsa3.yml@v2.0.0
    with:
      image: ghcr.io/${{ github.repository }}
      digest: ${{ needs.build.outputs.digest }}
      registry-username: ${{ github.actor }}
    secrets:
      registry-password: ${{ secrets.GITHUB_TOKEN }}
```

What this workflow produces, by the end:

```
ghcr.io/myorg/myrepo@sha256:def...                          (image)
ghcr.io/myorg/myrepo:sha256-def....sig                       (cosign signature)
ghcr.io/myorg/myrepo:sha256-def....att (or referrer)         (CycloneDX SBOM attestation)
ghcr.io/myorg/myrepo:sha256-def....intoto.jsonl              (SLSA L3 provenance)

Rekor entries for each of the above, queryable by:
  rekor-cli search --sha sha256:def... --rekor_server https://rekor.sigstore.dev
```

Three things to know about this workflow that the boilerplate hides:

1. **`id-token: write` is the magic.** This permission causes the runner to expose `$ACTIONS_ID_TOKEN_REQUEST_URL` and `$ACTIONS_ID_TOKEN_REQUEST_TOKEN`. Without it, cosign cannot get an OIDC token, signing fails, and the failure message is famously cryptic.

2. **The SLSA L3 job uses a *reusable workflow* (the `uses:` at the top of `provenance:`).** This is the L2→L3 promotion: the provenance is generated by code in `slsa-framework/slsa-github-generator`, not by code in your repo. You cannot tamper with the provenance generator by editing your own workflow file.

3. **`docker/build-push-action` has its own provenance/SBOM emission** (`provenance: true`, `sbom: true`). It's worth toggling on for ease-of-use but it produces *build-push-action* provenance, not SLSA-framework provenance. Pick one path or the other and document it.

---

## 19. GitLab CI, Tekton Chains, Buildkite, and Kubernetes ServiceAccount Signing

GitHub Actions is the most-documented but every CI with an OIDC provider works.

**GitLab CI.** GitLab issues OIDC tokens via `id_tokens:` in `.gitlab-ci.yml`:

```yaml
sign:
  image: alpine:3.19
  id_tokens:
    SIGSTORE_ID_TOKEN:
      aud: sigstore
  script:
    - apk add --no-cache cosign
    - cosign sign --yes "$IMAGE@$DIGEST"
```

The cosign CLI reads `$SIGSTORE_ID_TOKEN` automatically. The Fulcio cert SAN looks like `https://gitlab.com/myorg/myrepo//path/to/.gitlab-ci.yml@refs/heads/main`. Issuer: `https://gitlab.com`.

**Tekton Chains** (https://tekton.dev/docs/chains/, https://github.com/tektoncd/chains). Tekton is a Kubernetes-native CI; Chains is its sidecar controller that observes every TaskRun/PipelineRun completion and automatically generates and signs attestations. The big differentiator: signing is *not in the user's pipeline definition*. The user writes a Tekton pipeline that produces an image; Chains, running with its own ServiceAccount and keys, observes the completed run, builds the SLSA provenance from the run record, and signs it.

Chains config (`chains-config` ConfigMap in `tekton-chains` namespace):

```yaml
artifacts.taskrun.format: slsa/v2alpha3
artifacts.taskrun.storage: oci
artifacts.taskrun.signer: x509   # or kms

artifacts.oci.format: simplesigning
artifacts.oci.storage: oci
artifacts.oci.signer: x509

signers.x509.fulcio.enabled: 'true'
signers.x509.fulcio.identity-token-file: /var/run/sigstore/cosign/oidc-token

transparency.enabled: 'true'
transparency.url: https://rekor.sigstore.dev
```

Tekton + Chains is the cleanest separation: pipeline does what the user wants; provenance is generated by a controller the user can't suborn. This is Build L3 territory.

**Buildkite.** Buildkite supports OIDC tokens via `buildkite-agent oidc request-token --audience sigstore`. Sign:

```bash
export SIGSTORE_ID_TOKEN=$(buildkite-agent oidc request-token --audience sigstore)
cosign sign --yes ghcr.io/myorg/api@$DIGEST
```

Issuer: `https://agent.buildkite.com`.

**Kubernetes ServiceAccount signing.** Less common but possible: a pod running in a cluster with the cluster's API server set up as an OIDC issuer can use its ServiceAccount projected token as a Sigstore OIDC token. The pod's SA token has `aud: sigstore` (set in `projected.sources[].serviceAccountToken.audience`). Fulcio must trust the cluster's OIDC issuer URL (the API server's `--service-account-issuer` flag). This is mostly used for in-cluster signers like an internal Tekton or for cluster-internal SLSA generators.

---

## 20. Registry Support for OCI Artifacts and the Referrers API

Sigstore stores signatures and attestations *next to* the image in the same registry, as a separate artifact. There are two storage layouts in use:

**Legacy (pre-OCI 1.1) tag-based:**

```
ghcr.io/me/api:sha256-def....sig    (signature)
ghcr.io/me/api:sha256-def....att    (attestations)
ghcr.io/me/api:sha256-def....sbom   (older cosign sbom path)
```

The artifact is uploaded as an OCI image with a deterministic tag derived from the subject digest. Every registry that supports OCI image push supports this, because it's just a normal image push.

**OCI 1.1+ Referrers API:**

```
GET /v2/me/api/referrers/sha256:def...
  → {
      "schemaVersion": 2,
      "mediaType": "application/vnd.oci.image.index.v1+json",
      "manifests": [
        {
          "mediaType":   "application/vnd.oci.image.manifest.v1+json",
          "digest":      "sha256:aaa...",
          "size":        1234,
          "artifactType":"application/vnd.dev.sigstore.bundle.v0.3+json"
        },
        {
          "mediaType":   "application/vnd.oci.image.manifest.v1+json",
          "digest":      "sha256:bbb...",
          "size":        2345,
          "artifactType":"application/vnd.in-toto+json"
        }
      ]
    }
```

The referrers API formalizes "what other artifacts point at this one as their subject", returning an index. Cosign uses `subject` in the OCI manifest to declare the relationship, and the registry indexes by subject digest.

Registry support matrix (as of 2026):

```
REGISTRY                            COSIGN .sig TAGS    OCI 1.1 REFERRERS
─────────────────────────────────   ─────────────────   ────────────────────
AWS ECR                             yes (since 2023)    yes (since 2023)
Google Artifact Registry (GAR)      yes                  yes
Azure Container Registry (ACR)      yes                  yes
GitHub Container Registry (GHCR)    yes                  yes (since 2023)
Harbor                              yes                  yes (since 2.10)
Quay (Red Hat)                      yes                  yes
Docker Hub                          yes                  partial (uses fallback tag indexing)
Cloudsmith                          yes                  yes
JFrog Artifactory                   yes                  yes (recent versions)
```

When a registry doesn't natively support referrers, cosign falls back to a tag-based emulation:

```
Tag fallback for subject sha256:def...:
  ghcr.io/me/api:sha256-def...
  ↑ a tag derived from the subject, pointing at an OCI index manifest
    that lists all referring artifacts.

This lets verifiers find referrers on any registry, at the cost of an
extra tag per signed digest. Use COSIGN_EXPERIMENTAL_OCI11 or modern
cosign defaults; the behavior is automatic.
```

For on-prem registries (Harbor, Artifactory, internal Docker Distribution), check (a) referrers API support, (b) garbage collection: does the registry's GC understand that the signature artifact references the image and should not be deleted just because no tag points at the signature?

---

## 21. Admission-Time Verification: policy-controller, Kyverno, Connaisseur, Ratify

Signing is half the system. The other half is the admission webhook that refuses to admit pods whose images don't pass policy. Four production-grade choices:

```
policy-controller (sigstore/policy-controller)
  CRD:           ClusterImagePolicy
  Cosign-native: yes (uses cosign verify under the hood)
  Scope:         signature + attestation verification, image pinning
  Strengths:     reference implementation by the sigstore project; tracks
                 cosign features closely; CIP CRD is the canonical schema
                 for sigstore-style policy.
  Weaknesses:    narrower scope than Kyverno; no general policy beyond
                 image identity.

Kyverno (kyverno/kyverno)
  CRD:           ClusterPolicy with verifyImages rules
  Cosign-native: yes (calls sigstore libraries directly)
  Scope:         general policy engine (validate/mutate/generate) +
                 image verification + Notary v2 + manual public keys +
                 attestation verification with CEL/JMESPath assertions
  Strengths:     one engine for all admission policy; mutation support
                 (tag-to-digest); rich condition language; reports.
  Weaknesses:    learning curve; CRDs are large.

Connaisseur (sse-secure-systems/connaisseur)
  CRD:           ImagePolicy
  Cosign-native: yes, plus Notary v1/v2
  Scope:         narrowly: image signature enforcement only
  Strengths:     opinionated, simple, focused on signature gates;
                 supports multiple validators per policy.
  Weaknesses:    less active development than Kyverno/policy-controller.

Ratify (ratify-project/ratify, CNCF)
  CRD:           Store, Verifier, Policy, NamespacedPolicy
  Cosign-native: yes, plus Notary, plus pluggable
  Scope:         verification framework with pluggable verifiers and stores
  Strengths:     designed for heterogeneous environments (multiple
                 verifiers, multiple registries); the "Verifier" abstraction.
  Weaknesses:    more moving parts; younger project; integrates with
                 Gatekeeper for policy decisions.
```

The decision tree:

```
Already running Kyverno for general admission policy?
  → use Kyverno verifyImages.

Want the sigstore-native, schema-stable, minimal-surface option?
  → policy-controller.

Want a focused signature-only gate?
  → Connaisseur.

Building a heterogeneous platform across many trust frameworks?
  → Ratify (often with Gatekeeper).
```

All four implement the same loop: webhook intercept → fetch signature artifact → verify cosign-style → fetch attestations if required → match against policy → admit or reject. The differences are in the policy language and the integration surface.

---

## 22. ClusterImagePolicy YAML in Depth

The canonical sigstore policy CRD. Source: https://github.com/sigstore/policy-controller. A realistic policy:

```yaml
apiVersion: policy.sigstore.dev/v1beta1
kind: ClusterImagePolicy
metadata:
  name: require-signed-by-our-ci
spec:
  # Which images this policy applies to.
  images:
  - glob: "ghcr.io/myorg/**"
  - glob: "myregistry.internal/**"

  # What it must satisfy.
  authorities:

  # (a) keyless signature from our CI
  - name: ci-keyless
    keyless:
      url: https://fulcio.sigstore.dev
      identities:
      - issuer: https://token.actions.githubusercontent.com
        subjectRegExp: '^https://github\.com/myorg/[^/]+/\.github/workflows/.+@refs/heads/main$'
      # Use the TUF-distributed trust root by default; pin if needed.
      trustRootRef: sigstore-public

    # And ALSO require these attestations.
    attestations:
    - name: slsa-provenance
      predicateType: https://slsa.dev/provenance/v1
      policy:
        type: cue
        data: |
          predicate: {
              buildDefinition: {
                  buildType: "https://actions.github.io/buildtypes/workflow/v1"
                  externalParameters: workflow: {
                      repository: "https://github.com/myorg/myrepo"
                      ref:        =~ "^refs/(heads/main|tags/v[0-9]+\\.[0-9]+\\.[0-9]+)$"
                  }
              }
              runDetails: {
                  builder: id: =~ "^https://github\\.com/.+"
              }
          }

    - name: sbom
      predicateType: https://cyclonedx.org/bom

  # Optional: fallback authority — long-lived key for emergency builds.
  - name: emergency-kms
    key:
      kms: awskms:///arn:aws:kms:us-east-1:123:key/emergency-cosign-key

  # Default behavior when policy fails.
  mode: enforce          # warn | enforce
```

Companion CRD for opting namespaces into enforcement (policy-controller uses a label):

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: production
  labels:
    policy.sigstore.dev/include: "true"
```

Without the label, the policy is not evaluated for the namespace. This lets you roll out policy gradually — first to `policy-test`, then to `staging`, then to `production`.

Behavior notes:

- **OR within authorities.** If you list two authorities, the image needs to match at least one. Use this for keyless-OR-kms fallback.
- **AND within an authority's attestations.** All attestations listed in an authority block must verify.
- **glob matches.** policy-controller uses globs in `images.glob`, not regexp. `**` matches across path segments.
- **mode: warn.** Policy violations are logged but do not reject. Use during rollout, then flip to `enforce`. Pair with metrics (see §34) to watch the would-have-rejected rate.

---

## 23. Kyverno verifyImages YAML in Depth

Kyverno's `verifyImages` rule lives in a normal `ClusterPolicy`. Source: https://github.com/kyverno/kyverno. An equivalent of the above policy:

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-signed-images
spec:
  validationFailureAction: Enforce        # Enforce | Audit
  webhookTimeoutSeconds: 30
  failurePolicy: Fail                     # see pitfalls (§35) before changing

  rules:
  - name: verify-ghcr-myorg
    match:
      any:
      - resources:
          kinds: [Pod]
    verifyImages:
    - imageReferences:
      - "ghcr.io/myorg/*"
      mutateDigest: true                  # tag → digest substitution (§24)
      verifyDigest: true
      required: true                      # at least one signature must match
      attestors:
      - entries:
        - keyless:
            issuer: https://token.actions.githubusercontent.com
            subject: 'https://github.com/myorg/*/.github/workflows/*@refs/heads/main'
            rekor:
              url: https://rekor.sigstore.dev
              ignoreTlog: false
            ctlog:
              ignoreSCT: false
      attestations:
      - type: https://slsa.dev/provenance/v1
        attestors:
        - entries:
          - keyless:
              issuer: https://token.actions.githubusercontent.com
              subject: 'https://github.com/myorg/*/.github/workflows/*@refs/heads/main'
        conditions:
        - all:
          - key: '{{ buildDefinition.externalParameters.workflow.repository }}'
            operator: Equals
            value: https://github.com/myorg/myrepo
          - key: '{{ buildDefinition.externalParameters.workflow.ref }}'
            operator: Equals
            value: refs/heads/main
          - key: '{{ runDetails.builder.id }}'
            operator: AnyIn
            value:
            - 'https://github.com/actions/runner-images'
            - 'https://github.com/slsa-framework/slsa-github-generator/.github/workflows/generator_container_slsa3.yml@refs/tags/v2.0.0'
      - type: https://cyclonedx.org/bom
        attestors:
        - entries:
          - keyless:
              issuer: https://token.actions.githubusercontent.com
              subject: 'https://github.com/myorg/*/.github/workflows/*@refs/heads/main'
```

Kyverno-specific knobs worth knowing:

- **`mutateDigest: true`.** Kyverno will resolve the tag at admission and rewrite the pod spec to use the digest. This is the §24 mutation.
- **JMESPath in `conditions[].key`.** The `{{ ... }}` is JMESPath over the attestation predicate. Kyverno also supports CEL in newer versions.
- **`required: true`.** Without this, an image with no signature is admitted (the rule "doesn't apply"). With it, missing signatures fail. This is the most common Kyverno verifyImages misconfiguration.
- **`validationFailureAction: Audit`.** Like policy-controller's `warn`. Reports show up via `PolicyReport` CRDs.

You can also require **manual public keys** (long-lived):

```yaml
attestors:
- entries:
  - keys:
      publicKeys: |
        -----BEGIN PUBLIC KEY-----
        MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE...
        -----END PUBLIC KEY-----
```

Or **Notary v2** signatures (an alternative to Sigstore):

```yaml
attestors:
- entries:
  - certificates:
      cert: |
        -----BEGIN CERTIFICATE-----
        MIIDazCCAlOgAwIBAgIUI...
        -----END CERTIFICATE-----
```

The Kyverno engine evaluates all of `attestors[]` as OR (any match) and `attestations[]` as AND (all required).

---

## 24. Tag-to-Digest Mutation at Admission

The most underrated single feature of admission-side image verification: **rewriting `image: nginx:1.27` to `image: nginx@sha256:abc...` before the pod is stored.**

```
Developer writes:                   Admission stores:
─────────────────────             ───────────────────────────────────────
spec:                              spec:
  containers:                        containers:
  - name: web                        - name: web
    image: nginx:1.27                  image: nginx@sha256:abc123def456...

                                   The tag is replaced with the digest
                                   that was current at admission time,
                                   AFTER signature verification.
```

Why this matters:

1. **Verification is over digests.** Without mutation, the kubelet pulls the tag at pull time. The tag may have changed since admission (registry race). With mutation, the pod spec contains the digest; the kubelet pulls the digest; the digest cannot change.

2. **Source-of-truth pinning.** Humans want to write `nginx:1.27` for readability. The cluster state, what GitOps reconciles, should be the digest. Mutation gives you both.

3. **Reproducibility.** A Deployment stored with a tag in spec re-resolves the tag on every pod creation (different pods might pull different content if the tag moves). A Deployment stored with a digest gives every pod the same content forever, until the spec is updated.

The flow:

```
1. User submits pod with image: ghcr.io/me/api:v1.2.3
2. Mutating admission webhook (Kyverno or policy-controller):
     a. HEAD ghcr.io/me/api:v1.2.3 → digest sha256:def...
     b. Run signature/attestation verification against sha256:def...
        (this is the verifyImages logic)
     c. If verified, mutate the pod spec:
          image: ghcr.io/me/api@sha256:def...
     d. Pass to validating webhooks / write to etcd.
3. Pod stored with image: ghcr.io/me/api@sha256:def...
4. Kubelet pulls by digest; content is content-addressable and cannot
   be tampered with mid-flight.
```

Kyverno enables this with `mutateDigest: true` in `verifyImages`. Policy-controller does it via a separate `ClusterImagePolicy.spec.mode: enforce` plus the policy is on by default for matched images.

Subtle but important: **the mutation must be in a mutating webhook that runs after image verification**. Kyverno does both in the same controller. If you split them across two webhooks, ordering matters (mutating runs before validating; image verification in a validating webhook will see the mutated image — that's correct — but a mutating webhook *after* image verification could re-rewrite to an unverified image).

The mirror-attack variant of this mutation: if your CI publishes to `ghcr.io/me/api` and an attacker poses as a registry mirror serving `mirror.evil.com/me/api`, and your admission rule globs `**`, the attacker's tag-to-digest resolution happens against the attacker's registry. **Always pin the registry in the glob**, e.g., `ghcr.io/me/**`, never `**`.

---

## 25. Vulnerability Scanning and Admission Gating

A signed image with a clear SBOM and a verified SLSA provenance can still ship a known CVE. Vulnerability scanning closes that gap. The two coordinates:

- **Build-time scanning.** Trivy / Grype / Snyk in CI; fail the build on critical CVEs. Lowest cost, lowest coverage (CVEs published *after* the build are missed).
- **Continuous scanning.** A controller in the cluster (or scheduled job in a central scanner) rescans running images periodically and reports findings against a vuln database (NVD, OSV, GHSA, vendor advisories).

Admission gating using continuous scanning:

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: block-critical-cves
spec:
  rules:
  - name: trivy-scan
    match:
      any:
      - resources:
          kinds: [Pod]
    context:
    - name: scanResult
      apiCall:
        method: GET
        url: "http://trivy-operator.scanner-system.svc/scan/{{ images.containers.[].name }}"
    validate:
      message: "Image has critical CVEs: {{ scanResult.criticals }}"
      deny:
        conditions:
          any:
          - key: "{{ scanResult.criticalCount }}"
            operator: GreaterThan
            value: 0
```

This is a sketch — the productionized version typically uses `trivy-operator` (https://github.com/aquasecurity/trivy-operator), which scans cluster-wide and produces `VulnerabilityReport` CRDs that Kyverno can read directly:

```yaml
context:
- name: vulnReport
  apiCall:
    method: GET
    urlPath: "/apis/aquasecurity.github.io/v1alpha1/namespaces/{{ request.namespace }}/vulnerabilityreports"
```

The three operational tensions:

1. **Fresh scan vs admission latency.** Scanning takes seconds-to-minutes; admission has a hard timeout (~10–30s). You scan **outside** of admission and admission reads cached results.
2. **CVE flood vs reality.** A debian:bookworm-based image will routinely have 50–200 CVEs, most low/medium and many irrelevant to your use. Pure "reject any CVE" policy is operationally untenable. Use severity-based thresholds plus VEX (§26).
3. **Ground truth drift.** Scanner X reports a CVE, scanner Y doesn't. Pick one source of truth; pinning to vendor advisories (RHEL, Chainguard) is more stable than NVD.

---

## 26. VEX: Vulnerability EXchange

The signal-to-noise problem with CVEs has a structural fix: **VEX** (Vulnerability EXchange). A VEX statement is a signed assertion that *some artifact is not affected by some CVE, even though the CVE seems to apply*.

```
Two formats:

CSAF VEX (OASIS CSAF 2.0 with VEX profile)
  https://docs.oasis-open.org/csaf/csaf/v2.0/
  XML/JSON; rich; used by Red Hat, Cisco, Siemens.

OpenVEX (openvex/spec)
  Minimal JSON; designed for Sigstore-native signing.
  Used by Chainguard, the OpenVEX project, npm advisories.
```

An OpenVEX statement:

```json
{
  "@context": "https://openvex.dev/ns/v0.2.0",
  "@id":      "https://openvex.dev/docs/example/vex-2026-05-23",
  "author":   "Security Team <secops@myorg.example>",
  "timestamp":"2026-05-23T12:00:00Z",
  "version":  1,
  "statements": [
    {
      "vulnerability": { "name": "CVE-2024-12345" },
      "products": [
        {
          "@id": "pkg:oci/api@sha256:def456...?repository_url=ghcr.io/myorg"
        }
      ],
      "status":     "not_affected",
      "justification": "vulnerable_code_not_in_execute_path",
      "impact_statement": "The vulnerable function is in a build-time-only utility, not present at runtime."
    }
  ]
}
```

Possible `status` values: `not_affected`, `affected`, `fixed`, `under_investigation`. Possible `justification` values for `not_affected` (per the spec): `component_not_present`, `vulnerable_code_not_present`, `vulnerable_code_not_in_execute_path`, `vulnerable_code_cannot_be_controlled_by_adversary`, `inline_mitigations_already_exist`.

The VEX statement is signed via `cosign attest --type openvex --predicate vex.json ...`, attached to the image. Vulnerability scanners (Trivy 0.50+, Grype, vexctl) can read attached VEX and suppress matching findings:

```bash
trivy image --vex repo ghcr.io/me/api@sha256:def...
# Trivy fetches VEX attestations from the image and applies them.
```

Why this matters at admission: without VEX, a policy that rejects critical CVEs will reject 80% of legitimate images because their base distros report inherited CVEs the application doesn't actually exercise. With VEX, the vendor (you, the publisher) signs an explicit "not affected" statement; the admission scanner trusts the VEX (because it's signed) and the policy passes. The signal-to-noise ratio of admission goes from 5% real-blocks to 80% real-blocks.

VEX is **not** an excuse to ignore CVEs. It is a way to make "we have triaged this CVE" auditable. Each VEX statement has an author and a signature — the trail of who decided what, when, is preserved.

---

## 27. Base Image Hygiene: Distroless, Chainguard, Scratch

The cheapest way to reduce supply-chain attack surface is to ship less software. The base image is where most of your CVEs come from; minimizing it is high-leverage.

```
SCRATCH (FROM scratch)
  Empty image. Your binary and nothing else.
  Use case: statically linked Go/Rust binaries.
  Pros: zero attack surface, no shell, no libc, smallest size.
  Cons: no debugging in-container; you can't exec a shell.

DISTROLESS (gcr.io/distroless/...)
  Google's distroless images: libc, CA certs, tzdata, and your runtime.
  No shell, no package manager, no busybox.
  Variants: base, cc, java, python3, nodejs, static.
  Pros: tiny, hardened, no shell-injection vectors.
  Cons: debug images (gcr.io/distroless/...:debug) exist but aren't
        meant for production.

CHAINGUARD IMAGES (cgr.dev/chainguard/...)
  Distroless-style images built from source on a daily cadence.
  Published with cosign signatures and SLSA L3 provenance by default.
  Pros: zero-CVE-by-default operationally; signed; current.
  Cons: paid for non-free tier; not all ecosystems covered.

ALPINE (alpine:3.20)
  Tiny (5MB) base; musl libc; busybox shell; apk package manager.
  Pros: small, fast.
  Cons: musl vs glibc compatibility surprises; not as hardened as
        distroless.

DEBIAN-SLIM (debian:bookworm-slim)
  Stripped-down Debian; ~30MB.
  Pros: full glibc compat, package availability.
  Cons: many CVEs inherited from base packages; large attack surface.

UBUNTU (ubuntu:24.04)
  Like debian-slim but bigger. Often the default; rarely the right choice.
```

Numbers for a typical Python app:

```
Base                              Image size    CVEs reported by Trivy (median)
─────────────────────────────────  ───────────  ───────────────────────────────
python:3.12                         1.0 GB        ~200 (mostly low/medium)
python:3.12-slim                    140 MB         ~50
gcr.io/distroless/python3-debian12  60 MB           ~5
cgr.dev/chainguard/python:latest   45 MB            0–2
```

The supply-chain principle: every CVE in a base image is a CVE you didn't write but you ship. Smaller base = fewer CVEs to triage, fewer VEX statements to maintain, fewer signed promises to keep current.

The corollary: **if you use a base image, sign your verification of that base image at build time**. The `resolvedDependencies` field of SLSA Provenance v1 (§17) names the base image digest; you can write a policy that the listed base image digest must be one of an approved list. This is the closest you get to "vetted base images" as a structural property.

---

## 28. The cosign UX Gap

Every adoption story has the same pain point: developers don't think in cosign. They think in Docker. The cosign CLI is excellent for security engineers and unrecognizable to application developers. If signing requires devs to run `cosign sign`, signing will not happen.

The fix is to make signing invisible:

```
Developer workflow:
  $ git push origin main

CI workflow (entirely automated):
  1. Build image
  2. Push image
  3. cosign sign (keyless OIDC, no key management)
  4. Generate SBOM
  5. cosign attest SBOM
  6. Generate SLSA provenance (via reusable workflow)
  7. cosign attest provenance

Admission workflow (entirely automated):
  Pod create → policy-controller/Kyverno verifies → admit or reject

Developer sees:
  - Pull request opened
  - CI passes (or fails with a clear "couldn't sign" message)
  - Deploy succeeds (or fails with "image not signed by trusted CI")
```

Three UX investments that pay back:

1. **Make the signing workflow a reusable, well-named Action / Pipeline.** A team should opt-in by adding `uses: myorg/sign-and-deploy@v1`, not by copying YAML. Single source of truth, single place to fix bugs.

2. **Make the admission rejection message useful.** "ImagePullBackOff" is the kubernetes default for everything; "Pod rejected by Kyverno: image ghcr.io/team/api:v1.2.3 has no signature from a trusted identity (expected subject matching `^https://github.com/myorg/.+@refs/heads/main$`)" tells the developer exactly what to do.

3. **Surface CI signing failures as PR comments.** If `cosign sign` fails because the OIDC permission is missing, the dev should see "Add `permissions: id-token: write` to your workflow" as a PR comment, not as a stack trace in CI logs.

The cultural pattern: signing is a platform team's deliverable, not an application team's chore. The application team's contract is "use the build action; ship the image". The platform team's contract is "all images built by the build action are signed, attested, and verifiable; admission enforces it."

---

## 29. Air-Gapped Supply Chain

Sigstore is online infrastructure. In air-gapped environments — banks, governments, defense, certain critical infrastructure — you cannot call `fulcio.sigstore.dev` or `rekor.sigstore.dev`. The supply chain still works, but you run the infrastructure yourself.

```
┌─────────────────────────────────────────────────────────────────────┐
│  Internal Sigstore deployment (an air-gapped cluster)              │
│                                                                     │
│    ┌─────────────────┐    ┌─────────────────┐                      │
│    │ Internal Fulcio │    │ Internal Rekor  │                      │
│    │ ca:             │    │ tlog signing    │                      │
│    │  offline root + │    │ key in KMS      │                      │
│    │  online intermed│    │                 │                      │
│    └────────┬────────┘    └────────┬────────┘                      │
│             │                       │                              │
│             └──────────┬────────────┘                              │
│                        │                                           │
│                        ▼                                           │
│              ┌──────────────────┐                                 │
│              │  Internal TUF    │                                 │
│              │  root mirror     │                                 │
│              │  (signed by      │                                 │
│              │   offline keys)  │                                 │
│              └──────────────────┘                                 │
└─────────────────────────────────────────────────────────────────────┘
                        ▲
                        │ COSIGN_TUF_MIRROR=https://tuf.internal/
                        │ COSIGN_OIDC_ISSUER=https://oidc.internal/
                        │ --fulcio-url=https://fulcio.internal/
                        │ --rekor-url=https://rekor.internal/
                        │
                ┌───────┴────────┐
                │ Internal CI    │
                │ (Tekton, etc)  │
                └────────────────┘
```

The components to deploy:

1. **TUF mirror.** Generate offline root keys (HSM, multi-person ceremony), use `tuf` CLI (https://github.com/theupdateframework/go-tuf) to publish the metadata. Distribute the trusted root via your existing software distribution.

2. **Fulcio.** Helm chart: https://github.com/sigstore/helm-charts/tree/main/charts/fulcio. Configure trusted OIDC issuers (your internal IdP — Keycloak, Dex, etc). Configure CA: either a self-signed CA, or chained to your enterprise PKI.

3. **Rekor.** Helm chart: https://github.com/sigstore/helm-charts/tree/main/charts/rekor. Backed by a SQL database (MySQL/PostgreSQL) and a Trillian backend.

4. **Internal OIDC issuer.** Whatever your CI talks to. Tekton + Dex is common; GitLab self-hosted is a popular shortcut.

5. **Configuration distribution.** Every cosign invocation needs `--fulcio-url`, `--rekor-url`, `--tuf-mirror`, `--oidc-issuer`. Bake these into your CI templates and into the cluster's policy-controller / Kyverno config.

Offline signing for fully disconnected environments uses key-based signing (`cosign sign --key`) plus offline Rekor (`--tlog-upload=false`). The trade-off: no transparency log, no detectable misuse, manual key rotation. Reserve for the genuinely-offline case.

The harder problem in air-gapped: **mirroring third-party signatures**. When you mirror `docker.io/library/redis` into your internal registry, you must also mirror its signature artifacts and its SBOM/provenance attestations. Tools: `cosign copy`, `oras copy`, registry-to-registry replication (Harbor, JFrog). The signature artifact is its own OCI artifact; a naive mirror that copies only the image manifest leaves you without a signature to verify.

```bash
# Copy image + all referrers (signatures, attestations) from public to internal
cosign copy --force \
  docker.io/library/redis:7.2 \
  myregistry.internal/library/redis:7.2

# oras alternative
oras cp -r docker.io/library/redis:7.2 myregistry.internal/library/redis:7.2
```

---

## 30. Multi-Arch Signing

A modern image tag is usually a manifest *list* (OCI Image Index), not a single manifest. `nginx:1.27` resolves to an index with entries for linux/amd64, linux/arm64, linux/arm/v7, sometimes ppc64le, s390x.

```
ghcr.io/me/api:v1.2.3
  └── index manifest sha256:aaa...
       ├── linux/amd64 → manifest sha256:bbb...
       ├── linux/arm64 → manifest sha256:ccc...
       └── linux/arm/v7 → manifest sha256:ddd...
```

cosign 2.x signs the **index** by default:

```bash
cosign sign ghcr.io/me/api:v1.2.3
# signs sha256:aaa... (the index)
```

The result is one signature, on the index digest. At admission, when a kubelet on an amd64 node resolves the tag, it gets manifest `bbb`. The signature is on `aaa`, not on `bbb`. Verification with default cosign correctly walks the index and verifies against `aaa`. But policy engines vary: some verify against the platform-specific manifest the node will pull; some verify against the index.

To be safe in heterogeneous-arch environments, sign both:

```bash
cosign sign --recursive ghcr.io/me/api:v1.2.3
# signs the index AND each per-arch manifest
```

Verification side: both policy-controller and Kyverno support multi-arch correctly by default in current versions. But check your config:

```yaml
# Kyverno - default behavior verifies whatever digest is in pod.spec
# (the kubelet resolves to a per-arch digest before pulling)

# policy-controller - resolves to platform-matching child manifest by default
```

The pitfall: signing only the index, and an admission engine that verifies only the per-arch child manifest, results in 100% rejection. The pitfall happens during the upgrade where you transition multi-arch images and forget to re-sign with `--recursive`.

---

## 31. Provenance Pinning Across the SDLC

A signed image is one link in a chain. The chain needs to extend down to the deployment artifact:

```
git commit (signed)                      gitsign / GPG
   │
   ▼
build (provenance attestation)           cosign + Fulcio
   │
   ▼
image digest                             content-addressable
   │
   ▼
Helm chart / Kustomize bundle            cosign sign-blob, cosign artifact sign
referencing image@digest
   │
   ▼
GitOps repo (signed Helm chart digest)   gitsign on the chart commit
   │
   ▼
ArgoCD/Flux reconciliation               trust-from-repo verification
   │
   ▼
cluster admission (verify image signature) policy-controller/Kyverno
   │
   ▼
kubelet pulls by digest
```

Each step pins the next by digest:

- The Helm chart references `image@sha256:def...`, not `image:v1.2.3`.
- The Helm chart itself is signed (cosign signs the OCI helm chart artifact).
- The git commit that pushes the chart to GitOps is signed (gitsign uses Fulcio identities for git commit signing).
- ArgoCD/Flux verifies the chart signature before rendering and applying.
- The cluster admission webhook verifies the image signature before running.

The chain is end-to-end: a malicious actor needs to compromise *each* link. Each link is signed by a different identity (the developer's gitsign cert, the CI's Fulcio cert, the platform team's KMS key for chart signing, etc.). Compromising one identity yields zero rather than the entire pipeline.

The maturity ladder for SDLC pinning:

1. Application image signed at build, verified at admission.
2. Application image digest pinned in Helm chart values.
3. Helm chart signed and digest-pinned in GitOps repo.
4. GitOps repo enforces signed commits (gitsign).
5. ArgoCD/Flux verifies chart signature on render.
6. Source repo enforces signed commits + branch protection.

Most organizations reach 1–2; 3–6 require organizational buy-in across teams and are gated by GitOps and dev workflow maturity (chapter 31 dives into the GitOps piece).

---

## 32. The Signing Ladder: L0 to L4

A pragmatic adoption ladder, calibrated to what a team can land in a quarter:

```
L0  PIN BY DIGEST (no signing)
    image: nginx@sha256:abc...
    Cost: zero. Tools: kubectl.
    Buys: immutability. An attacker can't swap content under a fixed
          digest; you'd at least notice the rejection.
    Doesn't buy: identity, provenance.

L1  KEYLESS COSIGN ON EVERY CI BUILD
    Add cosign-installer + cosign sign step to every CI workflow.
    Cost: 1–2 weeks of pipeline edits + cosign training.
    Tools: cosign, sigstore-installer, GitHub Actions id-token: write.
    Buys: cryptographic binding image → CI identity. Rekor logs every
          signing event.
    Doesn't buy: enforcement. A user can still pull unsigned images.

L2  VERIFY-AT-ADMISSION WITH KYVERNO OR POLICY-CONTROLLER
    Deploy Kyverno or policy-controller; write ClusterImagePolicy for
    each registry path; start in audit mode; flip to enforce.
    Cost: 2–4 weeks (policy authoring + opt-in rollout per namespace).
    Tools: Kyverno or policy-controller.
    Buys: refusal to run unsigned images. The threat model from §1
          starts being defended.
    Doesn't buy: provenance verification, SBOM coverage.

L3  REQUIRE SBOM + PROVENANCE ATTESTATIONS
    Add syft/trivy SBOM generation to CI. Add cosign attest steps.
    Update Kyverno/policy-controller to require predicate types.
    Cost: 4–8 weeks (CI changes, vuln triage, VEX adoption).
    Tools: syft, trivy, cosign attest, VEX.
    Buys: known dependency tree, scannable for vulns; auditable
          build provenance.
    Doesn't buy: tamper-resistant build (the user could fake provenance
                 from a self-hosted runner).

L4  SLSA L3+ BUILD PLATFORM, REPRODUCIBLE BUILDS
    Switch to slsa-github-generator (or Tekton Chains, or equivalent
    reusable-workflow-based builder). Enforce reproducible builds for
    critical artifacts.
    Cost: ongoing (depends on existing CI maturity).
    Tools: slsa-framework/slsa-github-generator, tektoncd/chains.
    Buys: provenance the build user cannot forge.
    Doesn't buy: protection against source-side compromise.
```

The mistake to avoid: claiming L4 because you signed an image. The signing ladder is monotonic — you cannot skip rungs.

Most production-mature organizations reach **L2 with selective L3** for their highest-stakes services. L4 is rare outside hyperscalers and certain regulated industries.

---

## 33. Performance of Admission Verification

Admission verification adds latency to every pod create. The components of the latency budget:

```
Pod create latency overhead (cold, no caches):
  ┌──────────────────────────────────────────────────────────┐
  │ HEAD registry to resolve tag           ~30–80 ms         │
  │ GET signature OCI artifact             ~50–150 ms        │
  │   (one round-trip + Merkle resolution if referrers API)  │
  │ Verify ECDSA signature                  <1 ms            │
  │ Verify cert chain (Fulcio root)        ~5 ms (cached)    │
  │ Verify Rekor inclusion proof           ~20–60 ms         │
  │   (one round-trip to Rekor unless cached in bundle)      │
  │ GET attestation OCI artifact           ~50–150 ms        │
  │ Verify attestation cert + Rekor        ~20–60 ms         │
  │ CUE/JMESPath/CEL policy evaluation      <10 ms           │
  └──────────────────────────────────────────────────────────┘
  Cold total:  ~150–500 ms per image, per pod-create
  Warm total:  ~10–50 ms (digest cache hit, signature cache hit)
```

With multiple containers per pod (sidecars, init containers), the latency multiplies; with Kyverno's caching, repeat verifications of the same digest are nearly free.

Caching strategies that production deployments rely on:

```
LAYER 1: digest-resolution cache
  HEAD /v2/me/api/manifests/v1.2.3 → sha256:def...
  Cache for TTL (~60 sec). The tag could move, so don't cache forever.

LAYER 2: signature-verification cache
  Key:   (image digest, policy identity)
  Value: pass / fail
  Cache for hours-to-days. The digest is immutable; the signature is
  immutable; the answer is stable. Invalidate only on policy change.

LAYER 3: TUF / Fulcio root cache
  Refresh on TUF schedule (Sigstore: every 6 hours).

LAYER 4: Rekor proof cache
  If the Rekor entry came with an inclusion proof, the proof itself
  is cached forever (it's signed by Rekor with a timestamp).

LAYER 5: Attestation predicate cache
  Same key as L2; cache for the same duration.
```

Kyverno's default caches give an order-of-magnitude reduction in steady state. Policy-controller has similar caching.

**Outage mode.** What happens when Rekor or Fulcio is unreachable?

- Default policy-controller / Kyverno: **fail the admission**, because the policy required Rekor verification and it could not be completed.
- Operational impact: pod creates stop globally during a Sigstore outage.
- Mitigation: use the signature bundle format (`cosign 2.x` default), which embeds the Rekor inclusion proof and signed timestamp at signing time. Verification can then complete offline. This makes Rekor an availability dependency at signing time, not at deploy time.

The big architectural choice: **online vs offline verification**. Offline verification (using bundles) is strictly better for cluster availability; the trade-off is that you trust the proof captured at signing time, with no post-hoc audit. Modern Sigstore deployments use bundles + periodic Rekor checks (an offline auditor that scans Rekor for anomalies).

---

## 34. Observability: Reports, Rekor Monitoring, CI Metrics

A signing system you don't monitor is a system you don't have.

**Cluster side: policy reports.**

Kyverno emits `PolicyReport` and `ClusterPolicyReport` CRDs for every verification event:

```yaml
apiVersion: wgpolicyk8s.io/v1alpha2
kind: PolicyReport
metadata:
  name: cpol-require-signed-images
  namespace: default
results:
- policy: require-signed-images
  rule:   verify-ghcr-myorg
  status: fail
  resources:
  - apiVersion: v1
    kind: Pod
    name: legacy-app-7b88d4f8c-x2gs8
  message: "image ghcr.io/oldteam/legacy: no matching signatures found"
summary:
  pass: 142
  fail: 3
  warn: 0
  error: 0
  skip: 0
```

Use this for the "what's running that wouldn't pass policy" question. Combined with `validationFailureAction: Audit`, you can run a policy in observe-only mode for weeks and quantify the rollout blast radius before flipping to `Enforce`.

policy-controller emits Kubernetes Events for policy decisions:

```
LAST SEEN   TYPE      REASON          OBJECT                       MESSAGE
2m          Warning   PolicyViolation pod/legacy-7b88d4f8c-x2gs8   image ghcr.io/oldteam/legacy@sha256:abc: no matching attestations
```

**Rekor side: transparency-log monitoring.**

Rekor-monitor (https://github.com/sigstore/rekor-monitor) runs a daemon that:

1. Fetches Rekor's signed tree head periodically.
2. Verifies tree consistency (no log forks).
3. Scans new entries for identities matching a watchlist (e.g., all identities under `https://github.com/myorg/`).
4. Alerts when an unexpected identity signs (e.g., a workflow run on a branch you didn't expect).

For paranoid environments, run your own monitor over the public Rekor. For air-gapped, run a monitor over your internal Rekor.

**CI side: signing metrics.**

The minimum metrics every CI signing pipeline should emit:

```
sigstore_cosign_sign_attempts_total{repo, workflow}
sigstore_cosign_sign_failures_total{repo, workflow, reason}
sigstore_cosign_sign_duration_seconds{repo, workflow}

sigstore_cosign_attest_attempts_total{repo, workflow, predicate_type}
sigstore_cosign_attest_failures_total{repo, workflow, predicate_type}

sbom_generation_duration_seconds{repo, generator}
sbom_components_total{repo}
```

The two failure modes to alert on:

1. **Signing failure rate above ~1%.** Usually a missing `id-token: write` permission, or a Fulcio outage. Either way: something upstream is unsigned.
2. **Verification rate vs signing rate divergence.** If CI signs N images and admission verifies M, M < N means either (a) signed images aren't being deployed (fine), or (b) admission is checking a different identity than CI is signing with (config drift; not fine).

**Cluster admission metrics.**

```
kyverno_policy_results_total{policy, rule, resource_kind, result="pass|fail|warn"}
kyverno_admission_review_duration_seconds{policy}
policy_controller_admission_decisions_total{policy, decision="allow|deny"}
policy_controller_verification_cache_hits_total
policy_controller_verification_cache_misses_total
```

A canonical SLO: 99% of pod creates complete admission verification in <500ms; 99.9% in <2s. If the metric blows out, suspect (a) cache miss storm, (b) registry/Rekor latency, (c) policy regex explosion.

---

## 35. Pitfalls

The list of mistakes every adoption hits at least once.

1. **Signing the tag instead of the digest.** `cosign sign image:v1.2.3` signs the *current digest behind that tag*. If the tag is rewritten tomorrow, the signature is orphaned. The signature is still valid for the old digest, useless for new tag content. Cosign does this transparently, but if you build tooling that treats the tag as the signed thing, you'll be surprised. Always think in digests; signatures attach to digests; tags are an index.

2. **cert-identity policy too permissive.** Writing `--certificate-identity-regexp '.*'` accepts any Fulcio cert from any issuer. This is the most-common-known CVE pattern in keyless adoption. Anchor every regex with `^...$`; pin the issuer; pin the branch.

3. **failurePolicy=Ignore on the verify webhook.** Kyverno and policy-controller default to `failurePolicy: Fail`, which means "webhook unreachable → reject". Setting `Ignore` for "availability" means a webhook outage silently admits every pod, including unsigned ones. If you must trade availability, do it knowingly with monitoring on the bypass rate.

4. **Not verifying attestations (only signatures).** A signature attests "came from CI"; the attestation attests "came from this CI workflow on this branch with these inputs". A policy that requires only signatures is satisfied by any signed image by any CI identity, which on a public Fulcio includes every public GitHub repo. Always pair `verifyImages` (signature) with `attestations[]` (provenance/SBOM) and `conditions[]` (predicate constraints).

5. **CA bundle for Fulcio out of date.** The TUF root for Sigstore rotates intermediates periodically. If your cluster is air-gapped and you mirrored the TUF metadata once and never refreshed, your verifier eventually trusts the wrong intermediates and rejects valid signatures (or, worse, accepts revoked ones). Schedule a TUF refresh job; `cosign initialize` is the recommended periodic operation.

6. **Rekor outage blocking deploys.** Default cosign 1.x verification was online to Rekor. Cosign 2.x writes bundles with embedded inclusion proofs. Verify using the bundle (offline-verifiable); cache TUF root locally. Without this, a Rekor outage halts your cluster's pod creates.

7. **Not signing base images you pull in.** Your policy requires signed images, your base image is `debian:bookworm` from Docker Hub. Debian doesn't sign with your identity. Either (a) carve out a base-image exception in policy (precisely; only specific registries), (b) re-publish base images into your own registry with your own signature after vetting, or (c) move to a base image distributor that does sign (Chainguard, distroless with cosign coverage).

8. **SBOM contains internal hostnames.** Syft happily includes filesystem paths and environment variables in some predicate fields. If your build environment has `GIT_URL=https://internal-gitea.corp.example/team/repo.git`, that string lands in the SBOM, which is published next to the image, which is in the registry, which may be public. Treat SBOMs as semi-public; scrub or redact before publishing.

9. **Signing webhook in kube-system creates a chicken-and-egg.** If your image-verification webhook itself requires signed images to start, and the cluster is bootstrapping, no pods can start, including the webhook. Standard fix: namespace selector excludes `kube-system` and the policy controller's own namespace. policy-controller and Kyverno both ship namespace-exclusion defaults; verify they're in effect for your installs.

10. **Pinned digest stale and unpatched.** Pin-by-digest is a security control; it is also an obstacle to patching. If your Deployment pins `nginx@sha256:abc...` and that digest has a critical CVE published yesterday, GitOps does nothing until someone updates the pin. Pair pinning with a renovation bot (renovate, dependabot) that opens PRs to bump pinned digests.

11. **Tag-to-digest mutation accepting digests from arbitrary registries.** Your Kyverno policy mutates `image: nginx:1.27` → `image: nginx@sha256:...`. If you don't constrain the registry in the rule's `imageReferences[]`, an attacker can submit `image: my-evil-registry.example.com/nginx:1.27` and your mutation will dutifully resolve it. Always pin the registry: `imageReferences: ["docker.io/library/nginx", "ghcr.io/myorg/*"]`, never `["*"]`.

12. **Air-gap registry not mirroring signatures.** You mirror `docker.io/library/redis` into `registry.internal/redis` but the mirror tool only copied the image manifest, not the `.sig` referrer. Admission checks for a signature, finds none, rejects. Use `cosign copy` or `oras cp -r` (recursive), or configure Harbor / Artifactory to replicate referrers.

13. **Pull-through cache bypassing verification.** A pull-through cache (Harbor proxy cache, Artifactory remote repo) serves `docker.io/library/redis:7.2` from a local cache without re-verifying upstream signatures. Worse: a poisoned cache entry persists. Either configure the cache to verify signatures on cache fill, or disable pull-through for security-critical paths.

14. **Manual public-key rotation drift.** Long-lived `cosign.pub` is pinned in a Kyverno policy YAML. The key is rotated. Three weeks later, three clusters still have the old key. Verification fails for newly signed images. Use config management (GitOps) for policy YAML; alert on policy-controller / Kyverno parsing errors; rotate keys gradually with overlap windows.

15. **Treating L1 (signed) as L4 (provenance).** Your dashboard says "100% of images signed". You believe that means SLSA L3. It does not. Signed-images-as-a-metric tells you "the CI ran cosign sign"; it tells you nothing about who that CI was, what it built, whether the source matched. Track SLSA level per service explicitly; don't conflate signed with provenanced.

16. **OIDC subject collision.** Two different OIDC issuers can mint tokens with identical `sub` claims. If your policy checks only `sub` and not `iss`, an attacker who controls *any* OIDC issuer can sign as you. The Fulcio cert encodes both as separate extensions; the policy must compare both. cosign's `--certificate-oidc-issuer` is non-optional in practice.

17. **Trusting the kubelet to verify.** The kubelet, by default, does not verify signatures. Image-pulling is content-addressable but content-addressability is not signing — it ensures the bytes match a digest, not that the digest matches an identity. Verification has to happen at admission (before the kubelet pulls) or as a separate kubelet plugin (`cri-o image-signature-policy` for cri-o; less commonly used).

18. **Re-signing on every merge to main.** A team CI re-tags and re-pushes the same content to the same digest. The digest is identical; the signature is re-uploaded; everything looks fine. Then someone does a force-rebuild that produces a different content. The old signature is still in the registry. cosign's behavior: it appends signatures, so multiple signatures may exist for the same digest. Policy is "at least one signature matches", so this is usually fine; but for forensics, you may want only one signature per digest. Use `cosign clean` periodically or enforce single-signature policy with care.

19. **Forgetting to delete the .sig artifact when GC'ing the image.** The image gets GC'd from the registry. The signature artifact (separate OCI artifact) does not. Now your registry has orphan signatures. Worse: a future image pushed with the same digest (rare but possible across registries) inherits the orphaned signature. Registry GC should follow referrers; modern Harbor and ECR do this; older registries don't.

20. **Policy in audit mode forever.** You roll out the policy in `audit` / `warn` mode. You watch the report. You see violations. You document the plan to flip to `enforce`. Eighteen months later, the policy is still in audit; ops never wanted to be the one who broke deploys. The pattern: have an explicit, calendared cutover from audit → enforce, with executive sponsorship for the breakage that follows.

21. **Long verification timeouts that hide an outage.** Webhook timeout 30s; signature verification taking 25s due to a Rekor slowdown means every pod create is 25s slower. The webhook hasn't failed; it's just slow. Set realistic timeouts (3–5s), alert on p95/p99 latencies, and watch the bundle-verification fast path.

22. **Believing "we use Chainguard images" replaces signing.** Chainguard images are excellent base images. They're signed by Chainguard. Your application image, built on top of them, is a different artifact that needs its own signature. The base-image signature is a property of `cgr.dev/chainguard/python`, not of `ghcr.io/myorg/my-app`.

23. **The webhook itself isn't policy-protected.** Your Kyverno deployment lives at `kyverno/kyverno`. An attacker with cluster-admin who finds an unpatched RCE creates a malicious `ValidatingWebhookConfiguration` that disables image verification, then deploys whatever they want. Defense-in-depth: ValidatingAdmissionPolicy that forbids modification of the image-verification webhook config; auditd alerts on `webhookconfigurations.admissionregistration.k8s.io` writes.

24. **VEX statements signed by the wrong identity.** A VEX statement saying "we are not affected by CVE-X" carries authority because of its signer. If the VEX is signed by the same automated CI identity that signs the image (and the CI is publicly known to be a build identity, not a security identity), the VEX is weak. VEX statements should be signed by a *security* identity — a separate CI workflow, a different Fulcio subject, a KMS key held by the security team.

25. **Ignoring the recursive flag for multi-arch.** `cosign sign image:tag` signs the index. The kubelet on a node pulls a per-arch child manifest. Some admission engines verify the child digest, not the index. Result: signed at index level, rejected at admission. Always use `cosign sign --recursive` for multi-arch publication.

26. **Identity changes silently breaking deploys.** GitHub Actions changes the URL format for workflow identities. Or you reorganize your repos (e.g., move from `myorg/api` to `myorg/services/api`). The Fulcio SAN changes; the admission policy's regex doesn't; every deploy fails Wednesday morning. Make identity policies a first-class deployable, monitor reject rate, treat any sudden change as a config drift incident.

27. **No off-ramp.** Production has a hot incident; the team needs to deploy a hotfix; cosign signing is broken because of an upstream Fulcio incident. There must be an emergency-break path: a "platform team admin can sign with KMS key X" or "this single namespace temporarily allows image verification audit mode". Document, drill, and audit the break-glass path. The alternative is teams disabling the policy permanently after one bad night.

28. **Confusing image immutability with image safety.** Pinning to `image@sha256:def...` is great. It does not say the image is safe to run. It says it's the bytes you pinned. If those bytes contained a backdoor at pin time, they still do. Pinning + signing + scanning are layered; you need all three.

---

## 36. TL;DR

A modern Kubernetes supply chain has four artifacts and one verifier:

```
SOURCE COMMIT     →    PROVENANCE        →    SBOM              →    SIGNED IMAGE
(gitsign / GPG)        (SLSA Provenance        (CycloneDX or         (cosign sig over
                        v1, signed via          SPDX, signed via      image digest, via
                        cosign attest)          cosign attest)        Fulcio + Rekor)
                              │
                              └────────────────────┬──────────────────────────┘
                                                   ▼
                                        ADMISSION VERIFICATION
                                        (policy-controller or Kyverno
                                         verifies signature + cert
                                         identity + Rekor inclusion +
                                         required attestations,
                                         mutates tag → digest, admits
                                         or rejects)
```

**Sigstore** does keyless signing: cosign asks Fulcio for a short-lived X.509 cert bound to an OIDC subject (typically a CI workflow identity), signs the image digest with the cert's private key, logs the event to Rekor for transparency, discards the private key, and uploads the signature as an OCI artifact next to the image. TUF distributes the trust root so the verifier knows whose Fulcio to trust. The whole flow takes 1–3 seconds and leaves no key material to steal.

**What gets signed** is always the **manifest digest**, never the tag. Tags are mutable; digests aren't; a signature on mutable content is a lie. Admission engines verify the digest; if your workflow trusts a tag, you have a tag-swap vulnerability.

**Identity policy** is the load-bearing knob. `--certificate-identity` and `--certificate-oidc-issuer` (and their regex variants) decide *which CI* you trust. Anchor every regex; pin the issuer; pin the branch. Permissive identity policies are the most common known weakness in keyless adoption.

**SBOMs** describe what's *in* the image. **SLSA Provenance** describes how the image was *built*. Both are signed via `cosign attest`, both follow the in-toto Statement format (subject + predicateType + predicate), both are stored as DSSE envelopes as OCI artifacts. Together with the image signature, they let admission demand "this image was built by my CI, from my source, with this dependency tree, and signed by an identity I trust."

**SLSA Build Track L1–L3** classifies the build platform's tamper-resistance. L1 generates provenance; L2 is hosted-and-signed; L3 makes the provenance unforgeable by the build user. Most production reaches L2; L3 requires reusable workflows (`slsa-github-generator`) or controllers (`Tekton Chains`) that the build user can't suborn.

**Admission-time verification** is where signing turns into enforcement. policy-controller (sigstore reference) or Kyverno (general policy engine, more popular) intercept pod creates, verify signatures and attestations against `ClusterImagePolicy` or `ClusterPolicy` rules, and admit or reject. Both support tag-to-digest **mutation**: rewrite `image: nginx:1.27` to `image: nginx@sha256:abc...` in the stored pod spec, so the kubelet pulls an immutable reference that matches what was verified.

**VEX** statements (CSAF or OpenVEX) let vendors signal "not affected" to vulnerability scanners, reducing CVE noise; **base image hygiene** (distroless, Chainguard, scratch) reduces inherited CVEs at the source; **vulnerability scanning** (Trivy, Grype, trivy-operator) is layered on top of signing for the orthogonal concern of "what's wrong with the code I signed".

**Performance** is acceptable: 50–200 ms per pod create in steady state with cosign 2.x bundles and Kyverno's verification cache. Worst-case bursts during cache miss can hit 500+ ms; Rekor outages can take it down indefinitely unless you use bundle-embedded inclusion proofs and verify offline.

**The adoption ladder** is L0 (pin by digest) → L1 (sign in CI) → L2 (verify at admission) → L3 (require provenance + SBOM) → L4 (SLSA L3 build platform). Most teams need L2; only the highest-stakes services need L3 or L4. Don't claim L4 when you have L1.

**The cultural goal** is: signing is invisible to developers (CI does it; admission verifies it; rejection messages are actionable); signing is auditable to security (Rekor is monitored, policy violations are reported); signing is reversible in incidents (emergency KMS path exists, audit-mode rollouts are calendar-bounded).

The supply chain is no longer "we trust our registry". It is *every step from source to running container is signed by a known identity, logged in a transparency log, and verified at admission against a policy that names exactly who you trust for what.* That is the difference between hoping you weren't Solarwinded and being able to prove it.
