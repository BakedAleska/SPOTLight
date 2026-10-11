-- target: main
-- Search functions (contract §3). The MCP server calls these; signatures are part of the contract.
-- Placeholders substituted by db/setup.py:
--   {{VEC_TYPE}}  'vector' or 'halfvec' (same as the documents.embedding column)
--   {{HNSW_SET}}  'SET hnsw.iterative_scan = strict_order' when pgvector >= 0.8, else empty. Scoped to the
--                 function call. It only matters if the optional HNSW index exists (filters still return k rows);
--                 with the default exact search it is a no-op.
-- similarity = 1 - cosine distance. NULL filters mean "no filter". Results are ordered best first.

CREATE OR REPLACE FUNCTION match_documents(
  query_embedding vector,
  kinds           text[] DEFAULT NULL,
  filter_asset    text   DEFAULT NULL,
  filter_location text   DEFAULT NULL,
  filter_mission  text   DEFAULT NULL,
  match_count     int    DEFAULT 5,
  min_similarity  real   DEFAULT 0.0)
RETURNS TABLE (doc_id text, kind text, title text, content text, metadata jsonb, asset_id text,
               location_id text, mission_id text, action_id text, ts timestamp, similarity real)
LANGUAGE sql STABLE
{{HNSW_SET}}
AS $fn$
  SELECT c.doc_id, c.kind, c.title, c.content, c.metadata, c.asset_id, c.location_id, c.mission_id,
         c.action_id, c.ts, (1 - c.dist)::real
  FROM (
    SELECT d.doc_id, d.kind, d.title, d.content, d.metadata, d.asset_id, d.location_id, d.mission_id,
           d.action_id, d.ts, d.embedding <=> query_embedding::{{VEC_TYPE}} AS dist
    FROM documents d
    WHERE d.embedding IS NOT NULL
      AND (kinds IS NULL OR d.kind = ANY (kinds))
      AND (filter_asset IS NULL OR d.asset_id = filter_asset)
      AND (filter_location IS NULL OR d.location_id = filter_location)
      AND (filter_mission IS NULL OR d.mission_id = filter_mission)
    ORDER BY d.embedding <=> query_embedding::{{VEC_TYPE}}
    LIMIT greatest(match_count, 0)
  ) c
  WHERE (1 - c.dist) >= coalesce(min_similarity, 0)
  ORDER BY c.dist, c.doc_id;
$fn$;

-- Hybrid: vector + Postgres full-text, merged by reciprocal rank fusion: score = sum 1/(rrf_k + rank).
-- The lexical side ORs the query's terms (plainto_tsquery's ANDs become ORs), so a natural-language question
-- still matches; docs containing EVERY term rank first, then by ts_rank_cd. An exact id such as 'HX-204' is
-- indexed as the tokens 'hx' + '-204', so only HX-204 docs contain both and they rank on top.
-- Exact ids inside a question (vector-store audit VS-07): id-like tokens in query_text (HX-204, PANEL-211, A0061,
-- AN014, WP05, M014) are ANDed into tsq_ids. Rows containing every id rank first on the lexical side and get an
-- extra 1/rrf_k in the fused score, so "What anomalies were found on PANEL-211 ...?" puts PANEL-211 docs above
-- semantic-only neighbours such as PANEL-212.
-- Each side contributes its top 4*match_count.
-- Trailing columns (contract §3, requested by the MCP agent):
--   similarity  1 - cosine distance for the row (same value match_documents returns; NULL without an embedding)
--   text_match  true if the row contains EVERY query term (plainto_tsquery), or every id-like token of the query,
--               i.e. an exact-ID / exact-phrase-terms hit such as 'HX-204'. Rows that only share some terms are false.
-- The return type changed, so the old function is dropped first (CREATE OR REPLACE can't change OUT columns).
DROP FUNCTION IF EXISTS match_documents_hybrid(vector, text, text[], text, text, text, int, int);
CREATE FUNCTION match_documents_hybrid(
  query_embedding vector,
  query_text      text,
  kinds           text[] DEFAULT NULL,
  filter_asset    text   DEFAULT NULL,
  filter_location text   DEFAULT NULL,
  filter_mission  text   DEFAULT NULL,
  match_count     int    DEFAULT 5,
  rrf_k           int    DEFAULT 60)
RETURNS TABLE (doc_id text, kind text, title text, content text, metadata jsonb, asset_id text,
               location_id text, mission_id text, action_id text, ts timestamp, score real,
               similarity real, text_match boolean)
LANGUAGE sql STABLE
{{HNSW_SET}}
AS $fn$
  WITH q AS (
    SELECT CASE WHEN query_text IS NULL OR btrim(query_text) = '' THEN NULL
                ELSE replace(plainto_tsquery('english', query_text)::text, ' & ', ' | ')::tsquery
           END AS tsq,                                            -- any term
           plainto_tsquery('english', coalesce(query_text, '')) AS tsq_all,  -- every term
           (SELECT string_agg('(' || plainto_tsquery('english', m[1])::text || ')', ' & ')
                     FILTER (WHERE plainto_tsquery('english', m[1])::text <> '')::tsquery
              FROM regexp_matches(coalesce(query_text, ''),
                   '(?:^|[^[:alnum:]-])([A-Za-z]{1,6}-[0-9]{2,4}|A[0-9]{4}|AN[0-9]{3}|WP[0-9]{2}|M[0-9]{3})(?![[:alnum:]-])',
                   'g') AS m) AS tsq_ids                          -- every id-like token (NULL if none)
  ),
  semantic AS (
    SELECT d.doc_id, row_number() OVER (ORDER BY d.embedding <=> query_embedding::{{VEC_TYPE}}, d.doc_id) AS rnk
    FROM documents d
    WHERE query_embedding IS NOT NULL AND d.embedding IS NOT NULL
      AND (kinds IS NULL OR d.kind = ANY (kinds))
      AND (filter_asset IS NULL OR d.asset_id = filter_asset)
      AND (filter_location IS NULL OR d.location_id = filter_location)
      AND (filter_mission IS NULL OR d.mission_id = filter_mission)
    ORDER BY d.embedding <=> query_embedding::{{VEC_TYPE}}, d.doc_id
    LIMIT greatest(match_count, 1) * 4
  ),
  lexical AS (
    SELECT d.doc_id, coalesce(d.content_tsv @@ q.tsq_ids, false) AS id_hit,
           row_number() OVER (ORDER BY coalesce(d.content_tsv @@ q.tsq_ids, false) DESC,
                                       (d.content_tsv @@ q.tsq_all) DESC,
                                       ts_rank_cd(d.content_tsv, q.tsq) DESC, d.doc_id) AS rnk
    FROM documents d, q
    WHERE q.tsq IS NOT NULL AND d.content_tsv @@ q.tsq
      AND (kinds IS NULL OR d.kind = ANY (kinds))
      AND (filter_asset IS NULL OR d.asset_id = filter_asset)
      AND (filter_location IS NULL OR d.location_id = filter_location)
      AND (filter_mission IS NULL OR d.mission_id = filter_mission)
    ORDER BY coalesce(d.content_tsv @@ q.tsq_ids, false) DESC, (d.content_tsv @@ q.tsq_all) DESC,
             ts_rank_cd(d.content_tsv, q.tsq) DESC, d.doc_id
    LIMIT greatest(match_count, 1) * 4
  ),
  fused AS (
    SELECT coalesce(s.doc_id, l.doc_id) AS doc_id,
           (coalesce(1.0 / (rrf_k + s.rnk), 0) + coalesce(1.0 / (rrf_k + l.rnk), 0)
            + CASE WHEN l.id_hit THEN 1.0 / greatest(rrf_k, 1) ELSE 0 END)::real AS rrf
    FROM semantic s FULL OUTER JOIN lexical l ON s.doc_id = l.doc_id
  )
  SELECT d.doc_id, d.kind, d.title, d.content, d.metadata, d.asset_id, d.location_id, d.mission_id,
         d.action_id, d.ts, f.rrf,
         (1 - (d.embedding <=> query_embedding::{{VEC_TYPE}}))::real,
         coalesce(d.content_tsv @@ q.tsq_all, false) OR coalesce(d.content_tsv @@ q.tsq_ids, false)
  FROM fused f JOIN documents d ON d.doc_id = f.doc_id CROSS JOIN q
  ORDER BY f.rrf DESC, d.doc_id
  LIMIT greatest(match_count, 0);
$fn$;
