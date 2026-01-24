"""Background task scheduler using APScheduler."""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.database import get_db_context, get_redis
from app.services.autocomplete import AutocompleteService
from app.services.trending import TrendingService
from app.services.tweet import TweetService

settings = get_settings()
logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler: AsyncIOScheduler | None = None


async def refresh_trending_task() -> None:
    """Background task to refresh trending topics cache."""
    try:
        async with get_db_context() as db:
            redis_client = get_redis()
            service = TrendingService(db, redis_client)

            count = await service.refresh_trending_cache("global")
            logger.info(f"Refreshed trending cache: {count} trends")

    except Exception as e:
        logger.error(f"Error refreshing trending cache: {e}")


async def rebuild_autocomplete_task() -> None:
    """Background task to rebuild autocomplete index."""
    try:
        async with get_db_context() as db:
            redis_client = get_redis()
            service = AutocompleteService(db, redis_client)

            count = await service.rebuild_autocomplete_index()
            logger.info(f"Rebuilt autocomplete index: {count} terms")

    except Exception as e:
        logger.error(f"Error rebuilding autocomplete index: {e}")


async def flush_views_task() -> None:
    """Background task to flush view buffers to database."""
    try:
        async with get_db_context() as db:
            redis_client = get_redis()
            service = TweetService(db, redis_client)

            count = await service.flush_view_buffers()
            if count > 0:
                logger.debug(f"Flushed {count} view buffers")

    except Exception as e:
        logger.error(f"Error flushing view buffers: {e}")


async def cleanup_deleted_tweets_task() -> None:
    """Background task to hard delete old soft-deleted tweets."""
    try:
        async with get_db_context() as db:
            redis_client = get_redis()
            service = TweetService(db, redis_client)

            count = await service.cleanup_deleted_tweets(hours=24)
            if count > 0:
                logger.info(f"Cleaned up {count} deleted tweets")

    except Exception as e:
        logger.error(f"Error cleaning up deleted tweets: {e}")


def setup_scheduler() -> AsyncIOScheduler:
    """Set up and start the background task scheduler."""
    global scheduler

    scheduler = AsyncIOScheduler(timezone="UTC")

    # Trending refresh - every 30 seconds
    scheduler.add_job(
        refresh_trending_task,
        trigger=IntervalTrigger(seconds=settings.trending_refresh_seconds),
        id="refresh_trending",
        name="Refresh trending topics cache",
        replace_existing=True,
        max_instances=1,
    )

    # Autocomplete rebuild - every 5 minutes
    scheduler.add_job(
        rebuild_autocomplete_task,
        trigger=IntervalTrigger(seconds=settings.autocomplete_refresh_seconds),
        id="rebuild_autocomplete",
        name="Rebuild autocomplete index",
        replace_existing=True,
        max_instances=1,
    )

    # Flush views - every 10 seconds
    scheduler.add_job(
        flush_views_task,
        trigger=IntervalTrigger(seconds=10),
        id="flush_views",
        name="Flush view buffers",
        replace_existing=True,
        max_instances=1,
    )

    # Cleanup deleted tweets - every hour
    scheduler.add_job(
        cleanup_deleted_tweets_task,
        trigger=IntervalTrigger(hours=1),
        id="cleanup_deleted",
        name="Cleanup deleted tweets",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.start()
    logger.info("Background scheduler started")

    return scheduler


def shutdown_scheduler() -> None:
    """Shutdown the background task scheduler."""
    global scheduler

    if scheduler:
        scheduler.shutdown(wait=False)
        logger.info("Background scheduler stopped")


async def run_initial_tasks() -> None:
    """Run initial tasks on startup to populate caches."""
    logger.info("Running initial cache population...")

    # Small delay to ensure connections are ready
    await asyncio.sleep(1)

    # Run trending and autocomplete refresh
    await refresh_trending_task()
    await rebuild_autocomplete_task()

    logger.info("Initial cache population complete")
