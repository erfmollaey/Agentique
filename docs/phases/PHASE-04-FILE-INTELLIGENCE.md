# Phase 4 — File Intelligence

**Status:** Not Started
**Blocked by:** Phase 3 — Research Engine
**Evidence base:** [`../ARCHITECTURE.md`](../ARCHITECTURE.md)

> **Nothing in this phase exists today.** The repository contains no upload handling, no download logic, no document parsing, and no file storage of any kind.

---

## 1. Objective

Allow users to submit documents and ask questions about them, with answers grounded in the document's actual content and traceable to a location within it.

This phase reuses the extraction, chunking, and citation machinery built in Phase 3 and applies it to user-supplied files rather than fetched web pages.

---

## 2. Current State

### Verified absences

| Capability | Status | Evidence |
|---|---|---|
| Document upload | Not Implemented | No handler, no Telegram document message handling |
| File download | Not Implemented | No download client, no file storage |
| Text extraction | Not Implemented | No parsing dependency declared or imported |
| Document chunking | Not Implemented | No chunking code |
| Embeddings / retrieval | Not Implemented | No embedding client, no vector store |
| Document Q&A | Not Implemented | No capability |
| Document summarization | Not Implemented | No capability |
| Information extraction | Not Implemented | No capability |
| Document comparison | Not Implemented | No capability |
| Page / location citations | Not Implemented | No capability |

### The catch-all handler problem

`app/bot/handlers.py:15` registers `@dp.message()` with no content filter. A user uploading a document today receives "⏳ analyzing your question…" and enqueues a research task with `query=None`. The audit records this as H-7. **Fixing H-7 is Phase 1 work; it does not deliver document handling.** After Phase 1, a document upload would be rejected cleanly. That is the correct Phase 1 outcome.

### What Phase 3 provides

This phase assumes Phase 3 delivered: content extraction, chunking with position metadata, citation generation with source-to-location linkage, and a restricted fetching client. Those components are reused rather than rewritten.

---

## 3. Scope

- Document ingestion for PDF, DOCX, TXT, and Markdown.
- URL-sourced documents where appropriate, routed through the Phase 3 restricted client.
- Secure file download and storage.
- Format-specific text extraction.
- Document chunking with position metadata.
- Document question answering.
- Document summarization.
- Structured information extraction.
- Document comparison across multiple uploads.
- Citations referencing a page, section, or offset within the source document.
- Limits on file size, type, count, and processing time.

---

## 4. Out of Scope

- **Web search and the research pipeline itself.** That is Phase 3. This phase consumes its components.
- **Research projects, workspaces, saved research, tags, bookmarks.** That is Phase 5.
- **Multi-agent orchestration.** That is Phase 6.
- **Deep Research mode, report export to PDF or Markdown, citation styles, charts, multilingual research, voice.** That is Phase 7.
- **Web dashboard, file library UI, API surface, SaaS, billing.** Phases 8 and 9.
- **Committing to an embedding provider or vector database.** See § 6 and AD-007. Retrieval strategy is evaluated, not assumed.
- **Native document rendering or conversion.** Conversion to images or HTML for layout preservation is not required; text extraction with position metadata is.
- **Fixing Phase 1, 2, or 3 defects.** Report them; do not absorb them.

---

## 5. Functional Requirements

### Ingestion
- FR-1 A user can send a supported document to the bot and have it stored and indexed.
- FR-2 Supported formats: PDF, DOCX, TXT, Markdown. URL-sourced documents are supported through the Phase 3 restricted client.
- FR-3 Unsupported formats are rejected with a clear message naming the supported types.
- FR-4 File size is bounded, and oversized files are rejected before download completes where possible.
- FR-5 File type is validated by content inspection, not by filename or declared MIME type alone.
- FR-6 A user may upload a bounded number of documents; the limit is configurable.
- FR-7 A document is associated with exactly one user and is never readable by another.
- FR-8 The original file is retained so extraction can be re-run when the parser improves.
- FR-9 A content hash is stored to detect re-uploads and duplicates.

### Extraction and chunking
- FR-10 Text is extracted per format, preserving reading order.
- FR-11 Page numbers are captured for PDF where available.
- FR-12 Section or heading boundaries are captured for DOCX and Markdown where available.
- FR-13 Content is chunked into retrieval-sized units with position metadata retained on every chunk.
- FR-14 Tables and structured content are either extracted meaningfully or explicitly marked as not supported, never silently dropped.
- FR-15 A document yielding no extractable text is reported as unprocessable rather than stored as empty.
- FR-16 Extraction failures are recorded per document with a reason.

### Document Q&A
- FR-17 A user can ask a question about an uploaded document.
- FR-18 Retrieval selects the chunks relevant to the question.
- FR-19 Answers are grounded in retrieved chunks, not generated from general knowledge.
- FR-20 Answers cite the document and, where available, the page or section.
- FR-21 A question not answerable from the document is reported as such rather than answered speculatively.
- FR-22 Answers respect Telegram message length limits.

### Summarization and extraction
- FR-23 A user can request a summary of a document, at a requested length.
- FR-24 A user can request structured extraction of specified fields.
- FR-25 Extraction failures on individual fields do not fail the whole request; partial output is returned and labeled as partial.

### Comparison
- FR-26 A user can compare two or more uploaded documents.
- FR-27 Comparison identifies agreement, difference, and contradiction.
- FR-28 Each comparative statement cites the documents supporting it.

### Lifecycle
- FR-29 A user can list their documents and delete them.
- FR-30 Deleting a document removes the file, its extracted text, and its chunks.

---

## 6. Technical Requirements

### Format handling

Each format needs a dedicated extractor behind a common interface:

```text
app/domain/documents/       Document, Chunk, Extractor protocol, domain types
app/infrastructure/docs/    One extractor per format
app/services/documents/    Ingestion, retrieval, Q&A, summarization, comparison
```

Extractors must not leak parser-library specifics into the service layer.

### Storage

- **Storage technology is undecided.** Local filesystem, object storage, or a managed service should be selected during implementation based on deployment topology, expected volume, retention requirements, and cost. The repository specifies nothing, so nothing is assumed here.
- Whatever is chosen, the application layer depends on an abstraction, not on a concrete store.
- Documents are associated with a user. Every read is ownership-checked.
- Retention and deletion must be real: deleting a document removes the bytes, not just the database row.

### Position metadata

Chunks must carry enough position information to produce a useful citation: page number for PDF, section or heading path for DOCX and Markdown, character offsets as a fallback. The Phase 3 citation model should be extended rather than replaced, so web and document citations share a representation.

### Retrieval strategy — explicitly undecided

**This document does not commit to embeddings or a vector store.**

Phase 3's pipeline is keyword-oriented. Whether document Q&A needs semantic retrieval is an empirical question that should be answered by measurement, not assumption. The evaluation should compare at minimum:

- Keyword or lexical retrieval over chunks.
- Semantic retrieval using embeddings.

against a small, representative set of real questions, measuring answer groundedness and citation correctness — not just recall.

The embedding provider and any vector store are selected only if measurement justifies them, and the decision is recorded in the Architecture Decision Log. See AD-007.

Whatever is chosen, retrieval sits behind an interface so the strategy is replaceable.

### Processing model

- Extraction is CPU- and memory-intensive and may be slow. It runs in the Celery worker, never in the message handler.
- The Phase 1 Celery time limits apply. A pathological document must not occupy a worker indefinitely.
- Extraction is asynchronous relative to upload: the user is told the document is being processed, and receives a terminal notification when it is ready or has failed. This follows the Phase 1 requirement that every message receives exactly one terminal response.

### Resource controls

- Maximum file size.
- Maximum page or character count.
- Maximum chunk count per document.
- Maximum documents per user.
- Explicit timeouts on every processing stage.

---

## 7. Security Requirements

Uploading files introduces a second untrusted-input surface alongside the Phase 3 web-fetch surface.

- **SR-1 File content is untrusted.** A document is data. Its text never becomes an instruction, and text inside an uploaded file is treated exactly like text inside a fetched web page — including prompt-injection content.
- **SR-2 Type validation by content, not by claim.** Filename extension and declared MIME type are untrusted and insufficient. The actual content must be inspected.
- **SR-3 Bounded resources.** Size, page, character, and chunk limits are enforced during ingestion, not after. A decompression bomb or a pathological file must not exhaust memory or disk.
- **SR-4 Isolated parsing.** Parsers must run with bounded memory and time. A parser vulnerability is a real risk with untrusted files; the mitigation is bounding and, where the deployment allows, process isolation.
- **SR-5 No path traversal.** A filename from a user must never influence where a file is written. Storage paths are generated by the application, never derived from user input.
- **SR-6 No arbitrary file read.** The application reads only files it has stored, and only for a user it has ownership-checked.
- **SR-7 User isolation.** Every document, chunk, and derived artifact is owned by exactly one user. Cross-user access is a test, not an assumption.
- **SR-8 Private storage.** Documents are user data. Access controls apply at the storage layer, not only in application code. A presigned-URL or equivalent mechanism, if used, must be short-lived and scoped to one object.
- **SR-9 Deletion is real.** Removing a document removes the stored bytes, not only metadata.
- **SR-10 No execution of document content.** Documents are parsed as data. Nothing in a document is executed, and no document-derived value is used to construct a path, command, or query.
- **SR-11 No secrets in derived content.** Extracted text is never logged in full, and never placed in a location where it could be exposed to another user.
- **SR-12 Malware scanning.** Whether to scan uploads, and with what, is a deployment decision to be made during implementation. It must be recorded as a decision rather than assumed either way.

---

## 8. Testing Requirements

| ID | Test | Verifies |
|---|---|---|
| T-1 | Each supported format uploads, extracts, and chunks correctly | FR-1, FR-10 |
| T-2 | An unsupported format is rejected with a clear message | FR-3 |
| T-3 | An oversized file is rejected | FR-4 |
| T-4 | **A file with a misleading extension and wrong content is rejected** | SR-2 |
| T-5 | A document yielding no text is reported unprocessable, not stored as empty | FR-15 |
| T-6 | Page numbers are captured for PDF and appear in citations | FR-11, FR-20 |
| T-7 | Sections or headings are captured for DOCX and Markdown | FR-12 |
| T-8 | **A filename containing traversal sequences cannot write outside the storage root** | SR-5 |
| T-9 | **A decompression bomb or pathological file is bounded in memory and time** | SR-3, SR-4 |
| T-10 | Answers are grounded in retrieved chunks and cite the document | FR-19, FR-20 |
| T-11 | An unanswerable question is reported as unanswerable, not answered speculatively | FR-21 |
| T-12 | **A document containing prompt-injection text does not alter system behavior** | SR-1 |
| T-13 | **One user's document is never retrievable by another user** | SR-7 |
| T-14 | Deleting a document removes the file, its text, and its chunks | FR-30, SR-9 |
| T-15 | Re-uploading identical content is detected by hash | FR-9 |
| T-16 | Summarization honors the requested length | FR-23 |
| T-17 | Partial extraction failure returns partial output labeled as partial | FR-25 |
| T-18 | Comparison identifies agreement, difference, and contradiction with citations | FR-27, FR-28 |
| T-19 | The upload path never executes document content | SR-10 |
| T-20 | The document count limit is enforced | FR-6 |
| T-21 | Retrieval strategy is replaceable behind its interface | Provider abstraction |

**Mandatory tests:** T-4, T-8, T-9, T-12, T-13. These cover content-type spoofing, path traversal, resource exhaustion, prompt injection, and cross-user isolation — the defining risks of this phase.

---

## 9. Documentation Requirements

- [ ] Supported formats and their known limitations documented.
- [ ] Extraction behavior per format documented, including what is not extracted.
- [ ] Storage technology and its selection rationale documented.
- [ ] Resource limits and how an operator changes them documented.
- [ ] The retrieval evaluation documented, including the measured comparison and the resulting decision.
- [ ] Any embedding or vector-store decision recorded in the Architecture Decision Log with its justification.
- [ ] The prompt-safety treatment of document content documented.
- [ ] Deletion semantics documented.
- [ ] `docs/PROJECT_PLAN.md` maturity table, phase status, and decision log updated.
- [ ] `docs/ARCHITECTURE.md` updated.

---

## 10. Acceptance Criteria

### Ingestion
- [ ] PDF, DOCX, TXT, and Markdown upload and process correctly.
- [ ] Unsupported formats are rejected with a clear message.
- [ ] File size, type, and count limits are enforced and tested.
- [ ] Type validation is based on content, not on filename or declared MIME type.
- [ ] A content hash detects re-uploads.

### Extraction
- [ ] Text is extracted with reading order preserved.
- [ ] Page numbers are captured for PDF and appear in citations.
- [ ] Sections or headings are captured for DOCX and Markdown.
- [ ] A document yielding no text is reported unprocessable.

### Capabilities
- [ ] A user can ask a question and receive a grounded, cited answer.
- [ ] An unanswerable question is reported as such.
- [ ] Summarization honors a requested length.
- [ ] Structured extraction returns partial output labeled partial when a field fails.
- [ ] Document comparison identifies agreement, difference, and contradiction with citations.

### Security
- [ ] A file with a misleading extension is rejected.
- [ ] A traversal filename cannot write outside the storage root.
- [ ] A pathological file is bounded in memory and time.
- [ ] A prompt-injection document cannot alter system behavior.
- [ ] One user's document is never retrievable by another user.
- [ ] Deleting a document removes the stored bytes.

### Quality
- [ ] Extraction is asynchronous, and the user always receives a terminal notification.
- [ ] Retrieval strategy is replaceable behind an interface.
- [ ] The retrieval evaluation is documented with its conclusion.
- [ ] Tests run without external services.
- [ ] Documentation is updated.
- [ ] No Phase 5–9 functionality was implemented.

---

## 11. Dependencies

### Prerequisites

**Phase 3 must be complete.** Specifically required:

| Phase 3 item | Why Phase 4 needs it |
|---|---|
| Content extraction abstraction | Reused for format-specific extractors |
| Chunking with position metadata | Documents are chunked the same way pages are |
| Citation model | Extended to carry page or section references |
| Restricted HTTP client | URL-sourced documents route through the same SSRF controls |
| Prompt-safety design | Document text is untrusted exactly like web text |
| Research run and source persistence | Documents attach to the same run and source model |
| Partial-result and failure-reporting patterns | Applied to extraction failures |

### Infrastructure

- **Storage for uploaded files — undecided.** Selected during implementation.
- PostgreSQL, extended with the document and chunk model.
- Redis and the Celery worker for asynchronous extraction.
- Outbound access only if URL-sourced documents are supported.

### Blocks

- **Phase 5** organizes documents alongside sources into projects and workspaces.
- **Phase 6** may add a document-specialized reader or extraction agent.
- **Phase 7** builds premium report generation over documents and sources.

---

## 12. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Prompt injection via document text | Model behavior hijacked by an attacker-supplied file | Treat document text exactly like web text; reuse Phase 3 isolation; mandatory test T-12 |
| Path traversal via filename | Arbitrary file write or read | Storage paths generated by the application, never from user input; mandatory test T-8 |
| Pathological or malicious files | Memory exhaustion, worker starvation, parser crash | Size, page, and time bounds; isolated parsing; mandatory test T-9 |
| Content-type spoofing | Malicious content processed as a supported type | Validate by content inspection; mandatory test T-4 |
| PDF and DOCX extraction quality varies widely | Inconsistent answer quality, user distrust | Document per-format limitations honestly; test against representative real files |
| Scanned PDFs with no text layer | Silent failure, empty results | Detect and report clearly rather than returning an empty answer |
| Chunking strategy poorly matched to document structure | Poor retrieval, weak citations | Measure retrieval quality; retain position metadata for citations |
| Premature commitment to embeddings and a vector store | Cost and operational complexity before justification | Evaluate lexical against semantic retrieval first; record the decision; keep retrieval behind an interface |
| Storage growth unbounded | Cost and quota exhaustion | Per-user quotas, retention policy, real deletion |
| Documents not scoped to users in the schema | Cross-user data exposure | Ownership enforced at the schema and service layers; mandatory test T-13 |
| Long extraction blocking the message handler | Handler timeout, user-visible failure | Extraction in the worker; terminal notification on completion or failure |

---

## 13. Definition of Done

Phase 4 is complete when:

1. Every acceptance criterion in § 10 is checked and evidenced.
2. A user can upload a supported document, ask questions about it, and receive grounded, cited answers.
3. The five mandatory security tests pass.
4. Extraction limitations per format are documented honestly, including formats that cannot be fully processed.
5. The retrieval evaluation is documented with a recorded decision and a replaceable strategy.
6. Deletion removes stored bytes, not only metadata.
7. Tests run without external services.
8. Documentation is updated.
9. No Phase 5–9 functionality was implemented.
10. Status is updated in this document and in `docs/PROJECT_PLAN.md`.

---

## 14. Status

**Current status: Not Started**

**Phase status in `docs/PROJECT_PLAN.md`: Not Started.**

**Blocked by:** Phase 3 — Research Engine.

No Phase 4 work has been implemented. The repository has no upload handling, no file storage, no parsing, and no document capability of any kind.

**Storage technology: Open.**
**Embedding provider and vector store: Open.** Recorded as AD-007 in the Architecture Decision Log. Neither is selected, and neither is assumed by this document.
