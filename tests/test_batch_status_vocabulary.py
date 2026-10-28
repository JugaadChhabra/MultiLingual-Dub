"""Pin the Video batch status vocabulary.

The Video pane (static/pane-video.js) matches on these exact strings to pick
the live bar, the amber badge and the terminal states of the poll. Renaming a
value here is a cross-file change: update the pane in the same commit.
"""
from typing import get_args

from services.video_pipeline.batch_store import BatchRowState, VideoBatchJobState


def test_batch_status_values():
    assert get_args(VideoBatchJobState.model_fields["status"].annotation) == (
        "queued", "running", "completed", "partial", "failed", "cancelled",
    )


def test_row_status_values():
    assert get_args(BatchRowState.model_fields["status"].annotation) == (
        "pending", "running", "completed", "failed", "cancelled",
    )
