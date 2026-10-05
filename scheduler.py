# scheduler.py - Periodic drift detection and self-healing scheduler
import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from config import Config
from orchestrator import HealingOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger("terraform-agent.scheduler")


def run_healing_cycle():
    """Execute a single healing cycle."""
    logger.info("=== Scheduled healing cycle started ===")
    try:
        orch = HealingOrchestrator(workdir=Config.AGENT_WORKDIR)
        result = orch.heal()
        outcome = result.get("outcome", "unknown")
        logger.info("Healing cycle completed — outcome: %s", outcome)
    except Exception as e:
        logger.error("Healing cycle failed with exception: %s", e)


def start_scheduler():
    """Start the blocking scheduler for periodic drift checks."""
    interval = Config.DRIFT_CHECK_INTERVAL_MINUTES
    logger.info(
        "Starting scheduler — drift check every %d minute(s), workdir=%s",
        interval, Config.AGENT_WORKDIR,
    )

    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_healing_cycle,
        "interval",
        minutes=interval,
        id="drift_check",
        name="Drift detection and self-healing",
    )

    # Run once immediately on startup
    run_healing_cycle()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    start_scheduler()
