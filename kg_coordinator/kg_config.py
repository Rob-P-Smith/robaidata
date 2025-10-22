"""
Knowledge Graph Service Configuration and Health Check

Manages KG service availability and graceful fallback when unavailable.
"""

import os
import logging
import asyncio
import time
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

import httpx

logger = logging.getLogger(__name__)


class KGServiceConfig:
    """Configuration for Knowledge Graph service integration"""

    def __init__(self):
        # KG Service settings
        self.enabled = os.getenv("KG_SERVICE_ENABLED", "false").lower() == "true"
        self.url = os.getenv("KG_SERVICE_URL", "http://kg-service:8088")
        self.timeout = float(os.getenv("KG_SERVICE_TIMEOUT", "1800.0"))  # 30 minutes
        self.health_check_interval = float(os.getenv("KG_HEALTH_CHECK_INTERVAL", "30.0"))  # 30 seconds
        self.max_retries = int(os.getenv("KG_MAX_RETRIES", "3"))

        # Health check state
        self._is_healthy = False
        self._last_health_check = None
        self._consecutive_failures = 0
        self._health_check_lock = asyncio.Lock()

        # HTTP client (reused for efficiency)
        self._client: Optional[httpx.AsyncClient] = None

        logger.info(f"KG Service Config: enabled={self.enabled}, url={self.url}")

    async def get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10)
            )
        return self._client

    async def close_client(self):
        """Close HTTP client"""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def check_health(self, force: bool = False) -> bool:
        """
        Check if KG service is healthy

        Args:
            force: Force health check even if recently checked

        Returns:
            True if KG service is healthy and available
        """
        if not self.enabled:
            return False

        # Check if recent health check is valid
        if not force and self._last_health_check:
            time_since_check = time.time() - self._last_health_check
            if time_since_check < self.health_check_interval:
                return self._is_healthy

        async with self._health_check_lock:
            # Double-check pattern (another task may have updated while waiting)
            if not force and self._last_health_check:
                time_since_check = time.time() - self._last_health_check
                if time_since_check < self.health_check_interval:
                    return self._is_healthy

            # Perform health check
            try:
                client = await self.get_client()
                response = await client.get(
                    f"{self.url}/health",
                    timeout=5.0  # Quick timeout for health check
                )

                if response.status_code == 200:
                    data = response.json()
                    status = data.get("status", "unknown")

                    # Consider service healthy if status is "healthy" or "degraded"
                    # (degraded means some dependencies down but service operational)
                    is_healthy = status in ["healthy", "degraded"]

                    if is_healthy:
                        if not self._is_healthy:
                            logger.info(f"✓ KG service is now healthy (status: {status})")
                        self._is_healthy = True
                        self._consecutive_failures = 0
                    else:
                        logger.warning(f"KG service unhealthy (status: {status})")
                        self._is_healthy = False
                        self._consecutive_failures += 1

                else:
                    logger.warning(f"KG service health check failed: HTTP {response.status_code}")
                    self._is_healthy = False
                    self._consecutive_failures += 1

            except httpx.TimeoutException:
                logger.warning("KG service health check timed out")
                self._is_healthy = False
                self._consecutive_failures += 1

            except httpx.ConnectError:
                if self._consecutive_failures == 0:
                    logger.warning(f"Cannot connect to KG service at {self.url}")
                self._is_healthy = False
                self._consecutive_failures += 1

            except Exception as e:
                logger.error(f"KG service health check error: {e}")
                self._is_healthy = False
                self._consecutive_failures += 1

            finally:
                self._last_health_check = time.time()

                # Log if service has been down for a while
                if self._consecutive_failures >= 5:
                    logger.error(
                        f"KG service has been unhealthy for {self._consecutive_failures} "
                        f"consecutive checks - falling back to vector-only search"
                    )

            return self._is_healthy

    async def send_to_kg_queue(
        self,
        content_id: int,
        url: str,
        title: str,
        markdown: str,
        chunks: list,
        metadata: dict
    ) -> Optional[Dict[str, Any]]:
        """
        Send document to KG service for processing

        Returns:
            Response dict if successful, None if failed or service unavailable
        """
        if not self.enabled:
            return None

        # Check health before sending
        if not await self.check_health():
            logger.debug(f"Skipping KG processing for content_id={content_id} (service unhealthy)")
            return None

        try:
            client = await self.get_client()

            request_data = {
                "content_id": content_id,
                "url": url,
                "title": title,
                "markdown": markdown,
                "chunks": chunks,
                "metadata": metadata
            }

            logger.info(f"Sending document to KG service: content_id={content_id}")

            response = await client.post(
                f"{self.url}/api/v1/ingest",
                json=request_data,
                timeout=self.timeout
            )

            if response.status_code == 200:
                result = response.json()
                logger.info(
                    f"✓ KG processing complete: content_id={content_id}, "
                    f"entities={result.get('entities_extracted', 0)}, "
                    f"relationships={result.get('relationships_extracted', 0)}"
                )
                return result

            else:
                logger.error(
                    f"KG service returned error: HTTP {response.status_code} "
                    f"for content_id={content_id}"
                )
                return None

        except httpx.TimeoutException:
            logger.error(f"KG service request timed out for content_id={content_id}")
            return None

        except httpx.ConnectError:
            logger.error(f"Cannot connect to KG service for content_id={content_id}")
            # Mark service as unhealthy
            self._is_healthy = False
            return None

        except Exception as e:
            logger.error(f"Error sending to KG service: {e}", exc_info=True)
            return None

    def get_status(self) -> Dict[str, Any]:
        """Get current KG service status"""
        return {
            "enabled": self.enabled,
            "url": self.url,
            "is_healthy": self._is_healthy,
            "last_health_check": (
                datetime.fromtimestamp(self._last_health_check).isoformat()
                if self._last_health_check else None
            ),
            "consecutive_failures": self._consecutive_failures
        }


# Global instance
_kg_config: Optional[KGServiceConfig] = None


def get_kg_config() -> KGServiceConfig:
    """Get global KG service configuration instance"""
    global _kg_config
    if _kg_config is None:
        _kg_config = KGServiceConfig()
    return _kg_config


async def close_kg_config():
    """Close KG service configuration and cleanup resources"""
    global _kg_config
    if _kg_config:
        await _kg_config.close_client()
        _kg_config = None
