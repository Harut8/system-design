"""Give every page its own meta description.

Without this, all pages share `site_description`, which search engines treat as
duplicate content and replace with a random snippet. Pages that set
`description:` in front matter keep it; everything else gets its first prose
paragraph, stripped of Markdown and cut to snippet length.
"""

import re

MAX_LEN = 155

_SKIP = re.compile(r"^\s*(#|```|~~~|\||<|!\[|---|\*\*\*|- |\* |\d+\. |!!!|\?\?\?|\$\$)")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MARKUP = re.compile(r"[*_`~]|<[^>]+>")


def paragraphs(markdown: str):
    para: list[str] = []
    in_fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("## "):  # only the intro, before the first section
            break
        line = re.sub(r"^\s*>\s?", "", line)  # a lead-in blockquote is usually the summary
        if not line.strip():
            if para:
                yield " ".join(para)
                para = []
            continue
        if not para and _SKIP.match(line):
            continue
        para.append(line.strip())
    if para:
        yield " ".join(para)


def first_paragraph(markdown: str) -> str:
    # Skip header blocks like "Tier 2, doc 16. Prerequisites: 14-pyobject.md ..."
    # -- they cross-reference other files and say nothing about this page.
    for i, para in enumerate(paragraphs(markdown)):
        if i >= 5:
            break
        if ".md" not in para and not para.rstrip().endswith(":"):
            return para
    return ""


def to_description(text: str) -> str:
    text = _LINK.sub(r"\1", text)
    text = _MARKUP.sub("", text)
    # Themes insert this into an attribute without escaping it.
    text = text.replace('"', "'")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= MAX_LEN:
        return text
    cut = text[:MAX_LEN].rsplit(" ", 1)[0].rstrip(",;:—-")
    return cut + "…"


def on_page_markdown(markdown, page, config, **kwargs):
    if not page.meta.get("description"):
        description = to_description(first_paragraph(markdown))
        if len(description) < 40:
            # No usable intro (e.g. straight into a table of contents).
            description = to_description(f"{page.title}. {config['site_description']}")
        page.meta["description"] = description
    return markdown
