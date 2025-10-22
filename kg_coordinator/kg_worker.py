"""
Knowledge Graph Background Worker

Processes the kg_processing_queue table and sends documents to kg-service.
Runs as a background task in the API server.
"""

import asyncio
import logging
import time
from typing import Dict, Any, Optional
from datetime import datetime

from robaidata.kg_coordinator.kg_queue import KGQueueManager
from robaidata.kg_coordinator.kg_config import get_kg_config

logger = logging.getLogger(__name__)


class KGWorker:
    """Background worker for processing KG queue"""

    def __init__(self, db_manager, poll_interval: float = 5.0):
        """
        Initialize KG worker

        Args:
            db_manager: RAGDatabase instance (GLOBAL_DB)
            poll_interval: Seconds between queue checks (default 5.0)
        """
        self.db_manager = db_manager
        self.poll_interval = poll_interval
        self.kg_config = get_kg_config()
        self.running = False
        self.stats = {
            "total_processed": 0,
            "total_success": 0,
            "total_failed": 0,
            "last_processed_at": None,
            "started_at": None
        }

    async def start(self):
        """Start the background worker"""
        if not self.kg_config.enabled:
            logger.info("KG service disabled - worker not started")
            return

        self.running = True
        self.stats["started_at"] = datetime.now()
        logger.info(f"✓ KG worker started (poll_interval={self.poll_interval}s)")

        # Reset any stale 'processing' items on startup
        await self._reset_stale_processing_items()

        try:
            iteration = 0
            while self.running:
                try:
                    await self._process_queue_batch()

                    # Run periodic cleanup every 20 iterations (~100 seconds)
                    iteration += 1
                    if iteration % 20 == 0:
                        await self._reset_stale_processing_items()

                except Exception as e:
                    logger.error(f"Error in KG worker loop: {e}", exc_info=True)

                # Sleep before next iteration
                await asyncio.sleep(self.poll_interval)

        except asyncio.CancelledError:
            logger.info("KG worker cancelled")
        finally:
            self.running = False
            logger.info("KG worker stopped")

    async def stop(self):
        """Stop the background worker"""
        logger.info("Stopping KG worker...")
        self.running = False

    async def _reset_stale_processing_items(self, stale_minutes: int = 60):
        """
        Reset stale 'processing' items back to 'pending'

        Detects items that have been in 'processing' status for longer than
        stale_minutes and resets them to 'pending' for retry. This handles
        cases where the worker crashed or container restarted mid-processing.

        Args:
            stale_minutes: Number of minutes before item considered stale (default 60)
        """
        try:
            with self.db_manager.transaction():
                # Find items in 'processing' status with no recent activity
                cursor = self.db_manager.execute_with_retry('''
                    SELECT id, content_id, processing_started_at
                    FROM kg_processing_queue
                    WHERE status = 'processing'
                    AND (
                        processing_started_at IS NULL
                        OR datetime(processing_started_at) < datetime('now', '-' || ? || ' minutes')
                    )
                ''', (stale_minutes,))

                stale_items = cursor.fetchall()

                if stale_items:
                    logger.warning(
                        f"Found {len(stale_items)} stale 'processing' items (>{stale_minutes}min old), "
                        f"resetting to 'pending'"
                    )

                    for queue_id, content_id, started_at in stale_items:
                        logger.info(
                            f"Resetting queue_id={queue_id} (content_id={content_id}) - "
                            f"started_at={started_at}"
                        )

                        # Reset to pending and clear timestamp
                        self.db_manager.execute_with_retry('''
                            UPDATE kg_processing_queue
                            SET status = 'pending',
                                processing_started_at = NULL,
                                retry_count = retry_count + 1
                            WHERE id = ?
                        ''', (queue_id,))

                    logger.info(f"✓ Reset {len(stale_items)} stale items to 'pending'")

        except Exception as e:
            logger.error(f"Error resetting stale processing items: {e}", exc_info=True)

    async def _process_queue_batch(self, batch_size: int = 5):
        """
        Process a batch of pending items from the queue

        Args:
            batch_size: Maximum items to process in one batch
        """
        # Get pending items from queue
        with self.db_manager.transaction():
            cursor = self.db_manager.execute_with_retry('''
                SELECT q.id, q.content_id, q.priority, c.url, c.title, c.markdown
                FROM kg_processing_queue q
                JOIN crawled_content c ON q.content_id = c.id
                WHERE q.status = 'pending'
                ORDER BY q.priority DESC, q.id ASC
                LIMIT ?
            ''', (batch_size,))

            pending_items = cursor.fetchall()

        if not pending_items:
            # No pending items, nothing to do
            return

        logger.info(f"Processing {len(pending_items)} items from KG queue")

        for queue_id, content_id, priority, url, title, markdown in pending_items:
            try:
                # Mark as processing with timestamp
                with self.db_manager.transaction():
                    self.db_manager.execute_with_retry('''
                        UPDATE kg_processing_queue
                        SET status = 'processing',
                            processing_started_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    ''', (queue_id,))

                # Get chunk metadata
                kg_queue = KGQueueManager(self.db_manager.db)
                chunks_metadata = kg_queue.get_chunk_metadata_for_content(content_id)

                if not chunks_metadata:
                    logger.warning(f"No chunk metadata found for content_id={content_id}")
                    await self._mark_failed(queue_id, "No chunk metadata found")
                    continue

                # Build chunks payload for kg-service
                chunks_payload = []
                for chunk in chunks_metadata:
                    chunks_payload.append({
                        "vector_rowid": chunk["vector_rowid"],
                        "chunk_index": chunk["chunk_index"],
                        "char_start": chunk["char_start"],
                        "char_end": chunk["char_end"],
                        "text": chunk["text"]
                    })

                # Get metadata
                cursor_meta = self.db_manager.execute_with_retry(
                    'SELECT metadata, tags FROM crawled_content WHERE id = ?',
                    (content_id,)
                )
                row_meta = cursor_meta.fetchone()

                metadata = {}
                if row_meta and row_meta[0]:
                    import json
                    try:
                        metadata = json.loads(row_meta[0])
                    except:
                        pass

                if row_meta and row_meta[1]:
                    metadata["tags"] = row_meta[1]

                # Send to KG service
                logger.info(f"Sending to KG service: content_id={content_id}, url={url}")

                result = await self.kg_config.send_to_kg_queue(
                    content_id=content_id,
                    url=url,
                    title=title,
                    markdown=markdown,
                    chunks=chunks_payload,
                    metadata=metadata
                )

                if result and result.get("success"):
                    # Write results back to SQLite
                    success = kg_queue.write_kg_results(content_id, result)

                    if success:
                        # Mark as completed
                        with self.db_manager.transaction():
                            self.db_manager.execute_with_retry('''
                                UPDATE kg_processing_queue
                                SET status = 'completed'
                                WHERE id = ?
                            ''', (queue_id,))

                        self.stats["total_processed"] += 1
                        self.stats["total_success"] += 1
                        self.stats["last_processed_at"] = datetime.now()

                        logger.info(
                            f"✓ Completed KG processing for content_id={content_id}: "
                            f"{result.get('entities_extracted', 0)} entities, "
                            f"{result.get('relationships_extracted', 0)} relationships"
                        )
                    else:
                        await self._mark_failed(queue_id, "Failed to write KG results to SQLite")
                        self.stats["total_failed"] += 1

                else:
                    # KG service failed or unavailable
                    error_msg = "KG service returned error or is unavailable"
                    await self._mark_failed(queue_id, error_msg)
                    self.stats["total_failed"] += 1

            except Exception as e:
                logger.error(f"Error processing queue item {queue_id}: {e}", exc_info=True)
                await self._mark_failed(queue_id, str(e))
                self.stats["total_failed"] += 1

    async def _mark_failed(self, queue_id: int, error: str):
        """Mark queue item as failed"""
        with self.db_manager.transaction():
            self.db_manager.execute_with_retry('''
                UPDATE kg_processing_queue
                SET status = 'failed',
                    error_message = ?
                WHERE id = ?
            ''', (error[:500], queue_id))  # Limit error message length

    def get_queue_size(self) -> Dict[str, int]:
        """Get the size of the KG processing queue by status"""
        cursor = self.db_manager.execute_with_retry('''
            SELECT status, COUNT(*) as count
            FROM kg_processing_queue
            GROUP BY status
        ''')

        status_counts = {}
        for status, count in cursor.fetchall():
            status_counts[status] = count

        # Get total count
        cursor_total = self.db_manager.execute_with_retry('''
            SELECT COUNT(*) FROM kg_processing_queue
        ''')
        total = cursor_total.fetchone()[0]

        return {
            "total": total,
            "pending": status_counts.get("pending", 0),
            "processing": status_counts.get("processing", 0),
            "completed": status_counts.get("completed", 0),
            "failed": status_counts.get("failed", 0)
        }

    def get_stats(self) -> Dict[str, Any]:
        """Get worker statistics"""
        queue_size = self.get_queue_size()
        return {
            **self.stats,
            "running": self.running,
            "poll_interval": self.poll_interval,
            "kg_service_enabled": self.kg_config.enabled,
            "kg_service_status": self.kg_config.get_status(),
            "queue_size": queue_size
        }


# Global worker instance
_kg_worker: Optional[KGWorker] = None


def get_kg_worker(db_manager=None) -> KGWorker:
    """Get or create global KG worker instance"""
    global _kg_worker
    if _kg_worker is None:
        if db_manager is None:
            from core.data.storage import GLOBAL_DB
            db_manager = GLOBAL_DB
        _kg_worker = KGWorker(db_manager)
    return _kg_worker


async def start_kg_worker(db_manager=None):
    """Start the global KG worker"""
    worker = get_kg_worker(db_manager)
    await worker.start()


async def stop_kg_worker():
    """Stop the global KG worker"""
    global _kg_worker
    if _kg_worker:
        await _kg_worker.stop()
        _kg_worker = None
