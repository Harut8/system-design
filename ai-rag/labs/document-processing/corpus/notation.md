# Notation, Units, and Names

A reference sheet for the normalization stage. Every line below is a case where
the reflexive `unicodedata.normalize("NFKC", text).lower()` changes what the text
means rather than what it looks like.

## 1. Mathematical notation

The kinetic energy term is E = mc², and the constraint surface is x² + y² = r².
Under NFKC the superscript two becomes an ordinary digit, so x² collapses to x2 —
a different expression, silently.

The detector noise floor is 10⁻⁹ A/√Hz. Sample volumes are quoted in m³ and areas
in m². A ½ cup is not the same as a ¼ cup, and NFKC rewrites both into digit-slash
sequences that no longer sort or compare as quantities.

Concentration was 5 µM using the micro sign (U+00B5) and 5 μM using Greek small mu
(U+03BC). These are distinct codepoints that NFKC merges. Bond length was 1.54 Å.

## 2. Roman numerals

Schedule Ⅶ of the Act supersedes Schedule Ⅳ and amends Schedule Ⅻ. Those are the
Unicode Number Forms characters, not the ASCII letters. Written in ASCII the same
sentence reads: Schedule VII supersedes Schedule IV and amends Schedule XII.

A citation index that NFKC-folds one form and not the other will treat the two
sentences as unrelated in the lexical branch and as near-identical in the dense one.

## 3. Case carries entity signal

The US filing deadline differs from the one us contractors were given.
A Polish supplier will polish the housing before shipment.
IT owns the ticket, and it is assigned to the platform team.
Revenue in March exceeded plan; the auditors march through the ledger in April.
The figure was disclosed by Apple; the apple harvest was unaffected.
The vendor of record is SAP; sap ingress damaged two units.
Guidance was published by WHO, and nobody recorded who approved it.
The AI team shipped it, and the ai particle is unrelated.

Lowercasing merges every left-hand term into its right-hand homograph. BM25 wants
that folding; the dense branch does not (§4.3).

## 4. Full-width and compatibility forms

The invoice number was recorded as ＡＢＣ１２３ in the source system and as ABC123 in
the replica. Under NFKC these become identical, which is convenient right up to the
point where the two systems disagree about which record is authoritative.

## 5. Ligatures and invisible characters

The classiﬁcation of ﬂow is deﬁned in the oﬃce manual. Those three ligatures are
single codepoints; a BM25 query for classification will not match this line until
they are expanded, which is the one compatibility mapping you do want (§4.1).

This line contains a soft hyphen in the word docu­ment, a zero-width space in
cost​centre, and curly quotes around ‘identity’ rather than 'identity'. All three
are invisible in a rendered view and all three break exact lexical match.
