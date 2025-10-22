"""
Knowledge Graph Queue Management

Handles queuing documents for KG processing and tracking chunk metadata.
"""

import sqlite3
import json
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime

from robaidata.kg_coordinator.kg_config import get_kg_config

logger = logging.getLogger(__name__)


class KGQueueManager:
    """Manages KG processing queue and chunk metadata"""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.kg_config = get_kg_config()

    def calculate_chunk_boundaries(
        self,
        markdown: str,
        chunks: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Calculate character positions for each chunk in the original markdown

        Args:
            markdown: Full markdown document
            chunks: List of chunk texts

        Returns:
            List of chunk metadata with boundaries:
            [
                {
                    "chunk_index": 0,
                    "text": "chunk text...",
                    "char_start": 0,
                    "char_end": 2500,
                    "word_count": 500
                },
                ...
            ]
        """
        chunk_metadata = []
        search_start = 0

        for idx, chunk_text in enumerate(chunks):
            # Find chunk in markdown starting from last position
            # This handles overlapping chunks correctly
            char_start = markdown.find(chunk_text, search_start)

            if char_start == -1:
                # Chunk not found exactly (may be due to cleaning)
                # Use approximate position based on previous chunks
                if idx == 0:
                    char_start = 0
                else:
                    char_start = chunk_metadata[idx - 1]["char_end"]

                char_end = char_start + len(chunk_text)
            else:
                char_end = char_start + len(chunk_text)
                # Update search start for next chunk (accounting for overlap)
                search_start = char_start + 1

            word_count = len(chunk_text.split())

            chunk_metadata.append({
                "chunk_index": idx,
                "text": chunk_text,
                "char_start": char_start,
                "char_end": char_end,
                "word_count": word_count
            })

        return chunk_metadata

    def store_chunk_metadata(
        self,
        content_id: int,
        chunk_metadata: List[Dict[str, Any]],
        vector_rowids: List[int]
    ) -> int:
        """
        Store chunk metadata in content_chunks table

        Args:
            content_id: Content ID from crawled_content
            chunk_metadata: List of chunk boundaries
            vector_rowids: List of content_vectors rowids (in same order as chunks)

        Returns:
            Number of chunks stored
        """
        if len(chunk_metadata) != len(vector_rowids):
            logger.error(
                f"Mismatch: {len(chunk_metadata)} chunks vs {len(vector_rowids)} vector rowids"
            )
            return 0

        stored_count = 0

        for chunk_meta, vector_rowid in zip(chunk_metadata, vector_rowids):
            try:
                self.db.execute('''
                    INSERT OR REPLACE INTO content_chunks
                    (rowid, content_id, chunk_index, chunk_text, char_start, char_end, word_count, kg_processed)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                ''', (
                    vector_rowid,
                    content_id,
                    chunk_meta["chunk_index"],
                    chunk_meta["text"],
                    chunk_meta["char_start"],
                    chunk_meta["char_end"],
                    chunk_meta["word_count"]
                ))
                stored_count += 1

            except sqlite3.Error as e:
                logger.error(f"Failed to store chunk metadata: {e}")

        return stored_count

    async def queue_for_kg_processing(
        self,
        content_id: int,
        priority: int = 1
    ) -> bool:
        """
        Add content to KG processing queue if service is available

        Args:
            content_id: Content ID to queue
            priority: Priority level (higher = process first)

        Returns:
            True if queued, False if skipped
        """
        # Check if KG service is enabled and healthy
        if not self.kg_config.enabled:
            logger.debug(f"KG service disabled - not queuing content_id={content_id}")
            return False

        is_healthy = await self.kg_config.check_health()

        if not is_healthy:
            # Mark as skipped due to service unavailability
            logger.info(
                f"KG service unavailable - skipping content_id={content_id} "
                f"(will process if service comes back online)"
            )

            try:
                self.db.execute('''
                    INSERT INTO kg_processing_queue
                    (content_id, status, priority, skipped_reason)
                    VALUES (?, 'skipped', ?, 'kg_service_unavailable')
                ''', (content_id, priority))

                # Mark chunks as not processed
                self.db.execute('''
                    UPDATE content_chunks
                    SET kg_processed = 0
                    WHERE content_id = ?
                ''', (content_id,))

                self.db.commit()

            except sqlite3.Error as e:
                logger.error(f"Failed to mark as skipped: {e}")

            return False

        # Service is healthy - add to queue
        try:
            self.db.execute('''
                INSERT INTO kg_processing_queue
                (content_id, status, priority)
                VALUES (?, 'pending', ?)
            ''', (content_id, priority))

            self.db.commit()

            logger.info(f"✓ Queued content_id={content_id} for KG processing")
            return True

        except sqlite3.IntegrityError:
            # Already in queue
            logger.debug(f"Content {content_id} already in KG queue")
            return False

        except sqlite3.Error as e:
            logger.error(f"Failed to queue for KG processing: {e}")
            return False

    def queue_for_kg_processing_sync(
        self,
        content_id: int,
        priority: int = 1
    ) -> bool:
        """
        Synchronously add content to KG processing queue (thread-safe)

        This version doesn't check service health - just queues the item.
        The KG worker will handle service availability when processing.
        Use this from synchronous contexts (like thread pool workers).

        Args:
            content_id: Content ID to queue
            priority: Priority level (higher = process first)

        Returns:
            True if queued, False if already in queue or error
        """
        # Check if KG service is enabled
        if not self.kg_config.enabled:
            logger.debug(f"KG service disabled - not queuing content_id={content_id}")
            return False

        try:
            self.db.execute('''
                INSERT INTO kg_processing_queue
                (content_id, status, priority)
                VALUES (?, 'pending', ?)
            ''', (content_id, priority))

            self.db.commit()

            logger.info(f"✓ Queued content_id={content_id} for KG processing (sync)")
            return True

        except sqlite3.IntegrityError:
            # Already in queue
            logger.debug(f"Content {content_id} already in KG queue")
            return False

        except sqlite3.Error as e:
            logger.error(f"Failed to queue for KG processing: {e}")
            return False

    def get_chunk_metadata_for_content(
        self,
        content_id: int
    ) -> List[Dict[str, Any]]:
        """
        Retrieve chunk metadata for a content item

        Args:
            content_id: Content ID

        Returns:
            List of chunk metadata dictionaries
        """
        cursor = self.db.execute('''
            SELECT
                rowid,
                chunk_index,
                char_start,
                char_end,
                chunk_text,
                word_count
            FROM content_chunks
            WHERE content_id = ?
            ORDER BY chunk_index ASC
        ''', (content_id,))

        chunks = []
        for row in cursor.fetchall():
            chunks.append({
                "vector_rowid": row[0],
                "chunk_index": row[1],
                "char_start": row[2],
                "char_end": row[3],
                "text": row[4],
                "word_count": row[5]
            })

        return chunks

    def write_kg_results(
        self,
        content_id: int,
        result: Dict[str, Any]
    ) -> bool:
        """
        Write KG processing results back to SQLite

        Args:
            content_id: Content ID
            result: Result dict from kg-service

        Returns:
            True if successful
        """
        try:
            # Update crawled_content
            self.db.execute('''
                UPDATE crawled_content
                SET kg_processed = 1,
                    kg_entity_count = ?,
                    kg_relationship_count = ?,
                    kg_document_id = ?,
                    kg_processed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (
                result.get("entities_extracted", 0),
                result.get("relationships_extracted", 0),
                result.get("neo4j_document_id"),
                content_id
            ))

            # Insert chunk_entities
            entities = result.get("entities", [])
            for entity in entities:
                for appearance in entity.get("chunk_appearances", []):
                    self.db.execute('''
                        INSERT INTO chunk_entities (
                            chunk_rowid,
                            content_id,
                            entity_text,
                            entity_normalized,
                            entity_type_primary,
                            entity_type_sub1,
                            entity_type_sub2,
                            entity_type_sub3,
                            confidence,
                            offset_start,
                            offset_end,
                            neo4j_node_id,
                            spans_multiple_chunks
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        appearance["vector_rowid"],
                        content_id,
                        entity["text"],
                        entity.get("normalized"),
                        entity.get("type_primary"),
                        entity.get("type_sub1"),
                        entity.get("type_sub2"),
                        entity.get("type_sub3"),
                        entity.get("confidence"),
                        appearance["offset_start"],
                        appearance["offset_end"],
                        result.get("neo4j_document_id"),  # Placeholder
                        entity.get("spans_multiple_chunks", False)
                    ))

            # Insert chunk_relationships
            relationships = result.get("relationships", [])
            for rel in relationships:
                chunk_rowids_json = json.dumps(rel.get("chunk_rowids", []))

                self.db.execute('''
                    INSERT INTO chunk_relationships (
                        content_id,
                        subject_entity,
                        predicate,
                        object_entity,
                        confidence,
                        context,
                        neo4j_relationship_id,
                        spans_chunks,
                        chunk_rowids
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    content_id,
                    rel.get("subject_text"),
                    rel.get("predicate"),
                    rel.get("object_text"),
                    rel.get("confidence"),
                    rel.get("context"),
                    None,  # Placeholder
                    rel.get("spans_chunks", False),
                    chunk_rowids_json
                ))

            # Mark chunks as processed
            self.db.execute('''
                UPDATE content_chunks
                SET kg_processed = 1
                WHERE content_id = ?
            ''', (content_id,))

            self.db.commit()

            logger.info(
                f"✓ Wrote KG results for content_id={content_id}: "
                f"{len(entities)} entities, {len(relationships)} relationships"
            )

            return True

        except sqlite3.Error as e:
            logger.error(f"Failed to write KG results: {e}")
            self.db.rollback()
            return False


def get_vector_rowids_for_content(db: sqlite3.Connection, content_id: int) -> List[int]:
    """
    Get vector rowids for a content item (in order)

    Args:
        db: Database connection
        content_id: Content ID

    Returns:
        List of rowids from content_vectors
    """
    cursor = db.execute('''
        SELECT rowid
        FROM content_vectors
        WHERE content_id = ?
        ORDER BY rowid ASC
    ''', (content_id,))

    return [row[0] for row in cursor.fetchall()]
