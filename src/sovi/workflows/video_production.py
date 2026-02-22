"""Temporal workflows for video production pipeline."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from sovi.models import (
        ContentFormat,
        GeneratedAsset,
        GeneratedScript,
        PlatformExport,
        Platform,
        QualityReport,
        ScriptRequest,
        TopicCandidate,
    )
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

# Retry policies per activity category
SCRIPT_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    non_retryable_error_types=["ContentPolicyViolation"],
)

ASSET_RETRY = RetryPolicy(
    maximum_attempts=5,
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=120),
    non_retryable_error_types=["InvalidInput", "QuotaExceeded"],
)

ASSEMBLY_RETRY = RetryPolicy(
    maximum_attempts=2,
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=1.0,
    maximum_interval=timedelta(seconds=5),
    non_retryable_error_types=["InvalidInputFormat"],
)

DISTRIBUTION_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=60),
    backoff_coefficient=3.0,
    maximum_interval=timedelta(seconds=960),
    non_retryable_error_types=["AccountBanned", "ContentViolation"],
)


@workflow.defn
class VideoProductionWorkflow:
    """Produce a single video from topic to distribution."""

    @workflow.run
    async def run(
        self,
        topic: TopicCandidate,
        content_format: ContentFormat,
        target_platforms: list[Platform],
    ) -> dict:
        workflow.logger.info("Starting production: topic=%s format=%s", topic.topic, content_format)

        # 1. Select hook via Thompson Sampling
        hook_id = await workflow.execute_activity(
            select_hook,
            args=[topic.niche_slug, target_platforms[0].value, None],
            start_to_close_timeout=timedelta(seconds=10),
        )

        # 2. Generate script
        script_request = ScriptRequest(
            topic=topic,
            content_format=content_format,
            hook_template_id=hook_id,
            target_platforms=target_platforms,
        )
        script: GeneratedScript = await workflow.execute_activity(
            generate_script,
            args=[script_request],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=SCRIPT_RETRY,
        )

        # 3. Parallel asset generation
        vo_task = workflow.execute_activity(
            generate_voiceover,
            args=[script.full_text, None],
            start_to_close_timeout=timedelta(seconds=120),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=ASSET_RETRY,
        )
        img_task = workflow.execute_activity(
            generate_images,
            args=[[script.hook_text, script.body_text], "budget"],
            start_to_close_timeout=timedelta(seconds=120),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=ASSET_RETRY,
        )
        music_task = workflow.execute_activity(
            select_background_music,
            args=["neutral", script.estimated_duration_s],
            start_to_close_timeout=timedelta(seconds=10),
        )

        voiceover, images, music = await asyncio.gather(vo_task, img_task, music_task)

        # 4. Transcribe for captions
        transcript = await workflow.execute_activity(
            transcribe_audio,
            args=[voiceover.file_path],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=ASSET_RETRY,
        )

        # 5. Assemble video
        all_assets = [voiceover] + images + [music]
        video_path: str = await workflow.execute_activity(
            assemble_video,
            args=[all_assets, transcript, content_format.value, script.estimated_duration_s],
            start_to_close_timeout=timedelta(seconds=180),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=ASSEMBLY_RETRY,
        )

        # 6. Export per platform + quality check
        exports: list[PlatformExport] = []
        for platform in target_platforms:
            export: PlatformExport = await workflow.execute_activity(
                export_for_platform,
                args=[video_path, platform.value],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=ASSEMBLY_RETRY,
            )
            qc: QualityReport = await workflow.execute_activity(
                quality_check,
                args=[export.file_path, platform.value],
                start_to_close_timeout=timedelta(seconds=30),
            )
            if qc.passed:
                exports.append(export)
            else:
                workflow.logger.warning(
                    "QC failed for %s: score=%.2f failures=%s",
                    platform, qc.score, qc.blocking_failures,
                )

        return {
            "script_id": str(script.script_id),
            "topic": topic.topic,
            "format": content_format.value,
            "exports": len(exports),
            "platforms": [e.platform.value for e in exports],
        }


@workflow.defn
class DailyBatchWorkflow:
    """Top-level daily production workflow triggered by Temporal Schedule."""

    @workflow.run
    async def run(self, niche_slugs: list[str]) -> dict:
        workflow.logger.info("Starting daily batch for %d niches", len(niche_slugs))

        # 1. Scan trends for each niche
        all_topics: list[TopicCandidate] = []
        for slug in niche_slugs:
            topics = await workflow.execute_activity(
                scan_trends,
                args=[slug],
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=ASSET_RETRY,
            )
            all_topics.extend(topics)

        workflow.logger.info("Found %d topics across %d niches", len(all_topics), len(niche_slugs))

        # 2. Fan out: one child workflow per topic
        handles = []
        for topic in all_topics:
            handle = await workflow.start_child_workflow(
                VideoProductionWorkflow.run,
                args=[topic, ContentFormat.FACELESS, [Platform(topic.platform)]],
                id=f"video-{topic.niche_slug}-{uuid4().hex[:8]}",
            )
            handles.append(handle)

        # 3. Collect results
        results = []
        for handle in handles:
            try:
                result = await handle.result()
                results.append(result)
            except Exception as e:
                workflow.logger.error("Child workflow failed: %s", e)

        # 4. Daily report
        from datetime import date

        await workflow.execute_activity(
            generate_daily_report,
            args=[date.today().isoformat()],
            start_to_close_timeout=timedelta(seconds=60),
        )

        return {
            "topics_found": len(all_topics),
            "videos_produced": len(results),
            "results": results,
        }
