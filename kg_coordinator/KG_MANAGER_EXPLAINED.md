# KG Manager - Detailed Explanation

## What is the KG Manager?

The **KG Manager** is a **worker pool orchestrator** that manages the lifecycle of multiple KG workers. Think of it as a **foreman** that supervises a team of workers, while the individual **KG Workers** are the ones actually doing the work.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    KG MANAGER (Orchestrator)                │
│                                                             │
│  Responsibilities:                                          │
│  • Start/stop worker pool                                   │
│  • Monitor worker health                                    │
│  • Aggregate statistics                                     │
│  • Restart failed workers                                   │
│  • Manage worker lifecycle                                  │
└────────────┬────────────────────────────────────────────────┘
             │ manages
             ↓
┌────────────────────────────────────────────────────────────┐
│                    WORKER POOL                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │ KG Worker #1 │  │ KG Worker #2 │  │ KG Worker #N │    │
│  │              │  │              │  │              │    │
│  │ • Poll queue │  │ • Poll queue │  │ • Poll queue │    │
│  │ • Process 5  │  │ • Process 5  │  │ • Process 5  │    │
│  │ • Send to KG │  │ • Send to KG │  │ • Send to KG │    │
│  │ • Write back │  │ • Write back │  │ • Write back │    │
│  └──────────────┘  └──────────────┘  └──────────────┘    │
└────────────────────────────────────────────────────────────┘
             │ all workers share
             ↓
┌────────────────────────────────────────────────────────────┐
│              SHARED RESOURCES                              │
│  • SQLite Database (GLOBAL_DB)                             │
│  • kg_processing_queue (job queue)                         │
│  • KG Service (http://localhost:8088)                      │
└────────────────────────────────────────────────────────────┘
```

---

## Core Functionality

### 1. **Start Worker Pool** - `start_workers()`

**Code**:
```python
from robaidata.kg_coordinator import start_kg_workers

await start_kg_workers(
    db_manager=GLOBAL_DB,
    num_workers=2,
    poll_interval=5.0
)
```

**What happens**:

1. **Manager creates worker instances**:
   ```python
   for i in range(num_workers):
       worker = KGWorker(db_manager, poll_interval=poll_interval)
       self.workers.append(worker)
   ```

2. **Each worker gets a background task**:
   ```python
   task = asyncio.create_task(worker.start())
   self.worker_tasks.append(task)
   ```

3. **Workers start polling independently**:
   - Worker #1 starts polling queue every 5 seconds
   - Worker #2 starts polling queue every 5 seconds
   - They run **concurrently** (not sequentially)

4. **Manager tracks state**:
   ```python
   self.running = True
   self.manager_stats["started_at"] = datetime.now()
   ```

**Result**:
```
✓ Started KG worker #1
✓ Started KG worker #2
✓ 2 KG worker(s) started successfully
```

**Key Point**: Workers are **independent**. If Worker #1 picks up 5 items, Worker #2 can simultaneously pick up the next 5 items.

---

### 2. **Stop Workers Gracefully** - `stop_workers()`

**Code**:
```python
from robaidata.kg_coordinator import stop_kg_workers

await stop_kg_workers()
```

**What happens**:

1. **Signal all workers to stop**:
   ```python
   for worker in self.workers:
       await worker.stop()  # Sets worker.running = False
   ```

2. **Cancel background tasks**:
   ```python
   for task in self.worker_tasks:
       if not task.done():
           task.cancel()
   ```

3. **Wait for tasks to complete**:
   ```python
   await asyncio.gather(*self.worker_tasks, return_exceptions=True)
   ```

4. **Clear state**:
   ```python
   self.workers.clear()
   self.worker_tasks.clear()
   self.running = False
   ```

**Result**:
```
Stopping 2 worker(s)...
✓ All workers stopped
```

**Key Point**: Graceful shutdown ensures no items are left in 'processing' state.

---

### 3. **Restart Workers** - `restart_workers()`

**Code**:
```python
from robaidata.kg_coordinator import restart_kg_workers

await restart_kg_workers(GLOBAL_DB, poll_interval=5.0)
```

**What happens**:

1. **Stop all current workers** (calls `stop_workers()`)
2. **Start new workers** (calls `start_workers()`)
3. **Increment restart counter**:
   ```python
   self.manager_stats["workers_restarted"] += 1
   ```

**Use cases**:
- Workers got stuck or unresponsive
- Need to reload configuration
- After fixing a bug in worker code

**Result**:
```
Restarting workers...
Stopping 2 worker(s)...
✓ All workers stopped
Starting 2 KG worker(s)...
✓ Started KG worker #1
✓ Started KG worker #2
✓ Workers restarted
```

---

### 4. **Check Worker Health** - `check_worker_health()`

**Code**:
```python
from robaidata.kg_coordinator import get_kg_manager

manager = get_kg_manager()
health = await manager.check_worker_health()
```

**What it returns**:
```python
{
    "manager_running": True,           # Is manager active?
    "total_workers": 2,                # How many workers total?
    "healthy_workers": 2,              # How many running?
    "unhealthy_workers": 0,            # How many crashed?
    "worker_details": [
        {
            "worker_id": 0,
            "running": True,           # Worker #1 status
            "stats": {
                "total_processed": 72,
                "total_success": 70,
                "total_failed": 2,
                "last_processed_at": "2025-10-21T23:45:23",
                "queue_size": {
                    "pending": 5,
                    "processing": 1,
                    "completed": 140,
                    "failed": 3
                }
            }
        },
        {
            "worker_id": 1,
            "running": True,           # Worker #2 status
            "stats": {
                "total_processed": 73,
                "total_success": 72,
                "total_failed": 1,
                ...
            }
        }
    ]
}
```

**Use cases**:
- Monitoring dashboard
- Alerting when workers crash
- Debugging which worker is having issues

**Example monitoring loop**:
```python
while True:
    await asyncio.sleep(60)  # Check every minute

    health = await manager.check_worker_health()

    if health["unhealthy_workers"] > 0:
        logger.warning(f"⚠ {health['unhealthy_workers']} workers failed!")
        await manager.restart_workers(GLOBAL_DB)
```

---

### 5. **Aggregate Statistics** - `get_aggregate_stats()`

**Code**:
```python
from robaidata.kg_coordinator import get_worker_stats

stats = get_worker_stats()
```

**What it returns**:
```python
{
    # Manager-level stats
    "total_workers": 2,
    "manager_running": True,
    "manager_started_at": "2025-10-21T23:00:00",
    "workers_restarted": 0,
    "last_health_check": "2025-10-21T23:45:15",

    # Aggregated worker stats (sum from all workers)
    "total_processed": 145,        # Worker1: 72 + Worker2: 73
    "total_success": 142,          # Worker1: 70 + Worker2: 72
    "total_failed": 3,             # Worker1: 2 + Worker2: 1

    # KG service status (from first worker)
    "kg_service_enabled": True,
    "kg_service_status": {
        "enabled": True,
        "url": "http://localhost:8088",
        "is_healthy": True,
        "last_health_check": "2025-10-21T23:45:00",
        "consecutive_failures": 0
    },

    # Queue status (shared across all workers, no aggregation)
    "queue_size": {
        "total": 150,
        "pending": 5,
        "processing": 2,           # Items currently being processed
        "completed": 140,
        "failed": 3
    }
}
```

**How aggregation works**:
- **Sum**: `total_processed`, `total_success`, `total_failed`
- **First worker**: KG service status (all workers see same service)
- **No aggregation**: Queue size (all workers see same queue)

**Use case**: Single API endpoint for monitoring tools.

---

## Comparison: Worker vs Manager

| Aspect | **KG Worker** | **KG Manager** |
|--------|--------------|---------------|
| **Purpose** | Process queue items | Orchestrate worker pool |
| **Scope** | Single worker instance | Multiple workers |
| **Main loop** | Poll queue every 5s | N/A (workers do polling) |
| **Processing** | Fetch 5 items, send to KG service | N/A (workers do processing) |
| **Statistics** | Individual worker stats | Aggregate all workers |
| **Health** | `worker.running` flag | Check all workers |
| **Lifecycle** | `start()`, `stop()` | `start_workers()`, `stop_workers()` |
| **Restart** | Must stop and recreate | `restart_workers()` for all |
| **File** | `kg_worker.py` | `kg_manager.py` |
| **Analogy** | Construction worker | Construction foreman |

---

## When to Use Manager vs Worker Directly

### **Use Manager (Production)**:

✅ **Running in production** - Need reliability and monitoring
✅ **Multiple workers** - Want 2+ workers for throughput
✅ **Need health checks** - Want to detect and restart failed workers
✅ **Need aggregate stats** - Want single view of all workers
✅ **Long-running service** - Workers run 24/7

**Example**: kg-service container auto-starting workers

### **Use Worker Directly (Development/Testing)**:

✅ **Testing** - Want to test single worker behavior
✅ **Debugging** - Want to step through worker logic
✅ **Simple scripts** - One-off processing tasks
✅ **Development** - Building new features

**Example**: Manual test script

---

## Real-World Example

### **Scenario**: Processing 1000 documents

#### **Without Manager (1 Worker)**:
```python
from robaidata.kg_coordinator import KGWorker

worker = KGWorker(GLOBAL_DB, poll_interval=5.0)
await worker.start()

# Worker processes in batches of 5 every 5 seconds
# 1000 documents ÷ 5 per batch = 200 batches
# 200 batches × 5 seconds = 1000 seconds (~17 minutes)
```

**Throughput**: ~60 documents/hour

#### **With Manager (4 Workers)**:
```python
from robaidata.kg_coordinator import start_kg_workers

await start_kg_workers(GLOBAL_DB, num_workers=4)

# 4 workers × 5 items per batch = 20 items per cycle
# 1000 documents ÷ 20 per cycle = 50 cycles
# 50 cycles × 5 seconds = 250 seconds (~4 minutes)
```

**Throughput**: ~240 documents/hour (4x faster!)

**Plus**:
- Automatic restart if worker crashes
- Health monitoring
- Aggregate statistics
- Graceful shutdown

---

## Current Integration

### **In kg-service Container** ([kg-service/api/server.py](../robaikg/kg-service/api/server.py:116-144)):

```python
# Startup (lifespan context manager)
from robaidata.kg_coordinator import start_kg_workers

# Get configuration from environment
num_workers = int(os.getenv("KG_NUM_WORKERS", "1"))
poll_interval = float(os.getenv("KG_POLL_INTERVAL", "5.0"))

# Start workers (uses manager internally)
worker_task = asyncio.create_task(
    start_kg_workers(GLOBAL_DB, num_workers, poll_interval)
)

# Shutdown (lifespan context manager)
from robaidata.kg_coordinator import stop_kg_workers

await stop_kg_workers()
worker_task.cancel()
```

**What happens**:
1. Container starts → kg-service API initializes
2. Manager creates 2 workers (from `KG_NUM_WORKERS=2`)
3. Workers start polling queue every 5 seconds
4. Workers run continuously until container stops
5. On shutdown, manager stops all workers gracefully

---

## Configuration

All manager settings come from environment variables:

```bash
# .env file
KG_NUM_WORKERS=2              # Number of workers (1-5 recommended)
KG_POLL_INTERVAL=5.0          # Seconds between queue polls
KG_SERVICE_ENABLED=true       # Enable/disable KG processing
KG_SERVICE_URL=http://localhost:8088
KG_SERVICE_TIMEOUT=1800.0     # 30 minutes
```

### **Tuning Recommendations**:

| System | RAM | Cores | Workers | Poll Interval |
|--------|-----|-------|---------|--------------|
| **Low-end** | 8GB | 4 | 1 | 10.0s |
| **Mid-range** | 16GB | 8 | 2 | 5.0s |
| **High-end** | 32GB+ | 16+ | 4 | 3.0s |

**Why not more workers?**
- Each worker holds database connections
- Too many workers = database contention
- Diminishing returns after 4-5 workers
- KG service becomes bottleneck, not workers

---

## Code Structure

### **Key Methods in kg_manager.py**:

```python
class KGWorkerManager:
    def __init__(self):
        self.workers = []              # List of KGWorker instances
        self.worker_tasks = []         # List of asyncio.Task objects
        self.running = False
        self.manager_stats = {...}

    async def start_workers(self, db_manager, num_workers, poll_interval):
        """Create and start worker pool"""
        for i in range(num_workers):
            worker = KGWorker(db_manager, poll_interval)
            self.workers.append(worker)
            task = asyncio.create_task(worker.start())
            self.worker_tasks.append(task)

    async def stop_workers(self):
        """Stop all workers gracefully"""
        for worker in self.workers:
            await worker.stop()
        for task in self.worker_tasks:
            task.cancel()
        await asyncio.gather(*self.worker_tasks, return_exceptions=True)

    async def restart_workers(self, db_manager, poll_interval):
        """Restart all workers"""
        await self.stop_workers()
        await self.start_workers(db_manager, num_workers, poll_interval)

    async def check_worker_health(self):
        """Check health of all workers"""
        health = {"healthy_workers": 0, "unhealthy_workers": 0, ...}
        for worker in self.workers:
            if worker.running:
                health["healthy_workers"] += 1
            else:
                health["unhealthy_workers"] += 1
        return health

    def get_aggregate_stats(self):
        """Get combined stats from all workers"""
        aggregate = {"total_processed": 0, "total_success": 0, ...}
        for worker in self.workers:
            stats = worker.get_stats()
            aggregate["total_processed"] += stats["total_processed"]
            aggregate["total_success"] += stats["total_success"]
        return aggregate
```

### **Convenience Functions**:

```python
# Global singleton instance
_kg_manager = None

def get_kg_manager() -> KGWorkerManager:
    """Get or create global manager"""
    global _kg_manager
    if _kg_manager is None:
        _kg_manager = KGWorkerManager()
    return _kg_manager

async def start_kg_workers(db_manager, num_workers=1, poll_interval=5.0):
    """Start workers (convenience function)"""
    manager = get_kg_manager()
    await manager.start_workers(db_manager, num_workers, poll_interval)

async def stop_kg_workers():
    """Stop workers (convenience function)"""
    manager = get_kg_manager()
    await manager.stop_workers()

def get_worker_stats():
    """Get stats (convenience function)"""
    manager = get_kg_manager()
    return manager.get_aggregate_stats()
```

---

## Summary

The **KG Manager** is a **production-grade orchestrator** that provides:

✅ **Worker Pool Management** - Start/stop multiple workers at once
✅ **Health Monitoring** - Detect and recover from failed workers
✅ **Aggregate Metrics** - Single view of entire system
✅ **Graceful Lifecycle** - Clean startup/shutdown
✅ **Restart Capabilities** - Recover from transient failures
✅ **Scalability** - Easy to add more workers for throughput

**Key Insight**: The manager doesn't process the queue itself. It **orchestrates** the workers that do the processing. Think of it as the **control plane** (manager) vs **data plane** (workers).

**In Production**: The manager runs inside the kg-service container, automatically starts 2 workers on startup, monitors their health, and provides aggregate statistics for monitoring.
