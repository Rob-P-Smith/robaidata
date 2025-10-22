# robaidata

**Shared Data Storage Layer for RobAI Tools**

robaidata is the centralized data persistence layer that implements a hybrid RAG (Retrieval-Augmented Generation) and Knowledge Graph architecture for the RobAI Tools ecosystem. It provides shared storage for vector embeddings, document content, and knowledge graph coordination.

## Overview

robaidata serves as the **single source of truth** for all data across the RobAI system, combining:

- **SQLite Database** - Fast vector search, document storage, and chunk management
- **Neo4j Integration** - Knowledge graph with entities and relationships
- **KG Coordination Layer** - Background workers for asynchronous graph extraction
- **Model Cache** - Shared embedding models across services

### Position in the Ecosystem

```
┌─────────────────────────────────────────────────────────────┐
│                   Shared Data Layer                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  [robaidata]                                                │
│      ├─ crawl4ai_rag.db (SQLite)                            │
│      ├─ kg_coordinator/ (Background Workers)                │
│      └─ models/ (Embedding Cache)                           │
│                                                             │
│  Used by ALL services:                                      │
│  ├─ robaitragmcp (primary RAG database)                     │
│  ├─ robairagapi (shared access)                             │
│  ├─ robaikg (KG processing results)                         │
│  └─ robaimodeltools (model management)                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Service Level**: Infrastructure (Shared by all services)
- **No direct dependencies** - Mounted as volume
- **Critical for**: All RAG and KG operations
- **Failure impact**: Data loss, service unavailability

## What It Provides

### Core Capabilities

1. **Hybrid Storage Architecture**
   - SQLite for vector search and RAG operations
   - Neo4j integration for knowledge graph traversal
   - Synchronized via content_id and vector_rowid
   - Bidirectional reference system

2. **Vector Similarity Search**
   - 384-dimensional embeddings (all-MiniLM-L6-v2)
   - sqlite-vec extension for fast search
   - Chunk-level granularity with position tracking
   - Sub-second query performance

3. **Knowledge Graph Coordination**
   - Background worker pool for async KG extraction
   - Queue-based processing with retry logic
   - Health monitoring and graceful degradation
   - Web dashboard for monitoring

4. **Shared Model Cache**
   - Sentence transformers (175 MB cached)
   - Reduces download time and disk usage
   - Consistent embeddings across services

### Directory Structure

```
robaidata/
├── crawl4ai_rag.db              # Main SQLite database (349 MB)
├── crawl4ai_rag.db-wal          # Write-ahead log (4.5 MB)
├── crawl4ai_rag.db-shm          # Shared memory file (32 KB)
├── README.md                    # This file
├── DATAREADME.md                # Detailed schema documentation
├── __init__.py                  # Package initialization
├── kg_coordinator/              # Knowledge Graph coordination layer
│   ├── __init__.py
│   ├── kg_config.py             # KG service configuration
│   ├── kg_queue.py              # Queue management
│   ├── kg_worker.py             # Background worker
│   ├── kg_manager.py            # Worker pool orchestrator
│   ├── kg_dashboard.py          # Monitoring dashboard
│   ├── KG_MANAGER_EXPLAINED.md  # Manager documentation
│   └── templates/
│       └── dashboard.html       # Web dashboard
└── models/                      # ML model cache (175 MB)
    └── hub/
        └── models--sentence-transformers--all-MiniLM-L6-v2/
```

## Database Schema

### SQLite Database: `crawl4ai_rag.db`

**Current Size**: 349 MB (4,105 documents)

**Mode**: RAM-first with background disk sync (configurable via `USE_MEMORY_DB`)

**Embedding Model**: all-MiniLM-L6-v2 (384 dimensions)

### Core Tables

#### 1. `crawled_content` - Document Storage

Stores crawled web pages with full content and metadata.

**Key Fields**:
- `id` - Unique content_id (primary key)
- `url` - Source URL (unique)
- `title` - Page title
- `content` - Cleaned plaintext
- `markdown` - Full markdown (used for KG extraction)
- `content_hash` - SHA256 for deduplication
- `timestamp` - When crawled
- `kg_processed` - KG extraction completed?
- `kg_entity_count` - Number of entities extracted
- `kg_relationship_count` - Number of relationships extracted
- `kg_document_id` - Neo4j Document node elementId

**Current Data**: 4,105 rows

---

#### 2. `content_vectors` - Vector Embeddings (Virtual Table)

Uses `sqlite-vec` extension for vector similarity search.

**Schema**:
```sql
CREATE VIRTUAL TABLE content_vectors USING vec0(
    embedding FLOAT[384],  -- 384-dim vector
    content_id INTEGER     -- FK to crawled_content.id
);
```

**Implementation**:
- Documents chunked (1000 chars default, 0 overlap)
- Navigation chunks filtered before embedding
- Multiple embeddings per document (1 per chunk)
- `rowid` links to `content_chunks` table

---

#### 3. `content_chunks` - Chunk Metadata

Links vector embeddings to document positions.

**Key Fields**:
- `rowid` - Same as content_vectors.rowid (primary key)
- `content_id` - FK to crawled_content
- `chunk_index` - Sequential: 0, 1, 2, ...
- `chunk_text` - Full chunk text
- `char_start` - Position in original markdown
- `char_end` - End position
- `word_count` - Words in chunk
- `kg_processed` - KG extraction done?

**Critical Relationship**:
```
content_vectors.rowid == content_chunks.rowid
```

This enables: **Vector Search Result** → **Chunk** → **Full Document** → **Character Positions**

---

#### 4. `chunk_entities` - Entity Mentions

Stores entity extractions from GLiNER for local querying.

**Key Fields**:
- `chunk_rowid` - FK to content_chunks
- `entity_text` - Original: "FastAPI"
- `entity_normalized` - Lowercase: "fastapi"
- `entity_type_primary` - "Framework"
- `entity_type_sub1` - "Backend"
- `entity_type_sub2` - "Python"
- `entity_type_sub3` - Optional 3rd level
- `confidence` - 0.0-1.0 from GLiNER
- `neo4j_node_id` - Reference to Neo4j Entity node

**Type Hierarchy Example**:
```
entity_text: "FastAPI"
entity_normalized: "fastapi"
type_primary: "Framework"
type_sub1: "Backend"
type_sub2: "Python"
type_full: "Framework.Backend.Python"
```

---

#### 5. `chunk_relationships` - Relationship Triples

Stores extracted relationships for local querying.

**Key Fields**:
- `subject_entity` - "FastAPI"
- `predicate` - "uses", "competes_with", etc.
- `object_entity` - "Pydantic"
- `confidence` - 0.0-1.0 from vLLM
- `context` - Sentence where found
- `neo4j_relationship_id` - Reference to Neo4j relationship
- `chunk_rowids` - JSON array of chunk IDs

---

#### 6. `kg_processing_queue` - KG Extraction Queue

Manages asynchronous knowledge graph extraction jobs.

**Key Fields**:
- `content_id` - FK to crawled_content
- `status` - 'pending', 'processing', 'completed', 'failed', 'skipped'
- `priority` - Higher = process first
- `retry_count` - Number of retry attempts
- `error_message` - If failed
- `result_summary` - JSON statistics

**Queue States**:
1. `pending` → Queued for KG extraction
2. `processing` → Currently being processed
3. `completed` → Extraction successful
4. `failed` → Extraction failed (with retry)
5. `skipped` → Intentionally skipped (e.g., service unavailable)

---

#### 7. Supporting Tables

- `sessions` - Session tracking for retention policies
- `blocked_domains` - Domain blocklist (suffix, substring, exact matching)

### Neo4j Graph Database

**Connection**: `bolt://localhost:7687`

**Database**: `neo4j` (default)

**Authentication**: Username `neo4j`, password from `NEO4J_PASSWORD`

### Node Types

#### 1. `Document` Nodes
```cypher
(:Document {
    content_id: INTEGER,        -- Matches SQLite crawled_content.id
    url: STRING,
    title: STRING,
    created_at: DATETIME,
    updated_at: DATETIME
})
```

#### 2. `Chunk` Nodes
```cypher
(:Chunk {
    vector_rowid: INTEGER,      -- Matches SQLite content_vectors.rowid
    chunk_index: INTEGER,
    char_start: INTEGER,
    char_end: INTEGER,
    text_preview: STRING,       -- First 200 chars
    created_at: DATETIME
})
```

#### 3. `Entity` Nodes
```cypher
(:Entity {
    normalized: STRING,         -- Primary key (lowercase)
    text: STRING,              -- Original entity text
    type_primary: STRING,
    type_sub1: STRING,
    type_sub2: STRING,
    type_sub3: STRING,
    type_full: STRING,
    mention_count: INTEGER,     -- Corpus-wide count
    avg_confidence: FLOAT,
    created_at: DATETIME,
    updated_at: DATETIME
})
```

### Relationship Types

1. **`HAS_CHUNK`** - Document → Chunk
2. **`MENTIONED_IN`** - Entity → Chunk (with context)
3. **Dynamic Semantic** - Entity → Entity (e.g., USES, COMPETES_WITH)
4. **`CO_OCCURS_WITH`** - Entity → Entity (co-occurrence tracking)

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

## Knowledge Graph Coordination

### KG Coordinator Components

#### **KGServiceConfig** (`kg_config.py`)

Global configuration and health checking for KG service.

**Features**:
- Health checking with exponential backoff
- Graceful fallback when service unavailable
- Configuration: enabled status, URL, timeout, retry logic
- Global singleton instance

**Usage**:
```python
from robaidata.kg_coordinator import kg_service_config

if kg_service_config.is_available():
    # Process with KG service
else:
    # Fallback to vector-only search
```

---

#### **KGQueueManager** (`kg_queue.py`)

Queue management and chunk tracking.

**Responsibilities**:
- Calculate chunk boundaries in markdown
- Store chunk metadata with character positions
- Manage KG processing queue
- Write results back to SQLite
- Track processing status

**Key Methods**:
- `queue_for_kg_processing(content_id, priority)`
- `update_kg_processing_status(content_id, status)`
- `write_kg_results(content_id, entities, relationships)`

---

#### **KGWorker** (`kg_worker.py`)

Background task that processes the queue.

**Behavior**:
- Polls queue every 5 seconds (configurable)
- Processes 5 items per batch
- Sends documents to KG service via HTTP POST
- Handles stale 'processing' items (>60 min reset)
- Tracks per-worker statistics

**States Tracked**:
- Total processed
- Successful extractions
- Failed extractions
- Current status

---

#### **KGWorkerManager** (`kg_manager.py`)

Orchestrates worker pool.

**Features**:
- Start/stop multiple workers (1-5 recommended)
- Health monitoring (detects crashed workers)
- Aggregate statistics from all workers
- Graceful restart capability
- Global singleton instance

**Usage**:
```python
from robaidata.kg_coordinator import kg_worker_manager

# Start worker pool
kg_worker_manager.start_workers(num_workers=2)

# Get statistics
stats = kg_worker_manager.get_statistics()

# Stop all workers
kg_worker_manager.stop_all_workers()
```

---

#### **KGDashboard** (`kg_dashboard.py`)

Web-based monitoring interface.

**Access**: Typically integrated into main API

**Features**:
- Real-time queue status
- Worker health visualization
- Service statistics dashboard
- Processing throughput metrics

## Data Flow

### Content Ingestion Pipeline

```
1. Web Crawl (Crawl4AI)
   ↓
2. Content Cleaning & Validation
   ↓
3. Store in crawled_content (id assigned)
   ↓
4. Chunk content (1000 chars default)
   ↓
5. Generate embeddings → content_vectors (rowid assigned)
   ↓
6. Store chunk metadata → content_chunks (rowid = vector rowid)
   ↓
7. Queue for KG processing → kg_processing_queue (status: pending)
   ↓
8. [Async Background] KG Worker polls queue every 5s
   ↓
9. KG Service extracts entities & relationships
   ↓
10. Store in chunk_entities, chunk_relationships (SQLite)
    ↓
11. Store in Neo4j graph database
```

### Query Patterns

#### Pattern 1: Vector Search → Document Retrieval

**Use Case**: Get full documents from vector similarity results.

```sql
-- Step 1: Vector search returns rowids
SELECT rowid, distance
FROM content_vectors
WHERE embedding MATCH ? AND k = 3;

-- Step 2: Map rowid → content_id → full document
SELECT cc.url, cc.title, cc.markdown
FROM content_chunks ck
JOIN crawled_content cc ON cc.id = ck.content_id
WHERE ck.rowid IN (rowids_from_search)
GROUP BY cc.id
ORDER BY MIN(distance);
```

---

#### Pattern 2: Entity Expansion via Neo4j

**Use Case**: Find documents containing related entities.

```cypher
-- Step 1: Find entity and expand
MATCH (search:Entity {text: "FastAPI"})
OPTIONAL MATCH (search)-[:MENTIONED_IN]->(chunk:Chunk)
OPTIONAL MATCH (chunk)<-[:MENTIONED_IN]-(cooccur:Entity)
RETURN collect(DISTINCT chunk.vector_rowid) AS chunk_rowids
```

```sql
-- Step 2: Retrieve documents
SELECT cc.id, cc.url, cc.title, cc.markdown,
       COUNT(DISTINCT ck.rowid) AS chunk_hit_count
FROM content_chunks ck
JOIN crawled_content cc ON cc.id = ck.content_id
WHERE ck.rowid IN (rowids_from_neo4j)
GROUP BY cc.id
ORDER BY chunk_hit_count DESC;
```

---

#### Pattern 3: Hybrid Search (RAG + KG)

Implemented in `robaitragmcp/core/search/enhanced_search.py`:

1. **Parallel Execution**:
   - Vector search (top 3 RAG results)
   - KG entity expansion (100 related entities, get chunks)

2. **Chunk → Document Aggregation**:
   - Map `vector_rowid` → `content_chunks` → `crawled_content`
   - Group chunks by `content_id`

3. **Ranking by Entity Density**:
   - **Tier 1** (1.0): Multiple search term entities
   - **Tier 2** (0.7): 1 search term + expanded entities
   - **Tier 3** (0.5): Single search term only

4. **Merge Results**:
   - Top 3 RAG documents
   - Top 5 unique KG documents (deduplicated)

## Service Integration

### Volume Mounting

robaidata is mounted as a volume in multiple services:

**Docker Compose Configuration**:
```yaml
services:
  robaitragmcp:
    volumes:
      - ${ROBAI_DATA_PATH:-./robaidata}:/data
    environment:
      - DB_PATH=/data/crawl4ai_rag.db

  robairagapi:
    volumes:
      - ${ROBAI_DATA_PATH:-./robaidata}:/data
    environment:
      - DB_PATH=/data/crawl4ai_rag.db
```

### Accessing the Database

#### From Python Services

```python
from robaimodeltools.storage import RAGDatabase

# Initialize database connection
db = RAGDatabase(db_path="/data/crawl4ai_rag.db")

# Vector search
results = await db.vector_search(query_embedding, top_k=5)

# Store content
content_id = await db.store_content(
    url="https://example.com",
    title="Example",
    markdown="# Content here"
)

# Queue for KG processing
from robaidata.kg_coordinator import kg_queue_manager
await kg_queue_manager.queue_for_kg_processing(content_id)
```

#### From KG Service

```python
from robaidata.kg_coordinator import kg_worker_manager

# Start workers on service startup
kg_worker_manager.start_workers(num_workers=2)
```

## Configuration

### Environment Variables

```bash
# SQLite Configuration
DB_PATH=/data/crawl4ai_rag.db
USE_MEMORY_DB=true                    # RAM-first with background sync

# Neo4j Configuration
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=knowledge_graph_2024
NEO4J_DATABASE=neo4j

# KG Service Configuration
KG_SERVICE_ENABLED=true
KG_SERVICE_URL=http://localhost:8088
KG_SERVICE_TIMEOUT=1800.0             # 30 minutes
KG_NUM_WORKERS=2                      # Worker pool size
KG_POLL_INTERVAL=5.0                  # Queue poll frequency (seconds)
KG_BATCH_SIZE=5                       # Items per batch
```

### Performance Tuning

**RAM Mode** (`USE_MEMORY_DB=true`):
- Loads database into memory for faster queries
- Background sync to disk periodically
- Recommended for production

**Worker Pool Size**:
- 1 worker: Basic processing
- 2 workers: 2x throughput (recommended)
- 3-5 workers: High throughput (requires more CPU)

**Poll Interval**:
- 5 seconds: Balanced (default)
- 1 second: Low latency (higher CPU)
- 10 seconds: Low overhead (higher latency)

## Monitoring & Maintenance

### Database Statistics

**Check database size**:
```bash
ls -lh /home/robiloo/Documents/robaitools/robaidata/crawl4ai_rag.db
```

**Get statistics via API**:
```bash
curl http://localhost:8080/api/v1/stats
```

**SQLite query**:
```sql
-- Document count
SELECT COUNT(*) FROM crawled_content;

-- Embedding count
SELECT COUNT(*) FROM content_vectors;

-- Queue status
SELECT status, COUNT(*) FROM kg_processing_queue GROUP BY status;

-- KG processing stats
SELECT
    COUNT(*) as total,
    SUM(kg_processed) as processed,
    AVG(kg_entity_count) as avg_entities
FROM crawled_content;
```

### Backup & Recovery

**SQLite Backup**:
```bash
# Stop services first
docker compose stop

# Backup database
cp robaidata/crawl4ai_rag.db robaidata/crawl4ai_rag.db.backup.$(date +%Y%m%d)

# Restart services
docker compose up -d
```

**Neo4j Backup**:
```bash
docker exec neo4j-kg neo4j-admin database dump neo4j --to-path=/backups
```

### Cleanup Operations

**Remove old session content**:
```sql
DELETE FROM crawled_content
WHERE retention_policy = 'session_only'
  AND timestamp < datetime('now', '-7 days');
```

**Vacuum to reclaim space**:
```sql
VACUUM;
ANALYZE;
```

**Neo4j cleanup**:
```cypher
// Remove orphaned chunks
MATCH (c:Chunk)
WHERE NOT (c)<-[:HAS_CHUNK]-(:Document)
DELETE c;

// Remove entities with no mentions
MATCH (e:Entity)
WHERE NOT (e)-[:MENTIONED_IN]->(:Chunk)
DELETE e;
```

## Troubleshooting

### Vector Search Returns No Results

**Check**:
```sql
-- Verify embeddings exist
SELECT COUNT(*) FROM content_vectors;

-- Verify sqlite-vec loaded
SELECT vec_version();
```

**Solution**: Ensure `sqlite-vec` extension is loaded and embeddings generated.

---

### KG Processing Stuck in 'pending'

**Check**:
```bash
# KG service health
curl http://localhost:8088/health

# Queue status
sqlite3 crawl4ai_rag.db "SELECT status, COUNT(*) FROM kg_processing_queue GROUP BY status;"
```

**Solution**:
```python
# Restart KG workers
from robaidata.kg_coordinator import kg_worker_manager
kg_worker_manager.restart_all_workers()
```

---

### Neo4j Connection Failed

**Check**:
```bash
# Neo4j running?
docker ps | grep neo4j-kg

# Network connectivity
nc -zv localhost 7687
```

**Solution**: Verify `NEO4J_PASSWORD` in `.env` and restart Neo4j service.

---

### Database Locked Errors

**Cause**: Multiple writers with WAL mode disabled

**Solution**: Enable WAL mode (should be default):
```sql
PRAGMA journal_mode=WAL;
```

## Resource Requirements

### Minimum
- **Disk**: 500 MB (database + model cache)
- **Memory**: 512 MB (database in memory)
- **CPU**: Minimal (background workers use ~10% per worker)

### Recommended
- **Disk**: 2 GB (for growth)
- **Memory**: 1 GB (RAM mode + overhead)
- **CPU**: 2 cores (for worker pool)

### Current Usage
- **Database**: 349 MB
- **Model Cache**: 175 MB
- **Total**: ~525 MB

## Technology Stack

- **Database**: SQLite 3 with WAL mode
- **Vector Extension**: sqlite-vec (binary search tree)
- **Embeddings**: sentence-transformers (all-MiniLM-L6-v2, 384-dim)
- **Graph Database**: Neo4j 5.x
- **Worker Framework**: Python asyncio
- **HTTP Client**: httpx (async)
- **Hash Function**: SHA256 (content deduplication)

## Schema Version

- **SQLite Schema Version**: 2.0 (with KG integration)
- **Neo4j Schema Version**: 1.0
- **Last Updated**: 2025-10-21

## Related Services

- **[robaitragmcp](../robaitragmcp/README.md)** - Primary RAG database consumer
- **[robairagapi](../robairagapi/README.md)** - Shared database access
- **[robaikg](../robaikg/README.md)** - KG service that processes queue
- **[robaimodeltools](../robaimodeltools/README.md)** - Database abstraction layer
- **[robaicrawler](../robaicrawler/README.md)** - Content source

## Additional Documentation

- **[DATAREADME.md](DATAREADME.md)** - Detailed schema documentation with all SQL DDL
- **[kg_coordinator/KG_MANAGER_EXPLAINED.md](kg_coordinator/KG_MANAGER_EXPLAINED.md)** - KG manager deep dive

## Contributing

This directory is part of the RobAI Tools monorepo. For contributions:

1. Never commit the actual database files (`*.db`, `*.db-wal`, `*.db-shm`)
2. Document schema changes in DATAREADME.md
3. Update version numbers when schema changes
4. Test migrations with existing data

## License

Part of RobAI Tools - See main repository for license information.
