"""
Knowledge Graph Coordinator

Manages the coordination layer between RAG database and KG service.
Handles queue management, worker processing, and service communication.
"""

from robaidata.kg_coordinator.kg_config import get_kg_config, close_kg_config, KGServiceConfig
from robaidata.kg_coordinator.kg_queue import KGQueueManager, get_vector_rowids_for_content
from robaidata.kg_coordinator.kg_worker import KGWorker, get_kg_worker, start_kg_worker, stop_kg_worker
from robaidata.kg_coordinator.kg_manager import (
    KGWorkerManager,
    get_kg_manager,
    start_kg_workers,
    stop_kg_workers,
    restart_kg_workers,
    get_worker_stats
)
from robaidata.kg_coordinator.kg_dashboard import (
    KGDashboard,
    get_dashboard,
    start_dashboard,
    stop_dashboard
)

__all__ = [
    # Config
    "KGServiceConfig",
    "get_kg_config",
    "close_kg_config",

    # Queue
    "KGQueueManager",
    "get_vector_rowids_for_content",

    # Worker
    "KGWorker",
    "get_kg_worker",
    "start_kg_worker",
    "stop_kg_worker",

    # Manager
    "KGWorkerManager",
    "get_kg_manager",
    "start_kg_workers",
    "stop_kg_workers",
    "restart_kg_workers",
    "get_worker_stats",

    # Dashboard
    "KGDashboard",
    "get_dashboard",
    "start_dashboard",
    "stop_dashboard",
]
