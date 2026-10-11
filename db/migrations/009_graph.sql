-- target: main
-- Graph RAG (contract §7): a knowledge graph in plain Postgres. Neon has no Apache AGE (no Cypher), so this is
-- node/edge tables + pgvector on nodes + multi-hop traversal with WITH RECURSIVE. Idempotent: safe to re-run.
-- Filled by db/graph_build.py (`db/job.sh graph`, run by db-init after ingest). Placeholders substituted by
-- db/setup.py, as for the documents table:
--   {{EMBED_DIM}}  vector width (EMBED_DIM, default 1024)
--   {{VEC_TYPE}}   'vector', or 'halfvec' when EMBED_DIM > 2000
-- pg_trgm is on Neon's extension list (https://neon.com/docs/extensions/pg-extensions) and in the pgvector image.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS kg_nodes (
  node_id     text PRIMARY KEY,          -- '<kind>:<key>', e.g. 'asset:HX-204', 'location:WP09', 'failure_type:LOCALIZATION_FAILURE'
  kind        text NOT NULL CHECK (kind IN ('mission','action','asset','location','environment','action_type',
                'failure_type','anomaly_type','anomaly','flag','policy_rule','topic','manual','operator_note','explanation')),
  key         text NOT NULL,             -- the natural id (asset_id, WP id, enum value, doc id...)
  label       text NOT NULL,             -- human-readable name ("Heat Exchanger 04", "Turbine Hall South")
  summary     text NOT NULL,             -- one or two sentences of facts about the node; this is what gets embedded
  props       jsonb NOT NULL DEFAULT '{}',
  doc_id      text REFERENCES documents(doc_id) ON DELETE SET NULL,   -- document-backed nodes (see contract §7)
  embedding   {{VEC_TYPE}}({{EMBED_DIM}}),
  embed_model text,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kg_edges (
  src      text NOT NULL REFERENCES kg_nodes ON DELETE CASCADE,
  rel      text NOT NULL,                -- contract §7 relation list
  dst      text NOT NULL REFERENCES kg_nodes ON DELETE CASCADE,
  weight   real NOT NULL DEFAULT 1.0,    -- strength (counts/rates for statistical edges)
  props    jsonb NOT NULL DEFAULT '{}',  -- e.g. {"n": 6, "rate": 0.5}; 'source': 'fk' | 'stat' | 'extract' | 'curated'
  evidence text,                         -- a citable id (doc_id, anomaly_id, action_id) supporting the edge
  PRIMARY KEY (src, rel, dst)
);

-- (src, ...) is covered by the primary key. Trigram GIN indexes back match_kg_nodes_text.
CREATE INDEX IF NOT EXISTS kg_edges_dst        ON kg_edges (dst);
CREATE INDEX IF NOT EXISTS kg_edges_rel        ON kg_edges (rel);
CREATE INDEX IF NOT EXISTS kg_nodes_kind       ON kg_nodes (kind);
CREATE INDEX IF NOT EXISTS kg_nodes_doc        ON kg_nodes (doc_id);
CREATE INDEX IF NOT EXISTS kg_nodes_label_trgm ON kg_nodes USING gin (label gin_trgm_ops);
CREATE INDEX IF NOT EXISTS kg_nodes_key_trgm   ON kg_nodes USING gin (key gin_trgm_ops);

-- ── Entry points by vector similarity ─────────────────────────────────────────────────────────────
-- similarity = 1 - cosine distance (same scale as match_documents). Nodes without an embedding are skipped.
-- NULL kinds = every kind; NULL min_similarity = 0. Exact search (a few hundred nodes).
CREATE OR REPLACE FUNCTION match_kg_nodes(
  query_embedding vector,
  kinds           text[] DEFAULT NULL,
  match_count     int    DEFAULT 5,
  min_similarity  real   DEFAULT 0.0)
RETURNS TABLE (node_id text, kind text, label text, summary text, doc_id text, similarity real)
LANGUAGE sql STABLE
AS $fn$
  SELECT c.node_id, c.kind, c.label, c.summary, c.doc_id, (1 - c.dist)::real
  FROM (
    SELECT n.node_id, n.kind, n.label, n.summary, n.doc_id, n.embedding <=> query_embedding::{{VEC_TYPE}} AS dist
    FROM kg_nodes n
    WHERE n.embedding IS NOT NULL
      AND (kinds IS NULL OR n.kind = ANY (kinds))
    ORDER BY n.embedding <=> query_embedding::{{VEC_TYPE}}, n.node_id
    LIMIT greatest(coalesce(match_count, 5), 0)
  ) c
  WHERE (1 - c.dist) >= coalesce(min_similarity, 0)
  ORDER BY c.dist, c.node_id;
$fn$;

-- ── Entry points by name / id text ────────────────────────────────────────────────────────────────
-- score (0..1, best first):
--   1.0   exact match of the query on node_id, key or label, ignoring case and punctuation
--         ('hx204', 'HX-204', 'asset:HX-204', 'localization failure', 'Turbine Hall South')
--   else  the best pg_trgm score, capped at 0.99:
--           similarity / word_similarity of the query against label and key ('Turbine Hall' -> WP08, WP09), or
--           strict_word_similarity of a label (or an alphanumeric key of 3+ chars) found INSIDE the query, if
--           >= 0.75 ('why does HX-204 keep failing' -> asset:HX-204).
-- Rows scoring below 0.3 are dropped. NULL/blank query -> no rows. NULL kinds = every kind.
-- Ties: closer whole-label similarity, then shorter label, then node_id.
CREATE OR REPLACE FUNCTION match_kg_nodes_text(
  query_text  text,
  kinds       text[] DEFAULT NULL,
  match_count int    DEFAULT 5)
RETURNS TABLE (node_id text, kind text, label text, summary text, doc_id text, score real)
LANGUAGE sql STABLE
AS $fn$
  WITH q AS (
    SELECT t, lower(regexp_replace(t, '[^[:alnum:]]+', '', 'g')) AS norm
    FROM (SELECT nullif(btrim(query_text), '') AS t) x
  ),
  raw AS MATERIALIZED (                            -- every trigram score computed once per row
    SELECT n.node_id, n.kind, n.label, n.summary, n.doc_id,
           (lower(n.node_id) = lower(q.t)
            OR lower(regexp_replace(n.key, '[^[:alnum:]]+', '', 'g')) = q.norm
            OR lower(regexp_replace(n.label, '[^[:alnum:]]+', '', 'g')) = q.norm) AS exact,
           similarity(n.label, q.t) AS label_sim,
           greatest(similarity(n.key, q.t), word_similarity(q.t, n.label), word_similarity(q.t, n.key)) AS fwd,
           strict_word_similarity(n.label, q.t) AS rev_label,
           CASE WHEN length(n.key) >= 3 AND n.key ~ '[A-Za-z]' THEN strict_word_similarity(n.key, q.t) END AS rev_key
    FROM kg_nodes n, q
    WHERE q.t IS NOT NULL
      AND (kinds IS NULL OR n.kind = ANY (kinds))
  ),
  s AS (
    SELECT r.*, greatest(r.label_sim, r.fwd,
                         CASE WHEN r.rev_label >= 0.75 THEN r.rev_label ELSE 0 END,
                         CASE WHEN r.rev_key >= 0.75 THEN r.rev_key ELSE 0 END) AS trgm
    FROM raw r
  )
  SELECT s.node_id, s.kind, s.label, s.summary, s.doc_id,
         (CASE WHEN s.exact THEN 1.0 ELSE least(s.trgm, 0.99) END)::real AS score
  FROM s
  WHERE s.exact OR s.trgm >= 0.3
  ORDER BY 6 DESC, s.label_sim DESC, length(s.label), s.node_id
  LIMIT greatest(coalesce(match_count, 5), 0);
$fn$;

-- ── k-hop neighbourhood ───────────────────────────────────────────────────────────────────────────
-- Walks kg_edges in BOTH directions from every seed (a WITH RECURSIVE over simple paths: a node already on the
-- path is never revisited, so cycles and self-loops terminate). Returns one row per reached node: its best
-- (shortest, then strongest) path. Seeds are not returned as hop 0, but a seed reached from ANOTHER seed is
-- (so the edge between two seeds still shows). Unknown seed ids are ignored.
--   max_hops     NULL -> 2; clamped to 0..3 (0 -> no rows)
--   rel_filter   NULL -> all relations; else only these edges are walked
--   kind_filter  NULL -> all kinds; else only rows of these kinds are RETURNED (the walk passes through any kind)
--   max_nodes    NULL -> 40; applied after kind_filter
-- Columns: hop >= 1; via_rel / via_node = the last edge walked (via_node is the previous node on the path; the
-- stored edge may point either way); evidence = that edge's; weight = the PATH's strength, the product of the
-- edge weights along it (hop 1: the edge's own weight), so a path through a hub (1 of WP05's 43 actions, weight
-- 1/43) ranks below one through a specific fact; path = the readable chain from the seed, alternating node ids
-- and steps: {location:WP05, <-common_at-, failure_type:LOCALIZATION_FAILURE, -relates_to->, topic:localization}
-- ('-rel->' walks src->dst, '<-rel-' walks dst->src).
-- Order: hop ascending, weight descending, then semantic kinds before instance kinds, then node_id.
CREATE OR REPLACE FUNCTION graph_neighborhood(
  seed_ids    text[],
  max_hops    int    DEFAULT 2,
  rel_filter  text[] DEFAULT NULL,
  kind_filter text[] DEFAULT NULL,
  max_nodes   int    DEFAULT 40)
RETURNS TABLE (node_id text, kind text, label text, summary text, doc_id text, hop int,
               via_rel text, via_node text, path text[], weight real, evidence text)
LANGUAGE sql STABLE
AS $fn$
  WITH RECURSIVE
  lim AS (SELECT least(greatest(coalesce(max_hops, 2), 0), 3) AS hops),
  seeds AS (
    SELECT DISTINCT n.node_id FROM kg_nodes n WHERE n.node_id = ANY (seed_ids)
  ),
  walk (node_id, hop, via_rel, via_node, path, weight, evidence) AS (
    SELECT s.node_id, 0, NULL::text, NULL::text, ARRAY[s.node_id], NULL::real, NULL::text
    FROM seeds s
    UNION ALL
    SELECT nx.node_id, w.hop + 1, nx.rel, w.node_id, w.path || nx.step || nx.node_id,
           (coalesce(w.weight, 1) * nx.weight)::real, nx.evidence
    FROM walk w
    CROSS JOIN LATERAL (
      SELECT e.dst AS node_id, e.rel, e.weight, e.evidence, '-' || e.rel || '->' AS step
      FROM kg_edges e
      WHERE e.src = w.node_id AND (rel_filter IS NULL OR e.rel = ANY (rel_filter))
      UNION ALL
      SELECT e.src, e.rel, e.weight, e.evidence, '<-' || e.rel || '-'
      FROM kg_edges e
      WHERE e.dst = w.node_id AND (rel_filter IS NULL OR e.rel = ANY (rel_filter))
    ) nx
    WHERE w.hop < (SELECT l.hops FROM lim l)
      AND NOT (nx.node_id = ANY (w.path))          -- simple paths only: no cycles, no self-loops
  ),
  best AS (
    SELECT DISTINCT ON (w.node_id) w.node_id, w.hop, w.via_rel, w.via_node, w.path, w.weight, w.evidence
    FROM walk w
    WHERE w.hop > 0
    ORDER BY w.node_id, w.hop, w.weight DESC, w.path
  )
  SELECT b.node_id, n.kind, n.label, n.summary, n.doc_id, b.hop, b.via_rel, b.via_node, b.path, b.weight, b.evidence
  FROM best b
  JOIN kg_nodes n ON n.node_id = b.node_id
  WHERE kind_filter IS NULL OR n.kind = ANY (kind_filter)
  ORDER BY b.hop, b.weight DESC,
           array_position(ARRAY['topic','manual','failure_type','anomaly_type','environment','action_type',
                                'policy_rule','location','asset','operator_note','explanation','anomaly','flag',
                                'mission','action'], n.kind),
           b.node_id
  LIMIT greatest(coalesce(max_nodes, 40), 0);
$fn$;

-- ── Grants (guarded like 007_roles.sql: warn instead of failing) ──────────────────────────────────
-- spot_mcp_reader: SELECT on both tables + EXECUTE on the three functions. spot_app: read/write (live runs write kg_*;
-- the INSERT/UPDATE grants are in 013_live.sql), plus the same EXECUTE.
-- (007's default privileges already give both roles their usual rights on new public tables; this makes the
-- graph grants explicit and independent of which role created the tables.)
DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'spot_mcp_reader') THEN
    GRANT USAGE ON SCHEMA public TO spot_mcp_reader;
    GRANT SELECT ON kg_nodes, kg_edges TO spot_mcp_reader;
    GRANT EXECUTE ON FUNCTION match_kg_nodes(vector, text[], int, real), match_kg_nodes_text(text, text[], int),
      graph_neighborhood(text[], int, text[], text[], int) TO spot_mcp_reader;
  ELSE
    RAISE WARNING 'role spot_mcp_reader missing; run migrate again after creating it (007_roles.sql)';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'spot_app') THEN
    GRANT USAGE ON SCHEMA public TO spot_app;
    GRANT SELECT ON kg_nodes, kg_edges TO spot_app;
    GRANT EXECUTE ON FUNCTION match_kg_nodes(vector, text[], int, real), match_kg_nodes_text(text, text[], int),
      graph_neighborhood(text[], int, text[], text[], int) TO spot_app;
  END IF;
EXCEPTION WHEN insufficient_privilege THEN
  RAISE WARNING 'could not apply graph grants (%); apply them as the database owner', SQLERRM;
END
$grants$;
