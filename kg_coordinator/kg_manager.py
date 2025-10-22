"""
KG Worker Manager

High-level lifecycle management for KG workers.
Provides start, stop, restart, and monitoring capabilities.
"""

import asyncio
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

from robaidata.kg_coordinator.kg_worker import KGWorker, get_kg_worker, stop_kg_worker
from robaidata.kg_coordinator.kg_config import get_kg_config

logger = logging.getLogger(__name__)


class KGWorkerManager:
    """
    Manages lifecycle of KG workers

    Provides high-level control over worker pool:
    - Start/stop workers
    - Monitor worker health
    - Restart failed workers
    - Aggregate statistics
    """

    def __init__(self):
        self.workers: List[KGWorker] = []
        self.worker_tasks: List[asyncio.Task] = []
        self.running = False
        self.manager_stats = {
            "started_at": None,
            "workers_restarted": 0,
            "last_health_check": None
        }

    async def start_workers(self, db_manager, num_workers: int = 1, poll_interval: float = 5.0):
        """
        Start worker pool

        Args:
            db_manager: RAGDatabase instance (GLOBAL_DB)
            num_workers: Number of workers to start (default 1)
            poll_interval: Seconds between queue checks
        """
        if self.running:
            logger.warning("Workers already running")
            return

        logger.info(f"Starting {num_workers} KG worker(s)...")
        self.running = True
        self.manager_stats["started_at"] = datetime.now()

        # Create workers
        for i in range(num_workers):
            worker = KGWorker(db_manager, poll_interval=poll_interval)
            self.workers.append(worker)

            # Start worker in background
            task = asyncio.create_task(worker.start())
            self.worker_tasks.append(task)

            logger.info(f"✓ Started KG worker #{i+1}")

        logger.info(f"✓ {num_workers} KG worker(s) started successfully")

    async def stop_workers(self):
        """Stop all workers gracefully"""
        if not self.running:
            logger.warning("No workers running")
            return

        logger.info(f"Stopping {len(self.workers)} worker(s)...")
        self.running = False

        # Stop all workers
        for worker in self.workers:
            await worker.stop()

        # Cancel all tasks
        for task in self.worker_tasks:
            if not task.done():
                task.cancel()

        # Wait for tasks to complete
        if self.worker_tasks:
            await asyncio.gather(*self.worker_tasks, return_exceptions=True)

        # Clear state
        self.workers.clear()
        self.worker_tasks.clear()

        logger.info("✓ All workers stopped")

    async def restart_workers(self, db_manager, poll_interval: float = 5.0):
        """Restart all workers"""
        logger.info("Restarting workers...")
        num_workers = len(self.workers)

        await self.stop_workers()
        await self.start_workers(db_manager, num_workers, poll_interval)

        self.manager_stats["workers_restarted"] += 1
        logger.info("✓ Workers restarted")

    async def check_worker_health(self) -> Dict[str, Any]:
        """
        Check health of all workers

        Returns:
            Health status dict with per-worker details
        """
        self.manager_stats["last_health_check"] = datetime.now()

        health = {
            "manager_running": self.running,
            "total_workers": len(self.workers),
            "healthy_workers": 0,
            "unhealthy_workers": 0,
            "worker_details": []
        }

        for i, worker in enumerate(self.workers):
            worker_healthy = worker.running

            if worker_healthy:
                health["healthy_workers"] += 1
            else:
                health["unhealthy_workers"] += 1

            health["worker_details"].append({
                "worker_id": i,
                "running": worker.running,
                "stats": worker.get_stats()
            })

        return health

    def get_aggregate_stats(self) -> Dict[str, Any]:
        """
        Get aggregate statistics from all workers

        Returns:
            Combined stats from all workers
        """
        aggregate = {
            "total_workers": len(self.workers),
            "manager_running": self.running,
            "manager_started_at": (
                self.manager_stats["started_at"].isoformat()
                if self.manager_stats["started_at"] else None
            ),
            "workers_restarted": self.manager_stats["workers_restarted"],
            "last_health_check": (
                self.manager_stats["last_health_check"].isoformat()
                if self.manager_stats["last_health_check"] else None
            ),
            # Aggregate worker stats
            "total_processed": 0,
            "total_success": 0,
            "total_failed": 0,
            "kg_service_enabled": False,
            "kg_service_status": None,
            "queue_size": {
                "total": 0,
                "pending": 0,
                "processing": 0,
                "completed": 0,
                "failed": 0
            }
        }

        # Aggregate stats from all workers
        for worker in self.workers:
            stats = worker.get_stats()
            aggregate["total_processed"] += stats.get("total_processed", 0)
            aggregate["total_success"] += stats.get("total_success", 0)
            aggregate["total_failed"] += stats.get("total_failed", 0)

            # Use first worker's KG service status
            if not aggregate["kg_service_enabled"]:
                aggregate["kg_service_enabled"] = stats.get("kg_service_enabled", False)
                aggregate["kg_service_status"] = stats.get("kg_service_status")

            # Use first worker's queue size (they all see same queue)
            if aggregate["queue_size"]["total"] == 0:
                aggregate["queue_size"] = stats.get("queue_size", aggregate["queue_size"])

        return aggregate


# Global manager instance
_kg_manager: Optional[KGWorkerManager] = None


def get_kg_manager() -> KGWorkerManager:
    """Get global KG manager instance"""
    global _kg_manager
    if _kg_manager is None:
        _kg_manager = KGWorkerManager()
    return _kg_manager


async def start_kg_workers(db_manager, num_workers: int = 1, poll_interval: float = 5.0):
    """
    Start KG worker pool (convenience function)

    Args:
        db_manager: RAGDatabase instance (GLOBAL_DB)
        num_workers: Number of workers to start (default 1)
        poll_interval: Seconds between queue checks
    """
    manager = get_kg_manager()
    await manager.start_workers(db_manager, num_workers, poll_interval)


async def stop_kg_workers():
    """Stop all KG workers (convenience function)"""
    manager = get_kg_manager()
    await manager.stop_workers()


async def restart_kg_workers(db_manager, poll_interval: float = 5.0):
    """Restart all KG workers (convenience function)"""
    manager = get_kg_manager()
    await manager.restart_workers(db_manager, poll_interval)


def get_worker_stats() -> Dict[str, Any]:
    """Get aggregate worker statistics (convenience function)"""
    manager = get_kg_manager()
    return manager.get_aggregate_stats()
