"""Background job support for the fact_inventory server.

Provides the periodic-task plugin, distributed lock helper, and job factories
used to guarantee that cleanup jobs run on only one instance at a time.
"""

from fact_inventory.server.background_job.history_cleanup import (
    create_history_cleanup_job,
)
from fact_inventory.server.background_job.lock import (
    BackgroundJobLeaseLostError,
    run_exclusive_background_job,
)
from fact_inventory.server.background_job.plugin import AsyncBackgroundJobPlugin
from fact_inventory.server.background_job.retain_cleanup import (
    create_retention_cleanup_job,
)

__all__ = [
    "AsyncBackgroundJobPlugin",
    "BackgroundJobLeaseLostError",
    "create_history_cleanup_job",
    "create_retention_cleanup_job",
    "run_exclusive_background_job",
]
