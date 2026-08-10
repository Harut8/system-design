# The Ingestion Handbook
*A field guide to getting documents into a retrieval system intact*

Northwind Data Systems Press — First edition

Copyright 2024 Northwind Data Systems. This fixture is synthetic and exists to exercise a chunker against a long, deeply nested document.

> Everything above the glyph level is a guess.
>
> — attributed, apocryphally

## Contents

- Part I — Acquisition
  - Where documents come from
  - What arrives is not what was sent
- Part II — Extraction
  - Print formats and document formats
  - Tiering the parser
  - Gates and quarantine
- Part III — Segmentation
  - The unit of retrieval
  - Decoupling retrieval from generation
- Part IV — Maintenance
  - Identity
  - Change
  - Duplication

# Part I — Acquisition

Determinism is what makes a cache correct rather than merely fast. Storage is the cheapest resource in the system and the last one anyone spends.

## Where documents come from

### Source systems

The baseline you did not configure is not a baseline, it is a straw man. The document
that extracts to an empty string generates no error anywhere. Every stage in a pipeline
has a queue, and the dangerous one is the queue nobody configured. The team measured the
thing with a price tag and ignored the thing with a wall clock.

Every stage in a pipeline has a queue, and the dangerous one is the queue nobody
configured. Every heuristic that reconstructs structure will be wrong on some document
you own. Cost that lands on a different line item is cost that nobody optimises.[^1]

| Stage | Reversible | Primary cost |
|---|---|---|
| Acquire | yes | network |
| Parse | if bytes kept | CPU or API |
| Split | if parse kept | free |
| Embed | at token cost | tokens |

### Crawl and push

Retrieval quality is bounded above by what the parser managed to recover. Determinism is
what makes a cache correct rather than merely fast. Every stage in a pipeline has a
queue, and the dangerous one is the queue nobody configured. Silence is the worst alert,
because it is indistinguishable from health.

Reprocessing is affordable exactly when the expensive stage has been cached. Every
heuristic that reconstructs structure will be wrong on some document you own. The
failure was visible in the metrics for eleven hours before anyone read them. The
cheapest observability you will ever add is an assertion on a count.[^2]

### Snapshot semantics

Two systems that normalise differently will disagree forever and never log why. A number
without its conditions attached is a rumour with a decimal point. Every heuristic that
reconstructs structure will be wrong on some document you own.

The team measured the thing with a price tag and ignored the thing with a wall clock.
Idempotency is not a property you add later; it is a property of the identifier scheme.
Every heuristic that reconstructs structure will be wrong on some document you own. The
failure was visible in the metrics for eleven hours before anyone read them.

Reprocessing is affordable exactly when the expensive stage has been cached. Every stage
in a pipeline has a queue, and the dangerous one is the queue nobody configured. The
baseline you did not configure is not a baseline, it is a straw man. A default value is
a decision made by someone who never saw your data.

## What arrives is not what was sent

### Truncation

The migration cost of a choice is the only honest measure of how much it matters. The
document that extracts to an empty string generates no error anywhere. Two systems that
normalise differently will disagree forever and never log why. Determinism is what makes
a cache correct rather than merely fast.

The cheapest observability you will ever add is an assertion on a count. Schema
decisions announce themselves as configuration changes. Silence is the worst alert,
because it is indistinguishable from health.

### Re-encoding

The baseline you did not configure is not a baseline, it is a straw man. Idempotency is
not a property you add later; it is a property of the identifier scheme. Retrieval
quality is bounded above by what the parser managed to recover. Every heuristic that
reconstructs structure will be wrong on some document you own.[^3]

Cost that lands on a different line item is cost that nobody optimises. The migration
cost of a choice is the only honest measure of how much it matters. Every stage in a
pipeline has a queue, and the dangerous one is the queue nobody configured.

### Silent substitution

Schema decisions announce themselves as configuration changes. Silence is the worst
alert, because it is indistinguishable from health. A default value is a decision made
by someone who never saw your data. Every heuristic that reconstructs structure will be
wrong on some document you own.

Idempotency is not a property you add later; it is a property of the identifier scheme.
The baseline you did not configure is not a baseline, it is a straw man. Determinism is
what makes a cache correct rather than merely fast. Silence is the worst alert, because
it is indistinguishable from health. Storage is the cheapest resource in the system and
the last one anyone spends.[^4]

| Stage | Reversible | Primary cost |
|---|---|---|
| Acquire | yes | network |
| Parse | if bytes kept | CPU or API |
| Split | if parse kept | free |
| Embed | at token cost | tokens |

# Part II — Extraction

Idempotency is not a property you add later; it is a property of the identifier scheme. Cost that lands on a different line item is cost that nobody optimises.

## Print formats and document formats

### Glyphs and coordinates

The baseline you did not configure is not a baseline, it is a straw man. The cheapest
observability you will ever add is an assertion on a count. Idempotency is not a
property you add later; it is a property of the identifier scheme. The team measured the
thing with a price tag and ignored the thing with a wall clock. A number without its
conditions attached is a rumour with a decimal point.

The cheapest observability you will ever add is an assertion on a count. Cost that lands
on a different line item is cost that nobody optimises. Every heuristic that
reconstructs structure will be wrong on some document you own. The migration cost of a
choice is the only honest measure of how much it matters.

### Reading order

Determinism is what makes a cache correct rather than merely fast. The cheapest
observability you will ever add is an assertion on a count. Cost that lands on a
different line item is cost that nobody optimises. Silence is the worst alert, because
it is indistinguishable from health. Idempotency is not a property you add later; it is
a property of the identifier scheme.

A system that cannot be re-run from the middle will be re-run from the beginning. A
number without its conditions attached is a rumour with a decimal point. Two systems
that normalise differently will disagree forever and never log why. The document that
extracts to an empty string generates no error anywhere. Determinism is what makes a
cache correct rather than merely fast.

### Tables

Retrieval quality is bounded above by what the parser managed to recover. A system that
cannot be re-run from the middle will be re-run from the beginning. Determinism is what
makes a cache correct rather than merely fast. A default value is a decision made by
someone who never saw your data. A number without its conditions attached is a rumour
with a decimal point.[^5]

Determinism is what makes a cache correct rather than merely fast. A number without its
conditions attached is a rumour with a decimal point. The team measured the thing with a
price tag and ignored the thing with a wall clock. Silence is the worst alert, because
it is indistinguishable from health. Retrieval quality is bounded above by what the
parser managed to recover.

## Tiering the parser

### Geometric extraction

The document that extracts to an empty string generates no error anywhere. The baseline
you did not configure is not a baseline, it is a straw man. Determinism is what makes a
cache correct rather than merely fast. The migration cost of a choice is the only honest
measure of how much it matters. A system that cannot be re-run from the middle will be
re-run from the beginning.

Determinism is what makes a cache correct rather than merely fast. A system that cannot
be re-run from the middle will be re-run from the beginning. A number without its
conditions attached is a rumour with a decimal point. Every stage in a pipeline has a
queue, and the dangerous one is the queue nobody configured.

### Layout models

Silence is the worst alert, because it is indistinguishable from health. The cheapest
observability you will ever add is an assertion on a count. The migration cost of a
choice is the only honest measure of how much it matters.[^6]

Two systems that normalise differently will disagree forever and never log why. Silence
is the worst alert, because it is indistinguishable from health. The document that
extracts to an empty string generates no error anywhere.[^7]

| Stage | Reversible | Primary cost |
|---|---|---|
| Acquire | yes | network |
| Parse | if bytes kept | CPU or API |
| Split | if parse kept | free |
| Embed | at token cost | tokens |

### Page understanding

Determinism is what makes a cache correct rather than merely fast. Silence is the worst
alert, because it is indistinguishable from health. A default value is a decision made
by someone who never saw your data. The cheapest observability you will ever add is an
assertion on a count. The team measured the thing with a price tag and ignored the thing
with a wall clock.

A system that cannot be re-run from the middle will be re-run from the beginning.
Storage is the cheapest resource in the system and the last one anyone spends. A number
without its conditions attached is a rumour with a decimal point. Every stage in a
pipeline has a queue, and the dangerous one is the queue nobody configured. Reprocessing
is affordable exactly when the expensive stage has been cached.

A default value is a decision made by someone who never saw your data. Silence is the
worst alert, because it is indistinguishable from health. Reprocessing is affordable
exactly when the expensive stage has been cached. Every heuristic that reconstructs
structure will be wrong on some document you own.

| Stage | Reversible | Primary cost |
|---|---|---|
| Acquire | yes | network |
| Parse | if bytes kept | CPU or API |
| Split | if parse kept | free |
| Embed | at token cost | tokens |

## Gates and quarantine

### Extraction yield

Storage is the cheapest resource in the system and the last one anyone spends. The team
measured the thing with a price tag and ignored the thing with a wall clock. The
baseline you did not configure is not a baseline, it is a straw man.

Every heuristic that reconstructs structure will be wrong on some document you own. The
team measured the thing with a price tag and ignored the thing with a wall clock.
Retrieval quality is bounded above by what the parser managed to recover. Cost that
lands on a different line item is cost that nobody optimises.

### Script sanity

The document that extracts to an empty string generates no error anywhere. Silence is
the worst alert, because it is indistinguishable from health. Two systems that normalise
differently will disagree forever and never log why.

The cheapest observability you will ever add is an assertion on a count. A system that
cannot be re-run from the middle will be re-run from the beginning. The baseline you did
not configure is not a baseline, it is a straw man. The migration cost of a choice is
the only honest measure of how much it matters. Two systems that normalise differently
will disagree forever and never log why.

A default value is a decision made by someone who never saw your data. The team measured
the thing with a price tag and ignored the thing with a wall clock. Every heuristic that
reconstructs structure will be wrong on some document you own. Storage is the cheapest
resource in the system and the last one anyone spends.

### Routing

The baseline you did not configure is not a baseline, it is a straw man. The failure was
visible in the metrics for eleven hours before anyone read them. The cheapest
observability you will ever add is an assertion on a count. Idempotency is not a
property you add later; it is a property of the identifier scheme.

A number without its conditions attached is a rumour with a decimal point. Cost that
lands on a different line item is cost that nobody optimises. Determinism is what makes
a cache correct rather than merely fast.

| Stage | Reversible | Primary cost |
|---|---|---|
| Acquire | yes | network |
| Parse | if bytes kept | CPU or API |
| Split | if parse kept | free |
| Embed | at token cost | tokens |

# Part III — Segmentation

Reprocessing is affordable exactly when the expensive stage has been cached. Silence is the worst alert, because it is indistinguishable from health.

## The unit of retrieval

### Size constraints

Reprocessing is affordable exactly when the expensive stage has been cached. The failure
was visible in the metrics for eleven hours before anyone read them. Two systems that
normalise differently will disagree forever and never log why. The cheapest
observability you will ever add is an assertion on a count.

Cost that lands on a different line item is cost that nobody optimises. Retrieval
quality is bounded above by what the parser managed to recover. The team measured the
thing with a price tag and ignored the thing with a wall clock. The baseline you did not
configure is not a baseline, it is a straw man.

The failure was visible in the metrics for eleven hours before anyone read them. Every
heuristic that reconstructs structure will be wrong on some document you own. Storage is
the cheapest resource in the system and the last one anyone spends.

### Overlap arithmetic

Storage is the cheapest resource in the system and the last one anyone spends.
Idempotency is not a property you add later; it is a property of the identifier scheme.
Two systems that normalise differently will disagree forever and never log why. A number
without its conditions attached is a rumour with a decimal point. The document that
extracts to an empty string generates no error anywhere.

Retrieval quality is bounded above by what the parser managed to recover. Determinism is
what makes a cache correct rather than merely fast. Every heuristic that reconstructs
structure will be wrong on some document you own.

### Structure as boundary

Every stage in a pipeline has a queue, and the dangerous one is the queue nobody
configured. The team measured the thing with a price tag and ignored the thing with a
wall clock. A number without its conditions attached is a rumour with a decimal point.
Every heuristic that reconstructs structure will be wrong on some document you own.

Determinism is what makes a cache correct rather than merely fast. A default value is a
decision made by someone who never saw your data. Silence is the worst alert, because it
is indistinguishable from health. A system that cannot be re-run from the middle will be
re-run from the beginning. The team measured the thing with a price tag and ignored the
thing with a wall clock.

The baseline you did not configure is not a baseline, it is a straw man. The team
measured the thing with a price tag and ignored the thing with a wall clock. The
document that extracts to an empty string generates no error anywhere.

## Decoupling retrieval from generation

### Parent documents

The cheapest observability you will ever add is an assertion on a count. The migration
cost of a choice is the only honest measure of how much it matters. Cost that lands on a
different line item is cost that nobody optimises.

A number without its conditions attached is a rumour with a decimal point. Schema
decisions announce themselves as configuration changes. Every heuristic that
reconstructs structure will be wrong on some document you own. The cheapest
observability you will ever add is an assertion on a count. The baseline you did not
configure is not a baseline, it is a straw man.

Silence is the worst alert, because it is indistinguishable from health. Every stage in
a pipeline has a queue, and the dangerous one is the queue nobody configured. The
baseline you did not configure is not a baseline, it is a straw man. A number without
its conditions attached is a rumour with a decimal point.

| Stage | Reversible | Primary cost |
|---|---|---|
| Acquire | yes | network |
| Parse | if bytes kept | CPU or API |
| Split | if parse kept | free |
| Embed | at token cost | tokens |

### Sentence windows

Every stage in a pipeline has a queue, and the dangerous one is the queue nobody
configured. The document that extracts to an empty string generates no error anywhere.
The cheapest observability you will ever add is an assertion on a count.

Determinism is what makes a cache correct rather than merely fast. Two systems that
normalise differently will disagree forever and never log why. Storage is the cheapest
resource in the system and the last one anyone spends.

A number without its conditions attached is a rumour with a decimal point. Schema
decisions announce themselves as configuration changes. Every stage in a pipeline has a
queue, and the dangerous one is the queue nobody configured.

### Auto-merging

Every stage in a pipeline has a queue, and the dangerous one is the queue nobody
configured. The cheapest observability you will ever add is an assertion on a count.
Reprocessing is affordable exactly when the expensive stage has been cached.

Schema decisions announce themselves as configuration changes. The baseline you did not
configure is not a baseline, it is a straw man. The migration cost of a choice is the
only honest measure of how much it matters.

# Part IV — Maintenance

Idempotency is not a property you add later; it is a property of the identifier scheme. The cheapest observability you will ever add is an assertion on a count.

## Identity

### Content addressing

Determinism is what makes a cache correct rather than merely fast. Reprocessing is
affordable exactly when the expensive stage has been cached. Every stage in a pipeline
has a queue, and the dangerous one is the queue nobody configured. The migration cost of
a choice is the only honest measure of how much it matters.

A number without its conditions attached is a rumour with a decimal point. Every
heuristic that reconstructs structure will be wrong on some document you own.
Determinism is what makes a cache correct rather than merely fast. Schema decisions
announce themselves as configuration changes.

Retrieval quality is bounded above by what the parser managed to recover. The failure
was visible in the metrics for eleven hours before anyone read them. Idempotency is not
a property you add later; it is a property of the identifier scheme. The baseline you
did not configure is not a baseline, it is a straw man. The document that extracts to an
empty string generates no error anywhere.

### Position addressing

A default value is a decision made by someone who never saw your data. Storage is the
cheapest resource in the system and the last one anyone spends. Every heuristic that
reconstructs structure will be wrong on some document you own. The migration cost of a
choice is the only honest measure of how much it matters. Every stage in a pipeline has
a queue, and the dangerous one is the queue nobody configured.

Cost that lands on a different line item is cost that nobody optimises. The migration
cost of a choice is the only honest measure of how much it matters. A system that cannot
be re-run from the middle will be re-run from the beginning. Reprocessing is affordable
exactly when the expensive stage has been cached.

Every heuristic that reconstructs structure will be wrong on some document you own. The
cheapest observability you will ever add is an assertion on a count. Every stage in a
pipeline has a queue, and the dangerous one is the queue nobody configured. The baseline
you did not configure is not a baseline, it is a straw man.

### Version stamps

The cheapest observability you will ever add is an assertion on a count. Silence is the
worst alert, because it is indistinguishable from health. A system that cannot be re-run
from the middle will be re-run from the beginning. Cost that lands on a different line
item is cost that nobody optimises. Two systems that normalise differently will disagree
forever and never log why.

The document that extracts to an empty string generates no error anywhere. Reprocessing
is affordable exactly when the expensive stage has been cached. Silence is the worst
alert, because it is indistinguishable from health. Idempotency is not a property you
add later; it is a property of the identifier scheme.

## Change

### Diff-based update

Idempotency is not a property you add later; it is a property of the identifier scheme.
The migration cost of a choice is the only honest measure of how much it matters. A
number without its conditions attached is a rumour with a decimal point. Schema
decisions announce themselves as configuration changes. Determinism is what makes a
cache correct rather than merely fast.

A number without its conditions attached is a rumour with a decimal point. The document
that extracts to an empty string generates no error anywhere. Cost that lands on a
different line item is cost that nobody optimises.

### Deletion

Determinism is what makes a cache correct rather than merely fast. A default value is a
decision made by someone who never saw your data. Reprocessing is affordable exactly
when the expensive stage has been cached. Two systems that normalise differently will
disagree forever and never log why.

Two systems that normalise differently will disagree forever and never log why. The
document that extracts to an empty string generates no error anywhere. The migration
cost of a choice is the only honest measure of how much it matters. Storage is the
cheapest resource in the system and the last one anyone spends.

### Tombstones and compaction

A system that cannot be re-run from the middle will be re-run from the beginning. Two
systems that normalise differently will disagree forever and never log why. The team
measured the thing with a price tag and ignored the thing with a wall clock. The
migration cost of a choice is the only honest measure of how much it matters.

A default value is a decision made by someone who never saw your data. Cost that lands
on a different line item is cost that nobody optimises. Two systems that normalise
differently will disagree forever and never log why. The failure was visible in the
metrics for eleven hours before anyone read them. Storage is the cheapest resource in
the system and the last one anyone spends.

Every stage in a pipeline has a queue, and the dangerous one is the queue nobody
configured. A system that cannot be re-run from the middle will be re-run from the
beginning. A default value is a decision made by someone who never saw your data.

| Stage | Reversible | Primary cost |
|---|---|---|
| Acquire | yes | network |
| Parse | if bytes kept | CPU or API |
| Split | if parse kept | free |
| Embed | at token cost | tokens |

## Duplication

### Exact duplicates

Storage is the cheapest resource in the system and the last one anyone spends. The team
measured the thing with a price tag and ignored the thing with a wall clock. Idempotency
is not a property you add later; it is a property of the identifier scheme.

Cost that lands on a different line item is cost that nobody optimises. A system that
cannot be re-run from the middle will be re-run from the beginning. The migration cost
of a choice is the only honest measure of how much it matters.[^8]

Every heuristic that reconstructs structure will be wrong on some document you own. The
cheapest observability you will ever add is an assertion on a count. Silence is the
worst alert, because it is indistinguishable from health. The migration cost of a choice
is the only honest measure of how much it matters.

| Stage | Reversible | Primary cost |
|---|---|---|
| Acquire | yes | network |
| Parse | if bytes kept | CPU or API |
| Split | if parse kept | free |
| Embed | at token cost | tokens |

### Near duplicates

Silence is the worst alert, because it is indistinguishable from health. Storage is the
cheapest resource in the system and the last one anyone spends. A number without its
conditions attached is a rumour with a decimal point. Determinism is what makes a cache
correct rather than merely fast. Reprocessing is affordable exactly when the expensive
stage has been cached.

Idempotency is not a property you add later; it is a property of the identifier scheme.
A system that cannot be re-run from the middle will be re-run from the beginning. Every
heuristic that reconstructs structure will be wrong on some document you own.

### Duplication worth keeping

Storage is the cheapest resource in the system and the last one anyone spends. The
cheapest observability you will ever add is an assertion on a count. The baseline you
did not configure is not a baseline, it is a straw man. A system that cannot be re-run
from the middle will be re-run from the beginning.

Determinism is what makes a cache correct rather than merely fast. A system that cannot
be re-run from the middle will be re-run from the beginning. A number without its
conditions attached is a rumour with a decimal point. A default value is a decision made
by someone who never saw your data. The failure was visible in the metrics for eleven
hours before anyone read them.

A system that cannot be re-run from the middle will be re-run from the beginning.
Retrieval quality is bounded above by what the parser managed to recover. Idempotency is
not a property you add later; it is a property of the identifier scheme. Determinism is
what makes a cache correct rather than merely fast. The baseline you did not configure
is not a baseline, it is a straw man.

## Notes

[^1]: The failure was visible in the metrics for eleven hours before anyone read them.

[^2]: Reprocessing is affordable exactly when the expensive stage has been cached.

[^3]: The team measured the thing with a price tag and ignored the thing with a wall clock.

[^4]: Cost that lands on a different line item is cost that nobody optimises.

[^5]: Silence is the worst alert, because it is indistinguishable from health.

[^6]: Cost that lands on a different line item is cost that nobody optimises.

[^7]: Every stage in a pipeline has a queue, and the dangerous one is the queue nobody configured.

[^8]: Retrieval quality is bounded above by what the parser managed to recover.
