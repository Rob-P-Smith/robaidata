# Data Storage Schema Documentation

This directory contains all persistent data for the RobAI RAG and Knowledge Graph system.

## Overview

The system uses a **hybrid storage architecture**:
- **SQLite** (`crawl4ai_rag.db`) - Vector embeddings, content storage, and chunk metadata
- **Neo4j** (remote graph database) - Knowledge graph with entities, relationships, and document structure

Both databases are synchronized via `content_id` (SQLite) ↔ `content_id` (Neo4j Document nodes).

## SQLite Database: `crawl4ai_rag.db`

**Location**: `/app/data/crawl4ai_rag.db` (in container) → `./robaidata/crawl4ai_rag.db` (on host)

**Mode**: RAM-first with background disk sync when `USE_MEMORY_DB=true`

**Model**: `all-MiniLM-L6-v2` (384-dimension embeddings)

### Core Tables

#### 1. `crawled_content` - Document Storage

Primary table for storing crawled web pages.

```sql
CREATE TABLE crawled_content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- Unique content_id
    url TEXT UNIQUE NOT NULL,              -- Source URL
    title TEXT,                            -- Page title
    content TEXT,                          -- Cleaned plaintext
    markdown TEXT,                         -- Full markdown (used for KG extraction)
    content_hash TEXT,                     -- SHA256 hash for deduplication
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    added_by_session TEXT,                 -- Session UUID that added this
    retention_policy TEXT DEFAULT 'permanent',  -- 'permanent' or 'session_only'
    tags TEXT,                             -- Comma-separated tags
    metadata TEXT,                         -- JSON metadata

    -- Knowledge Graph tracking
    kg_processed BOOLEAN DEFAULT 0,        -- Has KG extraction completed?
    kg_entity_count INTEGER DEFAULT 0,     -- Number of entities extracted
    kg_relationship_count INTEGER DEFAULT 0,
    kg_document_id TEXT,                   -- Neo4j Document node elementId
    kg_processed_at DATETIME               -- When KG processing completed
);

CREATE INDEX idx_crawled_content_kg
ON crawled_content(kg_processed, kg_processed_at);
```

**Key Points**:
- `markdown` column contains the **full document** used for retrieval
- Content is cleaned before storage (navigation removed, language validated)
- Documents are deduplicated by URL and content hash

#### 2. `content_vectors` - Vector Embeddings (Virtual Table)

Uses `sqlite-vec` extension for vector similarity search.

```sql
CREATE VIRTUAL TABLE content_vectors USING vec0(
    embedding FLOAT[384],  -- 384-dim vector from all-MiniLM-L6-v2
    content_id INTEGER     -- FK to crawled_content.id
);
```

**Implementation Details**:
- Each document is chunked (1000 chars, 0 overlap by default)
- Navigation chunks filtered before embedding
- Multiple embeddings per document (1 per chunk)
- `rowid` used to link to `content_chunks` table

**Related Internal Tables** (created by vec0):
- `content_vectors_info` - Extension metadata
- `content_vectors_chunks` - Chunk storage
- `content_vectors_rowids` - Rowid mapping
- `content_vectors_vector_chunks00` - Actual vector data
- `content_vectors_metadatachunks00` - Metadata

#### 3. `content_chunks` - Chunk Metadata

Links vector embeddings to document positions and KG entities.

```sql
CREATE TABLE content_chunks (
    rowid INTEGER PRIMARY KEY,              -- Same as content_vectors rowid
    content_id INTEGER NOT NULL,            -- FK to crawled_content
    chunk_index INTEGER NOT NULL,           -- Sequential: 0, 1, 2, ...
    chunk_text TEXT NOT NULL,               -- Full chunk text
    char_start INTEGER NOT NULL,            -- Position in original markdown
    char_end INTEGER NOT NULL,              -- End position in markdown
    word_count INTEGER,                     -- Words in chunk
    kg_processed BOOLEAN DEFAULT 0,         -- KG extraction done for this chunk
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (content_id) REFERENCES crawled_content(id) ON DELETE CASCADE
);

CREATE INDEX idx_content_chunks_content_id ON content_chunks(content_id);
CREATE INDEX idx_content_chunks_position ON content_chunks(char_start, char_end);
CREATE INDEX idx_content_chunks_kg_processed ON content_chunks(kg_processed);
```

**Critical Relationship**:
```
content_vectors.rowid == content_chunks.rowid
```

This allows mapping: **Vector Search Result** → **Chunk** → **Full Document** → **Character Positions**

#### 4. `chunk_entities` - Entity Mentions (Local)

Stores entity extractions for local querying and deduplication.

```sql
CREATE TABLE chunk_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_rowid INTEGER NOT NULL,           -- FK to content_chunks
    content_id INTEGER NOT NULL,            -- FK to crawled_content

    entity_text TEXT NOT NULL,              -- Original: "FastAPI"
    entity_normalized TEXT,                 -- Lowercase: "fastapi"

    -- Hierarchical type system
    entity_type_primary TEXT,               -- "Framework"
    entity_type_sub1 TEXT,                  -- "Backend"
    entity_type_sub2 TEXT,                  -- "Python"
    entity_type_sub3 TEXT,                  -- Optional 3rd level

    confidence REAL,                        -- 0.0-1.0 from GLiNER
    offset_start INTEGER,                   -- Position within chunk
    offset_end INTEGER,

    neo4j_node_id TEXT,                     -- Reference to Neo4j Entity node elementId
    spans_multiple_chunks BOOLEAN DEFAULT 0, -- True if in overlap region
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (chunk_rowid) REFERENCES content_chunks(rowid) ON DELETE CASCADE,
    FOREIGN KEY (content_id) REFERENCES crawled_content(id) ON DELETE CASCADE
);

CREATE INDEX idx_chunk_entities_chunk ON chunk_entities(chunk_rowid);
CREATE INDEX idx_chunk_entities_entity ON chunk_entities(entity_text);
CREATE INDEX idx_chunk_entities_type ON chunk_entities(entity_type_primary, entity_type_sub1);
CREATE INDEX idx_chunk_entities_content ON chunk_entities(content_id);
CREATE INDEX idx_chunk_entities_neo4j ON chunk_entities(neo4j_node_id);
CREATE INDEX idx_chunk_entities_normalized ON chunk_entities(entity_normalized);
```

**Type Hierarchy Example**:
- `entity_text`: "FastAPI"
- `entity_normalized`: "fastapi"
- `type_primary`: "Framework"
- `type_sub1`: "Backend"
- `type_sub2`: "Python"
- `type_full`: "Framework.Backend.Python"

#### 5. `chunk_relationships` - Relationship Triples (Local)

Stores extracted relationships for local querying.

```sql
CREATE TABLE chunk_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id INTEGER NOT NULL,            -- FK to crawled_content

    subject_entity TEXT NOT NULL,           -- "FastAPI"
    predicate TEXT NOT NULL,                -- "uses", "competes_with", etc.
    object_entity TEXT NOT NULL,            -- "Pydantic"

    confidence REAL,                        -- 0.0-1.0 from vLLM
    context TEXT,                           -- Sentence where found

    neo4j_relationship_id TEXT,             -- Reference to Neo4j relationship
    spans_chunks BOOLEAN DEFAULT 0,         -- Entities in different chunks
    chunk_rowids TEXT,                      -- JSON array: [45001] or [45001, 45015]
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (content_id) REFERENCES crawled_content(id) ON DELETE CASCADE
);

CREATE INDEX idx_chunk_relationships_content ON chunk_relationships(content_id);
CREATE INDEX idx_chunk_relationships_subject ON chunk_relationships(subject_entity);
CREATE INDEX idx_chunk_relationships_object ON chunk_relationships(object_entity);
CREATE INDEX idx_chunk_relationships_predicate ON chunk_relationships(predicate);
```

#### 6. `kg_processing_queue` - KG Extraction Queue

Manages asynchronous knowledge graph extraction jobs.

```sql
CREATE TABLE kg_processing_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id INTEGER NOT NULL,

    status TEXT DEFAULT 'pending',          -- 'pending', 'processing', 'completed', 'failed', 'skipped'
    priority INTEGER DEFAULT 1,             -- Higher = process first

    queued_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    processing_started_at DATETIME,
    processed_at DATETIME,

    retry_count INTEGER DEFAULT 0,
    error_message TEXT,
    result_summary TEXT,                    -- JSON statistics
    skipped_reason TEXT,                    -- If status='skipped'

    FOREIGN KEY (content_id) REFERENCES crawled_content(id) ON DELETE CASCADE
);

CREATE INDEX idx_kg_queue_status ON kg_processing_queue(status, priority, queued_at);
CREATE INDEX idx_kg_queue_content ON kg_processing_queue(content_id);
```

**Queue States**:
1. `pending` → Content queued for KG extraction
2. `processing` → Currently being processed by KG service
3. `completed` → Extraction successful
4. `failed` → Extraction failed (with retry_count)
5. `skipped` → Intentionally skipped (e.g., KG service unavailable)

### Supporting Tables

#### 7. `sessions` - Session Tracking

```sql
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_active DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

#### 8. `blocked_domains` - Domain Blocklist

```sql
CREATE TABLE blocked_domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT UNIQUE NOT NULL,           -- "*.ru", "*porn*", etc.
    description TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

**Pattern Types**:
- `*.ru` - Suffix match (all Russian domains)
- `*porn*` - Substring match (keyword anywhere in URL)
- `example.com` - Exact domain match

### Data Flow

```
1. Web Crawl (Crawl4AI)
   ↓
2. Content Cleaning & Validation
   ↓
3. Store in crawled_content
   ↓
4. Chunk content (1000 chars)
   ↓
5. Generate embeddings → content_vectors
   ↓
6. Store chunk metadata → content_chunks
   ↓
7. Queue for KG processing → kg_processing_queue
   ↓
8. [Async] KG Service extracts entities & relationships
   ↓
9. Store in chunk_entities, chunk_relationships (SQLite)
   ↓
10. Store in Neo4j graph (see below)
```

---

## Neo4j Graph Database

**Connection**: `bolt://localhost:7687` (default, see `NEO4J_URI` in `.env`)

**Database**: `neo4j` (default, see `NEO4J_DATABASE`)

**Authentication**: Username `neo4j`, password from `NEO4J_PASSWORD`

### Node Types

#### 1. `Document` - Document Nodes

```cypher
(:Document {
    content_id: INTEGER,        -- Primary key, matches SQLite crawled_content.id
    url: STRING,                -- Document URL
    title: STRING,              -- Document title
    created_at: DATETIME,       -- When first created
    updated_at: DATETIME        -- Last modified
})
```

**Constraints**:
```cypher
CREATE CONSTRAINT document_content_id_unique IF NOT EXISTS
FOR (d:Document) REQUIRE d.content_id IS UNIQUE;
```

**Note**: Full markdown and metadata are **NOT** stored in Neo4j. Use `content_id` to fetch from SQLite.

#### 2. `Chunk` - Chunk Nodes

```cypher
(:Chunk {
    vector_rowid: INTEGER,      -- Primary key, matches SQLite content_vectors.rowid
    chunk_index: INTEGER,       -- Sequence: 0, 1, 2, ...
    char_start: INTEGER,        -- Start position in document
    char_end: INTEGER,          -- End position
    text_preview: STRING,       -- First 200 chars
    created_at: DATETIME
})
```

**Constraints**:
```cypher
CREATE CONSTRAINT chunk_vector_rowid_unique IF NOT EXISTS
FOR (c:Chunk) REQUIRE c.vector_rowid IS UNIQUE;
```

**Relationships**:
```cypher
(Document)-[:HAS_CHUNK]->(Chunk)
```

#### 3. `Entity` - Named Entity Nodes

```cypher
(:Entity {
    normalized: STRING,         -- Primary key (lowercase entity)
    text: STRING,              -- Original entity text (first occurrence)

    -- Hierarchical type system
    type_primary: STRING,      -- "Framework", "Language", "Concept", etc.
    type_sub1: STRING,         -- "Backend", "Interpreted", etc.
    type_sub2: STRING,         -- "Python", "DynamicTyped", etc.
    type_sub3: STRING,         -- Optional 3rd level
    type_full: STRING,         -- "Framework.Backend.Python"

    mention_count: INTEGER,    -- How many times mentioned across corpus
    avg_confidence: FLOAT,     -- Rolling average confidence
    created_at: DATETIME,
    updated_at: DATETIME
})
```

**Constraints**:
```cypher
CREATE CONSTRAINT entity_normalized_unique IF NOT EXISTS
FOR (e:Entity) REQUIRE e.normalized IS UNIQUE;
```

**Merge Behavior**:
- Entities are **deduplicated by normalized text**
- `mention_count` increments on each new mention
- `avg_confidence` is recalculated as rolling average

### Relationship Types

#### 1. `MENTIONED_IN` - Entity → Chunk Mentions

```cypher
(Entity)-[:MENTIONED_IN {
    offset_start: INTEGER,      -- Character offset in chunk
    offset_end: INTEGER,
    confidence: FLOAT,          -- Extraction confidence
    context_before: STRING,     -- Text before mention (100 chars)
    context_after: STRING,      -- Text after mention (100 chars)
    sentence: STRING,           -- Containing sentence (500 chars)
    created_at: DATETIME
}]->(Chunk)
```

**Usage**: Find all chunks where an entity appears, with surrounding context.

#### 2. Dynamic Semantic Relationships - Entity → Entity

Created from relationship extraction (vLLM). Relationship type is **dynamic** based on the predicate.

**Examples**:
```cypher
(Entity {text: "FastAPI"})-[:USES {
    confidence: FLOAT,
    context: STRING,            -- Supporting sentence (500 chars)
    occurrence_count: INTEGER,  -- How many times this relationship seen
    created_at: DATETIME,
    updated_at: DATETIME
}]->(Entity {text: "Pydantic"})

(Entity {text: "React"})-[:COMPETES_WITH]->(Entity {text: "Vue"})
(Entity {text: "Python"})-[:SUPPORTS]->(Entity {text: "Type Hints"})
```

**Relationship Type Naming**:
- Predicate text is **uppercased** and spaces replaced with underscores
- Examples: "uses" → `USES`, "competes with" → `COMPETES_WITH`

**Merge Behavior**:
- Relationships are **deduplicated** by (subject, predicate, object)
- `occurrence_count` increments on each rediscovery
- `confidence` is recalculated as rolling average

#### 3. `CO_OCCURS_WITH` - Entity Co-occurrence

Tracks entities that appear in the **same chunks**.

```cypher
(Entity)-[:CO_OCCURS_WITH {
    count: INTEGER,             -- Number of shared chunks
    chunk_rowids: [INTEGER],    -- Array of chunk rowids
    created_at: DATETIME,
    updated_at: DATETIME
}]->(Entity)
```

**Directionality**: Ensured consistent by `normalized1 < normalized2` ordering.

**Usage**: Find related entities based on co-occurrence frequency.

### Graph Schema Visualization

```
┌─────────────┐
│  Document   │
│ content_id  │
└──────┬──────┘
       │ :HAS_CHUNK
       ↓
   ┌────────┐
   │ Chunk  │
   │rowid   │
   └───┬────┘
       ↑ :MENTIONED_IN
       │
   ┌───┴────┐
   │ Entity │────:USES────→ Entity
   │normalized                │
   └────────┘←──:CO_OCCURS_WITH
```

### Indexes

```cypher
-- Node indexes
CREATE INDEX entity_type_primary IF NOT EXISTS
FOR (e:Entity) ON (e.type_primary);

CREATE INDEX entity_mention_count IF NOT EXISTS
FOR (e:Entity) ON (e.mention_count);

CREATE INDEX chunk_vector_rowid_index IF NOT EXISTS
FOR (c:Chunk) ON (c.vector_rowid);

-- Relationship indexes (if needed)
CREATE INDEX mentioned_in_confidence IF NOT EXISTS
FOR ()-[m:MENTIONED_IN]-() ON (m.confidence);
```

---

## Hybrid Query Patterns

### Pattern 1: Vector Search → Full Document Retrieval

**Use Case**: Get full documents from vector similarity results.

```sql
-- Step 1: Vector search returns rowids
SELECT rowid, distance
FROM content_vectors
WHERE embedding MATCH ? AND k = 3;

-- Step 2: Map rowid → content_id → full document
WITH chunk_scores AS (
    SELECT rowid, distance FROM content_vectors WHERE ...
)
SELECT cc.url, cc.title, cc.markdown
FROM chunk_scores cs
JOIN content_chunks ck ON ck.rowid = cs.rowid
JOIN crawled_content cc ON cc.id = ck.content_id
GROUP BY cc.id
ORDER BY MIN(cs.distance);
```

### Pattern 2: Entity Expansion via Neo4j → Document Retrieval

**Use Case**: Find documents containing related entities.

```cypher
-- Step 1: Find search entity and expand via relationships
MATCH (search:Entity {text: "FastAPI"})
OPTIONAL MATCH (search)-[:MENTIONED_IN]->(chunk:Chunk)
OPTIONAL MATCH (chunk)<-[:MENTIONED_IN]-(cooccur:Entity)
RETURN collect(DISTINCT chunk.vector_rowid) AS chunk_rowids
```

```sql
-- Step 2: Retrieve documents from chunk rowids
SELECT cc.id, cc.url, cc.title, cc.markdown,
       COUNT(DISTINCT ck.rowid) AS chunk_hit_count
FROM content_chunks ck
JOIN crawled_content cc ON cc.id = ck.content_id
WHERE ck.rowid IN (rowids_from_neo4j)
GROUP BY cc.id
ORDER BY chunk_hit_count DESC;
```

### Pattern 3: Enhanced Search (RAG + KG Hybrid)

Implemented in `robaitragmcp/core/search/enhanced_search.py`:

1. **Parallel Execution**:
   - Vector search (top 3 RAG results)
   - KG entity expansion (find 100 related entities, get chunks)

2. **Chunk → Document Aggregation**:
   - Map `vector_rowid` → `content_chunks` → `crawled_content`
   - Group chunks by `content_id`

3. **Ranking by Entity Density**:
   - **Tier 1** (1.0): Documents with multiple search term entities
   - **Tier 2** (0.7): 1 search term + expanded entities
   - **Tier 3** (0.5): Single search term entity only

4. **Merge Results**:
   - Top 3 RAG documents
   - Top 5 unique KG documents (deduplicated)

---

## Obsolete Files

The following files in this directory are **empty** (0 bytes) and can be safely deleted:

- `crawl4ai.db` - Obsolete artifact from old `mcpragcrawl4ai` project
- `knowledge.db` - Obsolete artifact, never used

**Active Database**: `crawl4ai_rag.db` (336 MB)

---

## Configuration

See `robaitools/.env` for configuration:

```bash
# SQLite Configuration
DB_PATH=/app/data/crawl4ai_rag.db
USE_MEMORY_DB=true                    # RAM-first mode with background sync

# Neo4j Configuration
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=knowledge_graph_2024
NEO4J_DATABASE=neo4j
```

---

## Maintenance

### Database Size Monitoring

```bash
# Check database size
ls -lh /home/robiloo/Documents/robaitools/robaidata/crawl4ai_rag.db

# Get database statistics
curl http://localhost:8080/api/v1/stats
```

### Backup Recommendations

1. **SQLite**: Copy `crawl4ai_rag.db` file
   ```bash
   cp crawl4ai_rag.db crawl4ai_rag.db.backup.$(date +%Y%m%d)
   ```

2. **Neo4j**: Use Neo4j dump command
   ```bash
   docker exec neo4j-kg neo4j-admin database dump neo4j --to-path=/backups
   ```

### Cleanup Queries

```sql
-- Remove old session-only content
DELETE FROM crawled_content
WHERE retention_policy = 'session_only'
  AND timestamp < datetime('now', '-7 days');

-- Remove orphaned chunks (shouldn't happen with CASCADE)
DELETE FROM content_chunks
WHERE content_id NOT IN (SELECT id FROM crawled_content);
```

```cypher
// Remove orphaned chunks in Neo4j
MATCH (c:Chunk)
WHERE NOT (c)<-[:HAS_CHUNK]-(:Document)
DELETE c;

// Remove entities with no mentions
MATCH (e:Entity)
WHERE NOT (e)-[:MENTIONED_IN]->(:Chunk)
DELETE e;
```

---

## Performance Tuning

### SQLite Optimization

```sql
-- Analyze query planner statistics
ANALYZE;

-- Rebuild indexes (if needed)
REINDEX;

-- Vacuum to reclaim space
VACUUM;
```

### Neo4j Optimization

```cypher
// Update graph statistics
CALL db.stats.collect();

// Check index usage
CALL db.indexes();

// Warm up caches
MATCH (n) RETURN count(n);
MATCH ()-[r]->() RETURN count(r);
```

---

## Troubleshooting

### Issue: Vector search returns no results

**Check**:
1. Ensure `content_vectors` has embeddings: `SELECT COUNT(*) FROM content_vectors;`
2. Verify `sqlite-vec` loaded: `SELECT vec_version();`
3. Check embedding dimensions: Should be 384

### Issue: KG processing stuck in 'pending'

**Check**:
1. KG service health: `curl http://localhost:8088/health`
2. Queue status: `SELECT status, COUNT(*) FROM kg_processing_queue GROUP BY status;`
3. Check errors: `SELECT * FROM kg_processing_queue WHERE status = 'failed';`

### Issue: Neo4j connection failed

**Check**:
1. Neo4j running: `docker ps | grep neo4j-kg`
2. Password correct in `.env`
3. Network connectivity: `nc -zv localhost 7687`

---

## Schema Version

**SQLite Schema Version**: 2.0 (with KG integration)
**Neo4j Schema Version**: 1.0
**Last Updated**: 2025-10-21
