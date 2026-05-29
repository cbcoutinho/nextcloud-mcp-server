"""Composition root for the ingest producer (design §10).

``build_external_producer`` is called by the lifespan only when
``INGEST_MODE=external``; local mode uses the in-memory stream directly (it
already satisfies :class:`TaskProducer`). The transport under ``external`` is
selected from the ``INGEST_BUS_URL`` scheme (``nats://`` now, ``postgres://``
later) so moving the external processor to Postgres needs no new INGEST_MODE.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from ...config import Settings
from .ports import TaskProducer

logger = logging.getLogger(__name__)


def _transport_for(url: str) -> str:
    scheme = urlsplit(url).scheme.lower()
    if scheme.startswith("postgres"):
        return "postgres"
    if not scheme.startswith("nats"):
        logger.warning(
            "INGEST_BUS_URL scheme %r is neither nats:// nor postgres://; "
            "defaulting to the NATS transport",
            scheme,
        )
    return "nats"


async def build_external_producer(settings: Settings) -> TaskProducer:
    """Build the external-ingest producer for the configured transport.

    Precondition: ``settings.ingest_mode == "external"`` (so __post_init__ has
    guaranteed ``ingest_bus_url`` and ``tenant_id`` are set).
    """
    # Defence-in-depth (robust under ``python -O``, which strips asserts):
    # __post_init__ already guarantees these when ingest_mode == external.
    if settings.ingest_bus_url is None or settings.tenant_id is None:
        raise ValueError(
            "build_external_producer requires INGEST_BUS_URL and TENANT_ID "
            "(guaranteed by Settings validation when INGEST_MODE=external)"
        )

    transport = _transport_for(settings.ingest_bus_url)
    if transport == "postgres":
        from .postgres import PostgresTaskProducer  # noqa: PLC0415

        return await PostgresTaskProducer.connect(settings)

    from .nats import NatsTaskProducer  # noqa: PLC0415

    return await NatsTaskProducer.connect(
        url=settings.ingest_bus_url,
        tenant_id=settings.tenant_id,
        num_replicas=settings.ingest_bus_num_replicas,
    )
