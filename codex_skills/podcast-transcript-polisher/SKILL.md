---
name: podcast-transcript-polisher
description: Correct a Producer-prepared AirHub Whisper transcript chunk, improve speaker-name assignment, paragraph separation, punctuation, and proper-noun accuracy, then write the required JSON result. Use only for local podcast polish job JSON files after Whisper transcription; do not download audio, transcribe media, render final HTML, or edit Article state.
---

# AirHub Podcast Transcript Polisher

Invoke this skill explicitly as `$podcast-transcript-polisher`. The Python
Producer has already downloaded and transcribed the audio, assigned provisional
speakers, and split a long episode into bounded chunks. Correct the supplied
chunk without changing its meaning. The outer finalizer validates coverage and
order, joins all chunks, renders the final HTML, and updates Article state.

## Boundaries

1. Read only the exact job JSON path named by the prompt.
2. Process every object in `segments`, in order. Use `episode`,
   `participant_candidates`, `terminology_context`, and the read-only
   `context_before`/`context_after` segments as evidence.
3. Write exactly one UTF-8 JSON file at the result path named by the prompt.
4. Do not modify the source transcript, draft/final HTML, podcast job, Article
   queues, attachments, logs, or any unrelated file.
5. Do not perform audio work. Never claim to have heard audio; only use the
   Producer-provided text and metadata.

## Correction rules

- Preserve every `segment_id` exactly once and preserve input order. Do not add,
  delete, split, merge, or reorder segments.
- Correct punctuation, spacing, obvious homophone/ASR errors, sentence breaks,
  and light oral disfluencies. Preserve all substantive claims, numbers,
  qualifications, examples, and intentional repetition. Do not summarize,
  translate, fact-check opinions, or rewrite the speaker's style.
- Correct proper nouns such as people, organizations, products, models, papers,
  places, and technical terms when supported by episode title, shownotes,
  terminology context, adjacent segments, or a reliable web source. If evidence
  is insufficient, retain the source wording instead of guessing.
- Prefer an exact name from `participant_candidates` when the dialogue provides
  evidence for that identity. Keep a generic label such as `说话人 1` when the
  identity is uncertain. Never assign identity from demographic stereotypes.
- Keep one speaker per segment. When consecutive source segments represent
  different people, return different speaker labels so the deterministic HTML
  renderer creates separate paragraphs. Keep labels consistent across the
  entire chunk and with the supplied surrounding context.
- Use web search only when needed to verify a specific proper noun. Do not use
  browsing to add background material or new dialogue.

## Required result schema

```json
{
  "version": 1,
  "chunk_id": "chunk-0001",
  "segments": [
    {
      "segment_id": "seg-000001",
      "speaker": "主持人姓名",
      "text": "纠正后的完整文本。"
    },
    {
      "segment_id": "seg-000002",
      "speaker": "嘉宾姓名",
      "text": "下一位说话人的完整文本。"
    }
  ],
  "notes": [
    "仅记录重要且有证据的专有名词或说话人修正；没有则为空数组"
  ]
}
```

`version` must be integer `1`; `chunk_id` must exactly match the job. Each result
segment must contain only its original `segment_id`, one non-empty plain-text
speaker label, and one non-empty plain-text corrected transcript. Do not include
HTML or Markdown in speaker/text fields. `notes` must be a JSON string array.

## Completion check

- Every input `segment_id` appears exactly once in the same order.
- Speaker and text are non-empty plain text.
- The corrected text preserves the source meaning and substantive coverage.
- Proper-noun and identity corrections have evidence; uncertainty is retained.
- Only the requested result JSON was written.
