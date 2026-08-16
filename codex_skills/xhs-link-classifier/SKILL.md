---
name: xhs-link-classifier
description: Read an AirHub-prepared XHS text classification job, identify referenced Blog pages or arXiv papers, verify arXiv IDs, and write only the requested JSON result. Do not download XHS posts, modify cache files, or import manual lists.
---

# AirHub XHS Link Classifier

Invoke this skill explicitly as `$xhs-link-classifier`. The Python Producer has
already copied every cached XHS post body into one job JSON. Your only task is
semantic classification and factual retrieval; the outer runner validates the
result, writes `manual/<end-time>.txt`, and clears the captured cache entries.

## Inputs and boundaries

1. Read the exact job JSON path named by the prompt.
2. Process every object in `items`, in order. Each object has `item_id`,
   `cache_path`, the Producer-extracted original XHS `title`, `detected_urls`,
   and the full XHS `text`.
3. You may use web search or read-only HTTP requests to resolve a paper title to
   its real arXiv record. Prefer `arxiv.org`/`export.arxiv.org` and verify that
   the returned title actually matches the post. Never invent an ID.
4. Do not modify or delete `cache/xhs`, do not write `manual/`, do not archive
   Blog pages, and do not touch queue state. Those are outer-runner duties.
5. Write exactly one JSON file at the result path named by the prompt. Do not
   create maintenance documents, logs, scripts, or any other files.

## Classification rules

- `blog`: the post points readers to a specific non-arXiv web article/page.
  Return the full article URL, not merely the site origin and never the XHS URL.
- Treat GitHub repository/page URLs and Hugging Face repository/model/dataset
  URLs as `blog` targets. Preserve the full resource URL. The importer will add
  their origins to the Blog catalogue without downloading them.
- Treat `*.github.io` and other personal/static article or project pages as
  normal `blog` targets; the importer may archive these pages as self-contained
  HTML.
- `arxiv`: the post discusses or links a research paper that has an arXiv
  record. Return only the canonical arXiv identifier, for example
  `2608.01234v2` or `hep-th/9901001`, not an arXiv URL.
- A post may contain multiple real targets. Preserve their order of appearance.
- Strip tracking-only URL parameters when the article still resolves without
  them, but retain parameters that identify the actual page.
- When a detected URL redirects, use the final canonical article URL.
- If title/author evidence is insufficient, search is inconclusive, or the
  target is neither a Blog page nor an arXiv paper, mark that item `failed`.
  Uncertainty is a failure, not permission to guess.

## Required result schema

```json
{
  "version": 1,
  "items": [
    {
      "item_id": "xhs-00001",
      "status": "success",
      "targets": [
        {"type": "blog", "value": "https://example.org/post"},
        {"type": "arxiv", "value": "2608.01234v1"}
      ],
      "reason": "short evidence summary"
    },
    {
      "item_id": "xhs-00002",
      "status": "failed",
      "targets": [],
      "reason": "specific reason the target could not be verified"
    }
  ]
}
```

Every input `item_id` must appear exactly once. `status` is exactly `success`
or `failed`. A success has at least one target; a failure has an empty target
list and a concrete reason. The outer runner will reject malformed or unsafe
targets but will retain other valid items.

## Completion check

- Every job item has one result.
- Every arXiv identifier was verified against a real matching record.
- Every Blog value is a full `http(s)` article URL and not XHS/arXiv.
- The result is valid UTF-8 JSON at the requested path.
- No cache, manual list, Article queue, or unrelated file was changed.
