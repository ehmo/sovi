"""Temporal worker setup — registers workflows and activities, starts polling."""

from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from sovi.config import settings
from sovi.workflows.activities import (
    assemble_video,
    collect_metrics,
    distribute,
    export_for_platform,
    generate_daily_report,
    generate_images,
    generate_script,
    generate_video_clip,
    generate_voiceover,
    quality_check,
    scan_trends,
    select_background_music,
    select_hook,
    transcribe_audio,
)
from sovi.workflows.video_production import DailyBatchWorkflow, VideoProductionWorkflow

TASK_QUEUE = "sovi-production"


async def run_worker() -> None:
    """Connect to Temporal and start the worker."""
    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[VideoProductionWorkflow, DailyBatchWorkflow],
        activities=[
            scan_trends,
            generate_script,
            select_hook,
            generate_voiceover,
            generate_images,
            generate_video_clip,
            transcribe_audio,
            select_background_music,
            assemble_video,
            export_for_platform,
            quality_check,
            distribute,
            collect_metrics,
            generate_daily_report,
        ],
    )

    print(f"Worker started on queue={TASK_QUEUE}")
    await worker.run()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
