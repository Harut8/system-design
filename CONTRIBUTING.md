# Contributing

Corrections and improvements are welcome. The most useful contributions, roughly in order:

1. **Factual fixes.** A wrong number, an outdated default, or a mechanism that's described incorrectly.
   Please link the source (paper, source file and line, official docs).
2. **Broken labs.** A command that no longer works, or a result that doesn't match what you measured.
   Include your OS, versions, and the output you got.
3. **Clarity.** An explanation you had to read three times. Say which paragraph and what tripped you up;
   a rewrite is a bonus.
4. **Typos and broken links.** Just open the PR.

## How

- Small fixes: edit the file on GitHub (or use the ✏️ button on any page of the site) and open a PR.
- Bigger changes (a new section or chapter): open an issue first so we can agree on scope.
- Keep the existing style: plain Markdown, one chapter per file, numbered filename prefixes.

## External links

Prefer primary sources: papers, RFCs, official docs, source code. Third-party learning resources
(practice questions, courses, blog posts) are accepted when all of these hold:

- The linked content is free to read without an account.
- It adds substance beyond what the guide already says; the guide must still make sense without it.
- You disclose any affiliation with the site in the PR description.
- It sits on its own line at the end of a section, prefixed with *Further practice:*, never inline
  in an explanation. One link per contribution.

Maintainers may remove links that break, move behind a login, or change their terms.

## Previewing the site

```bash
pip install -r requirements-docs.txt
./scripts/stage-docs.sh && mkdocs serve
```

The build log lists any broken links or anchors. Please keep it clean.

## License

By contributing you agree that your contribution is licensed under [CC BY-SA 4.0](LICENSE) for notes
and [MIT](LICENSE-CODE) for code, matching the rest of the repository.
