"""Command-line entry points for the GS1 Digital Link Orchestrator.

Run as modules, e.g. ``python -m scripts.parse_export CLIENT_ID``. Scripts land
here per ``docs/IMPLEMENTATION_SPEC.md`` §8: ``inspect_export``, ``parse_export``,
``run_generate``, ``run_plan``, ``run_execute``, ``run_unpublish``,
``build_brick_map``, ``build_video_map``, and ``report_quality``.

(§8.4's ``verify_run`` was never built — post-run URL verification happens inside
``run_execute`` via ``lib.wp_client.WordPressClient.verify_url``. See §8.4.)
"""
