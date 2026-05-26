# Retrieval Arbiter — Design Spec

Pre-model retrieval contract for the academic knowledge pipeline.

Stack: **Qdrant** (vectors), **Neo4j** (graph), **SQLite** (audit), **Zotero**
(canonical documents + annotations), **Obsidian** (derived notes), **NotebookLM**
(staging), **megamem + Obsidian memory** (durable-memory helpers).

> RAGFlow has been deprecated from this stack; this spec targets a standalone
> retriever service. Vendor-side context compaction (Claude / Codex) is downstream
> of this layer and out of scope — the lever is what enters the model context here.

---

## 0. Roles — single source of truth per concern

| Store | Role in arbiter | Trust tier |
|---|---|---|
| **Zotero** | Canonical documents + annotations. Provenance authority. | `canonical` |
| **Obsidian** | Derived notes (lit notes, syntheses). Raw-file path. | `derived` |
| **NotebookLM** | Staging: docs/outputs not yet promoted. | `staging` |
| **Qdrant** | Vector index over chunks of canonical + derived content. | inherits source |
| **Neo4j** | Entity / citation / backlink graph. Navigation signal. | inherits source |
| **SQLite** | Audit log of every working set (not a retrieval source). | — |
| **megamem + Obsidian memory** | Durable-memory helpers. Write-gated by trust tier. | — |

The trust tier rides on every candidate chunk and drives arbitration, citation
eligibility, and memory promotion.

---

## 1. Interface

```
retrieve(query, intent, budget) -> WorkingSet
  intent  ∈ {EDIT, GROUND, CITE, NAVIGATE}
  budget  = { k_per_source, max_retrieval_tokens, max_hops }

WorkingSet = ordered[ Chunk ]
Chunk = {
  canonical_ref,        # see §2 — collapses representations
  source,               # zotero | obsidian | notebooklm
  trust,                # canonical | derived | staging
  char_span,            # (start,end) in the source doc
  text,
  provenance,           # {path|zotero_key, source_mtime, embed_model_ver, embedded_at}
  why,                  # one line, ≤120 chars
}
```

The arbiter is the **only** component allowed to inject into the model. Raw reads,
Qdrant hits, and Neo4j neighbors are *candidates*, never direct feeds.

---

## 2. Canonical reference + dedup

A Zotero PDF, its Qdrant chunk, and the Obsidian literature note about it are
different representations of the same underlying claim. Dedup must resolve to a
shared identity.

- `canonical_ref` = `zotero_item_key` when the chunk traces to a Zotero doc; else
  `obsidian://<note_id>`; else `notebooklm://<staging_id>`.
- Dedup key = `(canonical_ref, char_span)`. On overlap, **keep highest trust, then
  longest span**; drop the rest. A Zotero-canonical span beats the Obsidian
  paraphrase of the same passage.
- Cross-representation collapse: if an Obsidian note's frontmatter links a
  `zotero_key`, treat its quoted spans as pointing at the canonical doc so they
  dedup against Qdrant chunks of that doc.

---

## 3. Path arbitration by intent

| Intent | Primary path | Suppressed |
|---|---|---|
| **EDIT** (writing/changing a note) | Obsidian raw read of that note | retriever for that note |
| **GROUND** (synthesis, answering) | Qdrant summary + provenance | raw reads |
| **CITE** (anything carrying a reference) | **Zotero canonical only**; Qdrant locates, Zotero span is quoted | derived/staging as citation source |
| **NAVIGATE** (what connects to what) | Neo4j (see §4) | bulk text |

Rule: **staging (NotebookLM) content is never a CITE source** and is always labeled
`[staging]` in-prompt so pre-canonical material is not silently treated as
established.

---

## 4. Degree-aware graph expansion (Neo4j)

Academic graphs are hub-heavy (author nodes, topic MOCs, citation clusters). Naive
expansion is the token bomb.

- `max_hops` default 1; 2 only on explicit NAVIGATE with budget headroom.
- Per-node neighbor cap (e.g. ≤5), ranked by edge weight / recency.
- **Hub rule:** if `degree(node) > τ` (tune τ to the graph), return a one-line
  **neighbor list** (titles + refs), *not* neighbor text. The model asks for
  specifics if needed.
- Graph returns *pointers* (canonical_refs + why-edge) which then pass through §2
  dedup and §5 freshness like any other candidate — graph never injects text
  directly.

---

## 5. Freshness gate

- Reject any candidate where `provenance.source_mtime > embedded_at` (stale
  embedding) → serve nothing, enqueue re-embed.
- Zotero is the freshness authority for `canonical`; Obsidian file mtime for
  `derived`.
- Index **incrementally on write** (watch Zotero storage + vault) so rejections are
  rare.
- `staging` chunks always carry a freshness warning regardless of mtime, since
  NotebookLM content is by definition unverified.

---

## 6. Fan-out budget

- Small `k_per_source` (start 5–8 vector, ≤5 graph neighbors).
- Global `max_retrieval_tokens` ceiling the arbiter **cannot exceed** — when full,
  drop lowest trust × lowest score first.
- One-line `why` per chunk. Never multi-sentence preambles (the "Lost in the Middle"
  recall tax is real).

---

## 7. Audit log (SQLite)

One row per `retrieve()` call — the context-rot detector.

```
turn_id, ts, intent, query_hash,
candidates_in {zotero, obsidian, notebooklm, qdrant, neo4j},
dedup_collapses, freshness_rejects,
chunks_out, retrieval_tokens, k_requested, k_served,
trust_mix {canonical, derived, staging}
```

Alert when:
- `trust_mix.staging > 0` on a CITE turn,
- `dedup_collapses` spikes (duplication leak),
- `retrieval_tokens` trends up across turns (fan-out creep).

---

## 8. Memory promotion gate (megamem / Obsidian memory)

Don't let retrieval-heavy, low-trust context become durable memory.

- Only `canonical` and `derived` content is eligible for promotion to durable
  memory.
- **`staging` (NotebookLM) is hard-excluded** until promoted to canonical in Zotero.
- Every promoted memory item retains its `canonical_ref` + `embed_model_ver` so a
  later vault/Zotero change can invalidate it (closes the stale-memory loop).

---

## Flow summary

Candidates in from Qdrant / Neo4j / raw reads → one arbiter applies
**trust → dedup → intent-routing → graph caps → freshness → budget** → audited to
SQLite → guarded promotion path to durable memory.
