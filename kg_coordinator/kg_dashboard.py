"""
KG Dashboard - Web Interface for KG Manager

Provides a web dashboard for monitoring and managing KG workers and queue.
Automatically starts when KG Manager is running.
"""

import os
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

logger = logging.getLogger(__name__)


class KGDashboard:
    """Web dashboard for KG Manager"""

    def __init__(self, db_manager, kg_manager=None, host=None, port=None):
        """
        Initialize KG Dashboard

        Args:
            db_manager: RAGDatabase instance (GLOBAL_DB)
            kg_manager: KGWorkerManager instance (optional, will get singleton if None)
            host: Host to bind to (default: from env or 0.0.0.0)
            port: Port to bind to (default: from env or 8090)
        """
        self.db_manager = db_manager
        self.kg_manager = kg_manager
        self.host = host or os.getenv("KG_DASHBOARD_HOST", "0.0.0.0")
        self.port = int(port or os.getenv("KG_DASHBOARD_PORT", "8090"))
        self.server = None
        self.server_task = None

        # Create FastAPI app
        self.app = self._create_app()

    def _create_app(self) -> FastAPI:
        """Create FastAPI application"""
        app = FastAPI(
            title="KG Dashboard",
            description="Knowledge Graph Worker Management Dashboard",
            version="1.0.0"
        )

        # CORS middleware
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # Register routes
        self._register_routes(app)

        return app

    def _register_routes(self, app: FastAPI):
        """Register API routes"""

        # Get template directory
        template_dir = Path(__file__).parent / "templates"

        @app.get("/", response_class=HTMLResponse)
        async def dashboard_home():
            """Serve dashboard HTML"""
            html_file = template_dir / "dashboard.html"
            if not html_file.exists():
                return HTMLResponse("<h1>Dashboard template not found</h1>", status_code=404)

            with open(html_file, 'r') as f:
                return HTMLResponse(content=f.read())

        @app.get("/api/stats")
        async def get_stats():
            """Get dashboard statistics"""
            try:
                # Get content stats
                cursor = self.db_manager.execute_with_retry('''
                    SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN kg_processed = 1 THEN 1 ELSE 0 END) as processed,
                        SUM(CASE WHEN kg_processed = 0 THEN 1 ELSE 0 END) as not_processed
                    FROM crawled_content
                ''')
                content_stats = cursor.fetchone()

                # Get queue stats
                cursor = self.db_manager.execute_with_retry('''
                    SELECT status, COUNT(*) as count
                    FROM kg_processing_queue
                    GROUP BY status
                ''')
                queue_stats = {}
                for row in cursor.fetchall():
                    queue_stats[row[0]] = row[1]

                # Get entity stats
                cursor = self.db_manager.execute_with_retry('''
                    SELECT COUNT(DISTINCT entity_normalized) as unique_entities
                    FROM chunk_entities
                ''')
                entity_stats = cursor.fetchone()

                return {
                    "content": {
                        "total": content_stats[0] or 0,
                        "processed": content_stats[1] or 0,
                        "not_processed": content_stats[2] or 0
                    },
                    "queue": {
                        "pending": queue_stats.get("pending", 0),
                        "processing": queue_stats.get("processing", 0),
                        "completed": queue_stats.get("completed", 0),
                        "failed": queue_stats.get("failed", 0),
                        "skipped": queue_stats.get("skipped", 0)
                    },
                    "entities": {
                        "unique_entities": entity_stats[0] or 0
                    }
                }
            except Exception as e:
                logger.error(f"Failed to get stats: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/worker/stats")
        async def get_worker_stats():
            """Get KG worker statistics"""
            try:
                if not self.kg_manager:
                    from robaidata.kg_coordinator import get_kg_manager
                    self.kg_manager = get_kg_manager()

                stats = self.kg_manager.get_aggregate_stats()
                return stats
            except Exception as e:
                logger.error(f"Failed to get worker stats: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/content")
        async def get_content(
            page: int = 1,
            per_page: int = 50,
            search: str = "",
            filter_kg: str = "all"
        ):
            """Get content list with pagination"""
            try:
                # Build WHERE clause
                where_clauses = []
                params = []

                if search:
                    where_clauses.append("(url LIKE ? OR title LIKE ?)")
                    params.extend([f"%{search}%", f"%{search}%"])

                if filter_kg == "processed":
                    where_clauses.append("kg_processed = 1")
                elif filter_kg == "not_processed":
                    where_clauses.append("kg_processed = 0")

                where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

                # Get total count
                count_sql = f"SELECT COUNT(*) FROM crawled_content {where_sql}"
                cursor = self.db_manager.execute_with_retry(count_sql, tuple(params))
                total = cursor.fetchone()[0]

                # Get paginated content
                offset = (page - 1) * per_page
                content_sql = f'''
                    SELECT id, url, title, timestamp, kg_processed,
                           kg_entity_count, kg_relationship_count, kg_processed_at
                    FROM crawled_content
                    {where_sql}
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                '''
                params.extend([per_page, offset])

                cursor = self.db_manager.execute_with_retry(content_sql, tuple(params))
                content_list = []

                for row in cursor.fetchall():
                    content_list.append({
                        "id": row[0],
                        "url": row[1],
                        "title": row[2],
                        "timestamp": row[3],
                        "kg_processed": bool(row[4]),
                        "kg_entity_count": row[5],
                        "kg_relationship_count": row[6],
                        "kg_processed_at": row[7]
                    })

                return {
                    "content": content_list,
                    "total": total,
                    "page": page,
                    "per_page": per_page
                }
            except Exception as e:
                logger.error(f"Failed to get content: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/content/{content_id}")
        async def get_content_detail(content_id: int):
            """Get detailed content information"""
            try:
                # Get content
                cursor = self.db_manager.execute_with_retry('''
                    SELECT id, url, title, timestamp, kg_processed,
                           kg_entity_count, kg_relationship_count, kg_processed_at
                    FROM crawled_content
                    WHERE id = ?
                ''', (content_id,))

                row = cursor.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Content not found")

                content_data = {
                    "id": row[0],
                    "url": row[1],
                    "title": row[2],
                    "timestamp": row[3],
                    "kg_processed": bool(row[4]),
                    "kg_entity_count": row[5],
                    "kg_relationship_count": row[6],
                    "kg_processed_at": row[7]
                }

                # Get top entities
                if row[4]:  # If KG processed
                    cursor = self.db_manager.execute_with_retry('''
                        SELECT entity_text, entity_type_primary, COUNT(*) as occurrences
                        FROM chunk_entities
                        WHERE content_id = ?
                        GROUP BY entity_normalized, entity_type_primary
                        ORDER BY occurrences DESC
                        LIMIT 20
                    ''', (content_id,))

                    content_data["entities"] = [
                        {"entity_text": row[0], "entity_type_primary": row[1], "occurrences": row[2]}
                        for row in cursor.fetchall()
                    ]

                    # Get relationships
                    cursor = self.db_manager.execute_with_retry('''
                        SELECT subject_entity, predicate, object_entity, confidence
                        FROM chunk_relationships
                        WHERE content_id = ?
                        ORDER BY confidence DESC
                        LIMIT 50
                    ''', (content_id,))

                    content_data["relationships"] = [
                        {"subject_entity": row[0], "predicate": row[1], "object_entity": row[2], "confidence": row[3]}
                        for row in cursor.fetchall()
                    ]

                return content_data
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to get content detail: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/queue")
        async def get_queue(status: str = "all"):
            """Get queue items"""
            try:
                where_sql = "WHERE status = ?" if status != "all" else ""
                params = (status,) if status != "all" else ()

                sql = f'''
                    SELECT q.id, q.content_id, q.status, q.priority, q.queued_at,
                           q.retry_count, q.error_message, c.title, c.url
                    FROM kg_processing_queue q
                    LEFT JOIN crawled_content c ON q.content_id = c.id
                    {where_sql}
                    ORDER BY q.priority DESC, q.id ASC
                    LIMIT 500
                '''

                cursor = self.db_manager.execute_with_retry(sql, params)
                queue_list = []

                for row in cursor.fetchall():
                    queue_list.append({
                        "id": row[0],
                        "content_id": row[1],
                        "status": row[2],
                        "priority": row[3],
                        "queued_at": row[4],
                        "retry_count": row[5],
                        "error_message": row[6],
                        "title": row[7],
                        "url": row[8]
                    })

                return {"queue": queue_list}
            except Exception as e:
                logger.error(f"Failed to get queue: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/content/{content_id}/requeue")
        async def requeue_content(content_id: int):
            """Re-queue content for KG processing"""
            try:
                from robaidata.kg_coordinator import KGQueueManager

                kg_queue = KGQueueManager(self.db_manager.db)

                # Delete existing queue entry if exists
                self.db_manager.execute_with_retry(
                    "DELETE FROM kg_processing_queue WHERE content_id = ?",
                    (content_id,)
                )
                self.db_manager.db.commit()

                # Add to queue
                queued = kg_queue.queue_for_kg_processing_sync(content_id, priority=1)

                return {
                    "success": queued,
                    "message": "Content queued for processing" if queued else "Failed to queue content"
                }
            except Exception as e:
                logger.error(f"Failed to requeue content: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @app.delete("/api/content/{content_id}")
        async def delete_content(content_id: int):
            """Delete content"""
            try:
                # Delete from all related tables
                self.db_manager.execute_with_retry("DELETE FROM chunk_entities WHERE content_id = ?", (content_id,))
                self.db_manager.execute_with_retry("DELETE FROM chunk_relationships WHERE content_id = ?", (content_id,))
                self.db_manager.execute_with_retry("DELETE FROM content_chunks WHERE content_id = ?", (content_id,))
                self.db_manager.execute_with_retry("DELETE FROM content_vectors WHERE content_id = ?", (content_id,))
                self.db_manager.execute_with_retry("DELETE FROM kg_processing_queue WHERE content_id = ?", (content_id,))
                self.db_manager.execute_with_retry("DELETE FROM crawled_content WHERE id = ?", (content_id,))
                self.db_manager.db.commit()

                return {"success": True, "message": "Content deleted successfully"}
            except Exception as e:
                logger.error(f"Failed to delete content: {e}")
                self.db_manager.db.rollback()
                raise HTTPException(status_code=500, detail=str(e))

        @app.delete("/api/queue/{queue_id}")
        async def delete_queue_item(queue_id: int):
            """Delete queue item"""
            try:
                self.db_manager.execute_with_retry("DELETE FROM kg_processing_queue WHERE id = ?", (queue_id,))
                self.db_manager.db.commit()
                return {"success": True, "message": "Queue item removed"}
            except Exception as e:
                logger.error(f"Failed to delete queue item: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @app.put("/api/queue/{queue_id}/priority")
        async def update_queue_priority(queue_id: int, request: Request):
            """Update queue item priority"""
            try:
                data = await request.json()
                priority = data.get("priority", 1)

                self.db_manager.execute_with_retry(
                    "UPDATE kg_processing_queue SET priority = ? WHERE id = ?",
                    (priority, queue_id)
                )
                self.db_manager.db.commit()

                return {"success": True, "message": "Priority updated"}
            except Exception as e:
                logger.error(f"Failed to update priority: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/queue/clear")
        async def clear_queue():
            """Clear entire queue"""
            try:
                self.db_manager.execute_with_retry("DELETE FROM kg_processing_queue")
                self.db_manager.db.commit()
                return {"success": True, "message": "Queue cleared"}
            except Exception as e:
                logger.error(f"Failed to clear queue: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/queue/requeue-all")
        async def requeue_all():
            """Re-queue all content"""
            try:
                from robaidata.kg_coordinator import KGQueueManager

                # Clear existing queue
                self.db_manager.execute_with_retry("DELETE FROM kg_processing_queue")
                self.db_manager.db.commit()

                # Get all content IDs
                cursor = self.db_manager.execute_with_retry("SELECT id FROM crawled_content")
                content_ids = [row[0] for row in cursor.fetchall()]

                # Queue all
                kg_queue = KGQueueManager(self.db_manager.db)
                queued_count = 0

                for content_id in content_ids:
                    if kg_queue.queue_for_kg_processing_sync(content_id, priority=1):
                        queued_count += 1

                return {"success": True, "message": f"Queued {queued_count} items"}
            except Exception as e:
                logger.error(f"Failed to requeue all: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/queue/queue-unprocessed")
        async def queue_unprocessed():
            """Queue all unprocessed content"""
            try:
                from robaidata.kg_coordinator import KGQueueManager

                # Get unprocessed content IDs not in queue
                cursor = self.db_manager.execute_with_retry('''
                    SELECT c.id
                    FROM crawled_content c
                    LEFT JOIN kg_processing_queue q ON c.id = q.content_id
                    WHERE c.kg_processed = 0 AND q.id IS NULL
                ''')
                content_ids = [row[0] for row in cursor.fetchall()]

                # Queue all
                kg_queue = KGQueueManager(self.db_manager.db)
                queued_count = 0

                for content_id in content_ids:
                    if kg_queue.queue_for_kg_processing_sync(content_id, priority=1):
                        queued_count += 1

                return {"success": True, "message": f"Queued {queued_count} unprocessed items"}
            except Exception as e:
                logger.error(f"Failed to queue unprocessed: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/queue/requeue-failed")
        async def requeue_failed():
            """Re-queue failed items"""
            try:
                self.db_manager.execute_with_retry('''
                    UPDATE kg_processing_queue
                    SET status = 'pending', error_message = NULL, retry_count = retry_count + 1
                    WHERE status = 'failed'
                ''')
                self.db_manager.db.commit()

                cursor = self.db_manager.execute_with_retry("SELECT changes()")
                count = cursor.fetchone()[0]

                return {"success": True, "message": f"Re-queued {count} failed items"}
            except Exception as e:
                logger.error(f"Failed to requeue failed: {e}")
                raise HTTPException(status_code=500, detail=str(e))

    async def start(self):
        """Start dashboard server"""
        logger.info(f"Starting KG Dashboard on http://{self.host}:{self.port}")

        config = uvicorn.Config(
            app=self.app,
            host=self.host,
            port=self.port,
            log_level="info",
            access_log=False
        )

        self.server = uvicorn.Server(config)

        try:
            await self.server.serve()
        except asyncio.CancelledError:
            logger.info("Dashboard server cancelled")
        except Exception as e:
            logger.error(f"Dashboard server error: {e}")

    async def stop(self):
        """Stop dashboard server"""
        if self.server:
            logger.info("Stopping KG Dashboard...")
            self.server.should_exit = True
            await asyncio.sleep(0.5)  # Give time to stop


# Global dashboard instance
_dashboard: Optional[KGDashboard] = None
_dashboard_task: Optional[asyncio.Task] = None


def get_dashboard(db_manager, kg_manager=None) -> KGDashboard:
    """Get or create global dashboard instance"""
    global _dashboard
    if _dashboard is None:
        host = os.getenv("KG_DASHBOARD_HOST", "0.0.0.0")
        port = int(os.getenv("KG_DASHBOARD_PORT", "8090"))
        _dashboard = KGDashboard(db_manager, kg_manager, host, port)
    return _dashboard


async def start_dashboard(db_manager, kg_manager=None):
    """Start dashboard server (convenience function)"""
    global _dashboard_task
    dashboard = get_dashboard(db_manager, kg_manager)

    # Start in background
    _dashboard_task = asyncio.create_task(dashboard.start())
    logger.info(f"✓ KG Dashboard started at http://{dashboard.host}:{dashboard.port}")

    return _dashboard_task


async def stop_dashboard():
    """Stop dashboard server (convenience function)"""
    global _dashboard, _dashboard_task

    if _dashboard:
        await _dashboard.stop()

    if _dashboard_task and not _dashboard_task.done():
        _dashboard_task.cancel()
        try:
            await _dashboard_task
        except asyncio.CancelledError:
            pass

    _dashboard = None
    _dashboard_task = None
